import logging
import re
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
import httpx
from app.core.config import settings

logger = logging.getLogger("LLMCommunicator")

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

HANDOVER_PATTERNS = [
    r"\b(оператор[а-я]*|человек[а-я]*|менеджер[а-я]*|сотрудник[а-я]*|живо[йг][оа-я]*|позови[а-я]*|соедини[а-я]*)\b",
    r"\b(operator[a-z]*|odam[a-z]*|menejer[a-z]*|tirik\s*odam|hodim[a-z]*)\b",
]

@dataclass
class CommunicatorResponse:
    text: str
    is_handover_requested: bool = False
    error: Optional[str] = None
    media_summary: Optional[str] = None


class LLMCommunicator:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.GEMINI_API_KEY

    def _check_handover_intent(self, text: str) -> bool:
        """Проверка, требует ли клиент оператора явным текстом"""
        text_lower = text.lower()
        for pattern in HANDOVER_PATTERNS:
            if re.search(pattern, text_lower):
                return True
        return False

    def _clean_response(self, text: str) -> str:
        """
        Очистка ответа от плейсхолдеров в квадратных скобках и артефактов.
        Если модель всё же выдала [укажите цену] или подобное, заменяем на естественную фразу.
        """
        # Удаляем или заменяем плейсхолдеры в квадратных скобках
        cleaned = re.sub(r"\[(цена|стоимость|укажите цену|вставьте цену)[^\]]*\]", "уточняю стоимость", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"\[(имя|ваше имя|клиент)[^\]]*\]", "уважаемый клиент", cleaned, flags=re.IGNORECASE)
        # Если остались любые другие [ ... ], убираем скобки
        cleaned = re.sub(r"\[([^\]]+)\]", r"\1", cleaned)
        return cleaned.strip()

    async def generate_reply(
        self,
        system_prompt: str,
        messages: List[Dict[str, str]],
        model_name: str = "gemini-3.1-flash-lite",
        temperature: float = 0.4,
        lead_context: Optional[Dict[str, Any]] = None,
        media_parts: Optional[List[Dict[str, Any]]] = None,
        target_fields: Optional[List[Dict[str, str]]] = None,
        known_fields: Optional[Dict[str, Any]] = None,
    ) -> CommunicatorResponse:
        """
        Генерация ответа клиенту на основе истории сообщений, мультимодальных вложений
        и целевых квалификационных полей (FieldMappings).
        messages: список словарей [{"role": "user"|"assistant", "content": "..."}]
        media_parts: список словарей [{"mime_type": "audio/ogg", "data_b64": "..."}]
        target_fields: список полей, которые нужно выяснить [{"name": "...", "hint": "..."}]
        known_fields: словарь уже заполненных данных сделки {"Имя поля": "Значение"}
        """
        if not self.api_key:
            return CommunicatorResponse(
                text="Здравствуйте! Спасибо за обращение. Скоро мы свяжемся с вами.",
                error="GEMINI_API_KEY is not set"
            )

        # Проверка последнего сообщения пользователя на запрос оператора
        last_user_msg = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        is_handover = self._check_handover_intent(last_user_msg)

        # 1. Формирование блока уже известных данных о клиенте и сделке
        all_known = {}
        if lead_context:
            all_known.update(lead_context)
        if known_fields:
            all_known.update(known_fields)

        known_info = ""
        if all_known:
            known_lines = [f"- {k}: {v}" for k, v in all_known.items() if v]
            if known_lines:
                known_info = (
                    "\n\nУЖЕ ИЗВЕСТНЫЕ ДАННЫЕ О КЛИЕНТЕ И СДЕЛКЕ:\n"
                    + "\n".join(known_lines)
                    + "\n(НЕ переспрашивай эти параметры, используй их для контекста и персонализации ответа)"
                )

        # 2. Формирование блока целевых параметров для квалификации (FieldMapping)
        qualification_instruction = ""
        if target_fields:
            target_lines = [f"- {f.get('name')}: {f.get('hint', '')}" for f in target_fields if f.get('name')]
            if target_lines:
                qualification_instruction = (
                    "\n\nЦЕЛЕВЫЕ ДАННЫЕ, КОТОРЫЕ НУЖНО ВЫЯСНИТЬ У КЛИЕНТА (КВАЛИФИКАЦИЯ):\n"
                    + "\n".join(target_lines)
                    + "\n\nСТРОГИЕ ПРАВИЛА КВАЛИФИКАЦИИ И ОБЩЕНИЯ:\n"
                    "1. ПРАВИЛО ОДНОГО ВОПРОСА: Задавай максимум ОДИН ненавязчивый вопрос в сообщении! "
                    "Категорически запрещено присылать списки вопросов, анкеты или опросники.\n"
                    "2. СНАЧАЛА ПОЛЬЗА/ОТВЕТ, ЗАТЕМ ВОПРОС: Всегда сначала дай полноценный, дружелюбный ответ на вопрос или реплику клиента, "
                    "и только затем органично задай уместный вопрос по одному из недостающих параметров.\n"
                    "3. ЕСТЕСТВЕННОСТЬ: Задавай вопрос только тогда, когда это уместно по теме беседы. "
                    "Веди диалог как живой, заботливый менеджер по продажам.\n"
                    "4. НЕ ПЕРЕСПРАШИВАЙ: Если клиент уже сообщил информацию в переписке или она указана в уже известных данных, не спрашивай повторно."
                )
        elif known_info and (lead_context or known_fields):
            qualification_instruction = (
                "\n\nВсе ключевые квалификационные параметры сделки уже выяснены. "
                "Веди диалог к следующему целевому действию или отвечай на вопросы клиента."
            )

        media_instruction = ""
        if media_parts:
            media_instruction = (
                "\n\nИНСТРУКЦИЯ ПО ОБРАБОТКЕ МЕДИАФАЙЛОВ:\n"
                "К последнему обращению клиента прикреплен медиафайл (голосовое сообщение, кругляшек Telegram, видео или фото).\n"
                "1. Если это аудио или видеосообщение — внимательно прослушай речь, интонации и акцент клиента.\n"
                "2. Если это фото — внимательно изучи изображение (товар, документ, чек, скриншот).\n"
                "3. В САМОЙ ПЕРВОЙ строке своего ответа напиши краткую расшифровку или суть содержимого в формате:\n"
                "   [МЕДИА: <что именно сказал клиент или что изображено на фото/видео>]\n"
                "4. Со второй строки напиши живой, доброжелательный ответ клиенту (который будет отправлен в чат).\n"
                "5. Если в аудио/видео клиент явно просит позвать человека/оператора/менеджера, добавь в конце ответа маркер [[HANDOVER]]."
            )

        full_system_instruction = (
            f"{system_prompt}\n"
            f"{known_info}"
            f"{qualification_instruction}"
            f"{media_instruction}\n\n"
            "СТРОЖАЙШИЕ ПРАВИЛА ВЫВОДА:\n"
            "1. Запрещено использовать плейсхолдеры в квадратных скобках вида [цена], [имя], [товар]. "
            "Если точная информация неизвестна, ответь как живой менеджер: скажи, что уточняешь детали у склада/коллег, либо задай уточняющий вопрос.\n"
            "2. Не здоровайся повторно, если в диалоге уже есть приветствие.\n"
            "3. Будь лаконичным (1-3 живых, емких предложения), дружелюбным и естественным.\n"
            "4. Отвечай строго на том языке, на котором пишет или говорит клиент (русский или узбекский)."
        )

        # Формируем contents для Gemini API
        contents = []
        for msg in messages:
            role = "model" if msg.get("role") == "assistant" else "user"
            content = msg.get("content", "").strip()
            if content:
                contents.append({
                    "role": role,
                    "parts": [{"text": content}]
                })

        if not contents:
            contents.append({
                "role": "user",
                "parts": [{"text": "Здравствуйте"}]
            })

        # Ensure that Gemini turns alternate properly or combine consecutive same-role messages
        combined_contents = []
        for item in contents:
            if combined_contents and combined_contents[-1]["role"] == item["role"]:
                combined_contents[-1]["parts"][0]["text"] += f"\n{item['parts'][0]['text']}"
            else:
                combined_contents.append(item)

        # First message in Gemini must be role 'user'
        if combined_contents and combined_contents[0]["role"] != "user":
            combined_contents.insert(0, {"role": "user", "parts": [{"text": "Здравствуйте"}]})

        # Прикрепляем медиафайлы к последнему сообщению пользователя
        if media_parts and combined_contents:
            last_user_turn = None
            for turn in reversed(combined_contents):
                if turn["role"] == "user":
                    last_user_turn = turn
                    break
            if last_user_turn is None:
                last_user_turn = {"role": "user", "parts": [{"text": "Входящее вложение:"}]}
                combined_contents.append(last_user_turn)

            for mp in media_parts:
                last_user_turn["parts"].append({
                    "inlineData": {
                        "mimeType": mp["mime_type"],
                        "data": mp["data_b64"]
                    }
                })

        body = {
            "systemInstruction": {
                "parts": [{"text": full_system_instruction}]
            },
            "contents": combined_contents,
            "generationConfig": {
                "temperature": float(temperature),
                "maxOutputTokens": 400
            }
        }

        url = GEMINI_API_URL.format(model=model_name)
        params = {"key": self.api_key}

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, params=params, json=body)
                if resp.status_code != 200:
                    logger.error(f"Gemini error {resp.status_code}: {resp.text}")
                    return CommunicatorResponse(
                        text="Спасибо за сообщение! Минуту, проверяю информацию.",
                        is_handover_requested=is_handover,
                        error=f"Gemini API returned {resp.status_code}: {resp.text[:100]}"
                    )

                data = resp.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    return CommunicatorResponse(
                        text="Спасибо! Минуту, я уточняю информацию.",
                        is_handover_requested=is_handover
                    )

                parts = candidates[0].get("content", {}).get("parts", [])
                text_parts = [p.get("text", "") for p in parts if "text" in p]
                raw_text = "".join(text_parts).strip()

                # Извлечение краткой транскрипции/сути медиа
                media_summary = None
                media_match = re.search(r"\[МЕДИА:\s*([^\]]+)\]", raw_text, flags=re.IGNORECASE)
                if media_match:
                    media_summary = f"[Медиа: {media_match.group(1).strip()}]"
                    raw_text = re.sub(r"\[МЕДИА:\s*[^\]]+\]", "", raw_text, flags=re.IGNORECASE).strip()

                # Проверка явного маркера перевода на человека из медиа
                if "[[HANDOVER]]" in raw_text or "[[handover]]" in raw_text:
                    is_handover = True
                    raw_text = raw_text.replace("[[HANDOVER]]", "").replace("[[handover]]", "").strip()

                cleaned_text = self._clean_response(raw_text)

                return CommunicatorResponse(
                    text=cleaned_text,
                    is_handover_requested=is_handover,
                    media_summary=media_summary
                )

        except Exception as e:
            logger.exception(f"Исключение при генерации ответа LLM: {e}")
            return CommunicatorResponse(
                text="Спасибо за ожидание! Скоро отвечу вам.",
                is_handover_requested=is_handover,
                error=str(e)
            )


communicator = LLMCommunicator()
