import re
import asyncio
import json
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
import httpx
from app.core.config import settings
from app.services.gemini_client import gemini_http_client
from app.models.account import FieldMapping, DEFAULT_EXTRACTOR_SYSTEM_PROMPT

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

    @staticmethod
    def _is_same_or_redundant_value(existing: Any, new_val: Any, field_type: Optional[str], field_name: Optional[str]) -> bool:
        ex_str = str(existing or "").strip()
        nw_str = str(new_val or "").strip()
        if not ex_str or not nw_str:
            return False
        if ex_str.lower() == nw_str.lower():
            return True
        ft = (field_type or "").lower()
        fn = (field_name or "").lower()
        if ft == "multitext" or "телефон" in fn or "phone" in fn or "raqam" in fn or "номер" in fn:
            ex_digits = re.sub(r"\D", "", ex_str)
            nw_digits = re.sub(r"\D", "", nw_str)
            if len(ex_digits) >= 7 and len(nw_digits) >= 7 and ex_digits[-9:] == nw_digits[-9:]:
                return True
        ex_clean = re.sub(r"[^\w\s]", "", ex_str.lower()).strip()
        nw_clean = re.sub(r"[^\w\s]", "", nw_str.lower()).strip()
        if ex_clean and nw_clean and (ex_clean == nw_clean or ex_clean in nw_clean or nw_clean in ex_clean):
            return True
        return False

    async def extract_lead_fields(
        self,
        field_mappings: List[FieldMapping],
        messages: List[Dict[str, str]],
        current_lead_values: Optional[Dict[int, Any]] = None,
        model_name: str = "gemini-3.1-flash-lite",
        fallback_model: Optional[str] = None,
        media_parts: Optional[List[Dict[str, Any]]] = None,
        api_key: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ) -> ExtractionResult:
        """
        Извлекает значения полей сделки из диалога с клиентом и медиавложений.
        Только включенные поля (is_enabled=True) анализируются.
        """
        enabled_fields = [f for f in field_mappings if f.is_enabled]
        if not enabled_fields:
            return ExtractionResult(raw_response={}, fields_to_update=[], field_name_values={})

        effective_api_key = (api_key or "").strip() or self.api_key
        if not effective_api_key:
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
            entity = (fm.entity_type if isinstance(getattr(fm, "entity_type", None), str) and fm.entity_type in ("lead", "contact") else "lead")
            if fm.amo_field_id == -1:
                prop_key = "field_contact_sys_name"
            elif fm.amo_field_id == -2:
                prop_key = "field_lead_sys_name"
            else:
                prop_key = f"field_{entity}_{fm.amo_field_id}"
            hint = f" ({fm.ai_hint})" if fm.ai_hint else ""
            existing_val = current_values.get((entity, fm.amo_field_id), current_values.get(fm.amo_field_id))
            if existing_val is not None and str(existing_val).strip() != "":
                cur_val_str = f', Текущее значение в CRM: "{existing_val}"'
            elif fm.amo_field_id == -1:
                raw_nick = str(current_values.get("raw_contact_name") or "").strip()
                cur_val_str = (
                    f', Текущее значение в CRM: ПУСТО (Ник контакта из мессенджера Telegram/Instagram: "{raw_nick}". '
                    "ПРАВИЛО ДЛЯ ИМЕНИ КОНТАКТА: Если клиент назвал своё имя в диалоге — верни это имя. "
                    f'Если в диалоге ещё не называл, посмотри на ник мессенджера "{raw_nick}": если там написано настоящее человеческое имя (например Salohiddin, Алишер, Hojiakbar, Дильшод) — верни это имя! '
                    "Если же в нике написано название компании/отдела вроде Texnik Bo'lim, Marketing Markazi, магазин, либо случайный набор букв/цифр вроде йцуке123, user777, смайлики — НЕ используй ник и верни null!)"
                )
            elif fm.amo_field_id == -2:
                raw_lead = str(current_values.get("raw_lead_name") or "").strip()
                cur_val_str = (
                    f', Текущее значение в CRM: ПУСТО (Сейчас авто-шаблон amoCRM: "{raw_lead}". '
                    "Сформируй краткое название сделки только тогда, когда уже понятна суть запроса клиента или его имя + запрос; если клиент только поздоровался — верни null)"
                )
            else:
                cur_val_str = ', Текущее значение в CRM: ПУСТО'
            fields_desc.append(f'- key: "{prop_key}", Название: "{fm.field_name}"{hint}, Сущность: {entity}, Тип: {fm.field_type}{cur_val_str}')
            
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

        base_ext_prompt = (system_prompt or "").strip() or DEFAULT_EXTRACTOR_SYSTEM_PROMPT
        system_instruction = (
            f"{base_ext_prompt}\n\n"
            "Список доступных полей для извлечения (Available CRM fields to extract):\n"
            f"{fields_doc}"
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

        # Очередь моделей для вызова: сначала основная, затем настраиваемая резервная
        models_to_try = [model_name]
        chosen_fallback = (fallback_model or "").strip()
        if not chosen_fallback:
            chosen_fallback = "gemini-2.5-flash" if model_name != "gemini-2.5-flash" else "gemini-flash-latest"
        if chosen_fallback and chosen_fallback not in models_to_try:
            models_to_try.append(chosen_fallback)

        last_error: Optional[str] = None
        parsed: Optional[Dict[str, Any]] = None

        for cur_model in models_to_try:
            is_fallback = (cur_model != model_name)
            url = GEMINI_API_URL.format(model=cur_model)
            headers = {"x-goog-api-key": effective_api_key}

            # До 2 попыток на каждую модель (повтор при 503/429/5xx/сетевом сбое)
            for attempt in range(1, 3):
                try:
                    client = await gemini_http_client.get_client()
                    resp = await client.post(url, headers=headers, json=body)
                    if resp.status_code == 200:
                        data = resp.json()
                        candidates = data.get("candidates", [])
                        if not candidates:
                            logger.warning(f"Экстрактор {cur_model} вернул 200 без кандидатов: {data}")
                            break
                        raw_json_str = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
                        try:
                            parsed = json.loads(raw_json_str)
                            if is_fallback:
                                logger.info(f"✅ Экстрактор успешно сработал на резервной модели {cur_model}!")
                            last_error = None
                            break
                        except Exception as e:
                            logger.error(f"Не удалось распарсить JSON экстрактора {cur_model}: {raw_json_str} ({e})")
                            last_error = str(e)
                            break

                    last_error = f"Gemini extractor {cur_model} HTTP {resp.status_code}: {resp.text[:100]}"
                    if resp.status_code in (503, 429, 500, 502, 504):
                        logger.warning(
                            f"Gemini extractor {cur_model} вернул статус {resp.status_code} (попытка {attempt}/2). "
                            f"{'Ожидание 1с перед повтором...' if attempt == 1 else 'Переключение...'}"
                        )
                        if attempt == 1:
                            await asyncio.sleep(1.0)
                            continue
                        else:
                            break
                    else:
                        logger.error(f"Gemini extractor {cur_model} неустранимая ошибка {resp.status_code}: {resp.text}")
                        break

                except (httpx.TimeoutException, httpx.NetworkError) as net_err:
                    last_error = f"Сетевая ошибка/таймаут экстрактора {cur_model}: {net_err}"
                    logger.warning(f"Сетевая ошибка экстрактора {cur_model} (попытка {attempt}/2): {net_err}")
                    if attempt == 1:
                        await asyncio.sleep(1.0)
                        continue
                    else:
                        break
                except Exception as ex:
                    last_error = f"Исключение экстрактора {cur_model}: {ex}"
                    logger.exception(f"Непредвиденное исключение экстрактора {cur_model}: {ex}")
                    break

            if parsed is not None:
                break

        if not parsed:
            if last_error:
                logger.error(f"Экстракция полей не удалась на всех моделях ({models_to_try}): {last_error}")
            return ExtractionResult(
                raw_response={},
                fields_to_update=[],
                field_name_values={},
                error=last_error
            )

        # Фильтруем и сопоставляем с AmoCRM
        fields_to_update = []
        field_name_values = {}

        for fm in enabled_fields:
            entity = (fm.entity_type if isinstance(getattr(fm, "entity_type", None), str) and fm.entity_type in ("lead", "contact") else "lead")
            if fm.amo_field_id == -1:
                prop_key = "field_contact_sys_name"
            elif fm.amo_field_id == -2:
                prop_key = "field_lead_sys_name"
            else:
                prop_key = f"field_{entity}_{fm.amo_field_id}"
            val = parsed.get(prop_key, parsed.get(f"field_{entity}_{fm.amo_field_id}", parsed.get(f"field_{fm.amo_field_id}")))
            if val is None or val == "" or val == "null":
                continue

            # Проверяем, заполнено ли уже поле и изменилось ли значение по существу
            existing = current_values.get((entity, fm.amo_field_id), current_values.get(fm.amo_field_id))
            if existing is not None and str(existing).strip() != "":
                if not fm.overwrite_if_filled:
                    continue
                if self._is_same_or_redundant_value(existing, val, fm.field_type, fm.field_name):
                    continue

            val_obj: Dict[str, Any] = {"value": val}
            if (fm.field_type or "").lower() == "multitext":
                val_obj["enum_code"] = "WORK"

            fields_to_update.append({
                "field_id": fm.amo_field_id,
                "entity_type": entity,
                "values": [val_obj]
            })
            field_name_values[fm.field_name] = val

        return ExtractionResult(
            raw_response=parsed,
            fields_to_update=fields_to_update,
            field_name_values=field_name_values
        )


extractor = LLMExtractor()

