import json
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
import httpx
from app.core.config import settings
from app.services.gemini_client import gemini_http_client
from app.models.account import FieldMapping

logger = logging.getLogger("LLMExtractor")

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


@dataclass
class ExtractionResult:
    raw_response: Dict[str, Any]
    fields_to_update: List[Dict[str, Any]]
    field_name_values: Dict[str, Any]
    error: Optional[str] = None


class LLMExtractor:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.GEMINI_API_KEY

    async def extract_lead_fields(
        self,
        field_mappings: List[FieldMapping],
        messages: List[Dict[str, str]],
        current_lead_values: Optional[Dict[int, Any]] = None,
        model_name: str = "gemini-3.1-flash-lite",
        media_parts: Optional[List[Dict[str, Any]]] = None,
    ) -> ExtractionResult:
        """
        Извлекает значения полей сделки из диалога с клиентом и медиавложений.
        Только включенные поля (is_enabled=True) анализируются.
        """
        enabled_fields = [f for f in field_mappings if f.is_enabled]
        if not enabled_fields:
            return ExtractionResult(raw_response={}, fields_to_update=[], field_name_values={})

        if not self.api_key:
            return ExtractionResult(
                raw_response={},
                fields_to_update=[],
                field_name_values={},
                error="GEMINI_API_KEY is not configured"
            )

        current_values = current_lead_values or {}

        # Формируем описание полей для промпта
        fields_desc = []
        schema_props = {}
        for fm in enabled_fields:
            prop_key = f"field_{fm.amo_field_id}"
            hint = f" ({fm.ai_hint})" if fm.ai_hint else ""
            fields_desc.append(f'- key: "{prop_key}", Название: "{fm.field_name}"{hint}, Тип: {fm.field_type}')
            
            # Схема типов для Gemini
            ft = (fm.field_type or "").lower()
            if ft in ("numeric", "price"):
                schema_props[prop_key] = {"type": "NUMBER"}
            elif ft in ("checkbox", "boolean"):
                schema_props[prop_key] = {"type": "BOOLEAN"}
            else:
                schema_props[prop_key] = {"type": "STRING"}

        fields_doc = "\n".join(fields_desc)

        # Текст диалога
        conv_text = "\n".join([
            f"{'Клиент' if m.get('role') == 'user' else 'Менеджер'}: {m.get('content', '')}"
            for m in messages
        ])

        system_instruction = (
            "Ты — аналитик CRM. Твоя задача — внимательно изучить диалог между Клиентом и Менеджером, "
            "а также прикрепленные медиафайлы (голосовые сообщения, кругляшки, фото чеков/товаров) "
            "и извлечь ТОЧНЫЕ факты о клиенте и его запросе для сохранения в CRM.\n\n"
            "Список доступных полей для извлечения:\n"
            f"{fields_doc}\n\n"
            "ПРАВИЛА:\n"
            "1. Извлекай только ту информацию, о которой клиент явно сообщил сам или подтвердил слова менеджера.\n"
            "2. Если поле не упоминалось или нет уверенности — НЕ добавляй его в результат или укажи null.\n"
            "3. Не придумывай и не домысливай факты.\n"
            "4. Верни JSON-объект, где ключи — это строго идентификаторы полей (например field_12345), а значения — извлеченные данные."
        )

        user_content = f"Диалог:\n{conv_text}\n\nИзвлеки все релевантные поля."
        user_parts: List[Dict[str, Any]] = [{"text": user_content}]

        if media_parts:
            for mp in media_parts:
                user_parts.append({
                    "inlineData": {
                        "mimeType": mp["mime_type"],
                        "data": mp["data_b64"]
                    }
                })

        body = {
            "systemInstruction": {
                "parts": [{"text": system_instruction}]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": user_parts
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": schema_props
                }
            }
        }

        url = GEMINI_API_URL.format(model=model_name)
        params = {"key": self.api_key}

        try:
            client = await gemini_http_client.get_client()
            resp = await client.post(url, params=params, json=body)
            if resp.status_code != 200:
                logger.error(f"Gemini extractor error {resp.status_code}: {resp.text}")
                return ExtractionResult(
                    raw_response={},
                    fields_to_update=[],
                    field_name_values={},
                    error=f"Gemini error {resp.status_code}: {resp.text[:100]}"
                )

            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return ExtractionResult(raw_response={}, fields_to_update=[], field_name_values={})

            raw_json_str = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
            try:
                parsed = json.loads(raw_json_str)
            except Exception as e:
                logger.error(f"Не удалось распарсить JSON экстрактора: {raw_json_str} ({e})")
                return ExtractionResult(raw_response={"raw": raw_json_str}, fields_to_update=[], field_name_values={}, error=str(e))

            # Фильтруем и сопоставляем с AmoCRM
            fields_to_update = []
            field_name_values = {}

            for fm in enabled_fields:
                prop_key = f"field_{fm.amo_field_id}"
                val = parsed.get(prop_key)
                if val is None or val == "" or val == "null":
                    continue

                # Проверяем, заполнено ли уже поле и разрешена ли перезапись
                if not fm.overwrite_if_filled:
                    existing = current_values.get(fm.amo_field_id)
                    if existing is not None and existing != "":
                        # Пропускаем, так как перезапись отключена
                        continue

                fields_to_update.append({
                    "field_id": fm.amo_field_id,
                    "values": [{"value": val}]
                })
                field_name_values[fm.field_name] = val

            return ExtractionResult(
                raw_response=parsed,
                fields_to_update=fields_to_update,
                field_name_values=field_name_values
            )

        except Exception as e:
            logger.exception(f"Исключение при экстракции полей: {e}")
            return ExtractionResult(
                raw_response={},
                fields_to_update=[],
                field_name_values={},
                error=str(e)
            )


extractor = LLMExtractor()

