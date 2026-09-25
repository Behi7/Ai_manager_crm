import asyncio
import logging
import re
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
import httpx
from app.core.config import settings
from app.services.gemini_client import gemini_http_client

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
        fallback_model: Optional[str] = None,
        temperature: float = 0.4,
        lead_context: Optional[Dict[str, Any]] = None,
        media_parts: Optional[List[Dict[str, Any]]] = None,
        target_fields: Optional[List[Dict[str, str]]] = None,
        known_fields: Optional[Dict[str, Any]] = None,
        knowledge_base: Optional[str] = None,
        knowledge_mode: str = "plain_text",
        gemini_cache_name: Optional[str] = None,
        is_comment: bool = False,
        direct_link: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> CommunicatorResponse:
        """
        Генерация ответа клиенту на основе истории сообщений, мультимодальных вложений
        и целевых квалификационных полей (FieldMappings).
        messages: список словарей [{"role": "user"|"assistant", "content": "..."}]
        media_parts: список словарей [{"mime_type": "audio/ogg", "data_b64": "..."}]
        target_fields: список полей, которые нужно выяснить [{"name": "...", "hint": "..."}]
        known_fields: словарь уже заполненных данных сделки {"Имя поля": "Значение"}
        """
        effective_api_key = (api_key or "").strip() or self.api_key
        if not effective_api_key:
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
            known_lines = [f"- {k}: {v} (это данные САМОГО КЛИЕНТА, а НЕ контакты нашей компании)" for k, v in all_known.items() if v]
            if known_lines:
                known_info = (
                    "\n\nУЖЕ ИЗВЕСТНЫЕ ДАННЫЕ О КЛИЕНТЕ И СДЕЛКЕ: (АНКЕТНЫЕ ДАННЫЕ САМОГО КЛИЕНТА, НЕ НАШЕЙ КОМПАНИИ)\n"
                    + "\n".join(known_lines)
                    + "\n(НЕ переспрашивай эти параметры у клиента и НИКОГДА не выдавай телефон клиента за номер телефона нашей компании)"
                )

        # 2. Формирование блока целевых параметров для квалификации (FieldMapping)
        qualification_instruction = ""
        if target_fields:
            target_lines = [f"- {f.get('name')}: {f.get('hint', '')}" for f in target_fields if f.get('name')]
            if target_lines:
                qualification_instruction = (
                    "\n\nЦЕЛЕВЫЕ ДАННЫЕ, КОТОРЫЕ НУЖНО ВЫЯСНИТЬ У КЛИЕНТА (КВАЛИФИКАЦИЯ):\n"
                    + "\n".join(target_lines)
                    + "\n\nСТРОГИЕ ПРАВИЛА КВАЛИФИКАЦИИ И ОБЩЕНИЯ (ФАЗА СБОРА ДАННЫХ):\n"
                    "1. ⚠️ ВНИМАНИЕ — КВАЛИФИКАЦИЯ ЕЩЁ НЕ ЗАВЕРШЕНА! В списке выше остались незаполненные поля. "
                    "Пока этот список не пуст, КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО говорить клиенту, что «все данные записаны», «специалисты скоро свяжутся» "
                    "или спрашивать «есть ли у вас ещё вопросы?» (это условие из системного промпта действует ТОЛЬКО когда список целевых данных пуст!).\n"
                    "2. ПРАВИЛО ОДНОГО ВОПРОСА: Задавай максимум ОДИН ненавязчивый вопрос в сообщении! (по одному из недостающих параметров из списка выше). "
                    "Категорически запрещено присылать списки вопросов, анкеты или опросники.\n"
                    "3. СНАЧАЛА ПОЛЬЗА/ОТВЕТ, ЗАТЕМ ВОПРОС: Всегда сначала дай полноценный, дружелюбный ответ на вопрос или реплику клиента, "
                    "и только затем органично задай уместный вопрос по одному недостающему параметру из списка выше.\n"
                    "4. НЕ ПЕРЕСПРАШИВАЙ: Если клиент уже сообщил информацию или она указана в уже известных данных, не спрашивай повторно — спрашивай только то, что осталось в списке «ЦЕЛЕВЫЕ ДАННЫЕ, КОТОРЫЕ НУЖНО ВЫЯСНИТЬ»."
                )
        elif known_info and (lead_context or known_fields):
            qualification_instruction = (
                "\n\nВсе ключевые квалификационные параметры сделки уже выяснены. "
                "Больше НЕ нужно задавать квалификационные вопросы по анкете. "
                "Поблагодари клиента, сообщи что все данные записаны и специалисты свяжутся с ним в скором времени, и спроси, остались ли у него вопросы."
            )

        # 2.5. Формирование блока для публичных комментариев соцсетей
        comment_instruction = ""
        if is_comment:
            direct_part = f" Обязательно в конце сообщения укажи ссылку на Direct: {direct_link} для перехода в личные сообщения." if direct_link else ""
            comment_instruction = (
                "\n\nРЕЖИМ ОТВЕТА НА ПУБЛИЧНЫЙ КОММЕНТАРИЙ СОЦСЕТИ:\n"
                "Клиент написал комментарий под постом/Reels в соцсети (Instagram, Facebook и т.д.).\n"
                "1. Отвечай кратко, дружелюбно и по существу (1-2 предложения).\n"
                "2. Если клиент прислал '+', '++', огонёк 🔥, смайлик или вопрос о цене/товаре — поблагодари за интерес и предложи подробности в Direct.\n"
                f"3. Пригласи продолжить диалог в Direct.{direct_part}\n"
                "4. НЕ задавай длинных списков квалификационных вопросов под публичным постом."
            )
            # В режиме публичного комментария отключаем анкетную квалификацию
            qualification_instruction = ""

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

        # 3. Формирование блока Базы знаний и каталога продуктов
        knowledge_instruction = ""
        has_active_cache = bool(gemini_cache_name and knowledge_mode == "gemini_cache")

        if knowledge_base and knowledge_mode != "disabled" and not has_active_cache:
            knowledge_instruction = (
                "\n\nБАЗА ЗНАНИЙ И КАТАЛОГ ПРОДУКТОВ КОМПАНИИ:\n"
                "-----------------------------------------\n"
                f"{knowledge_base.strip()}\n"
                "-----------------------------------------\n"
                "ПРАВИЛА ИСПОЛЬЗОВАНИЯ КАТАЛОГА И РЕКОМЕНДАЦИЙ:\n"
                "1. Этот каталог — твоя внутренняя экспертная база знаний. Используй её для точных ответов на вопросы о товарах, услугах, тарифах и ценах.\n"
                "2. СТРОГИЙ ЗАПРЕТ НА СПАМ КАТАЛОГОМ И ПОВЕДЕНИЕ ПРОДАЖНИКА НА ПРИВЕТСТВИЕ: Если клиент просто поздоровался ('Привет', 'Salom', 'Assalomu alaykum') — НЕ вываливай сразу весь прайс-лист, но и КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО отвечать шаблонной фразой справочной службы «Чем я могу вам помочь?» / «Sizga qanday yordam bera olaman?». Ты — активный менеджер по продажам! Поздоровайся, в 1 короткой фразе обозначь направление компании (согласно системному промпту) и сразу перехвати инициативу: задай первый живой вопрос по квалификации клиента из списка целевых данных (например, что именно интересует для бизнеса или какая задача стоит).\n"
                "3. ТОЧЕЧНАЯ РЕКОМЕНДАЦИЯ: Рекомендуй конкретный продукт (1, максимум 2 подходящих варианта) ТОЛЬКО тогда, когда клиент сам спросил о товарах/ценах или когда в ходе диалога стали понятны его потребности.\n"
                "4. ОБОСНОВАНИЕ ВЫГОДЫ: Предлагая продукт, кратко поясни, почему именно он подходит клиенту (например: 'Для вашей команды из 8 человек оптимален тариф X, так как в него уже включена интеграция WhatsApp').\n"
                "5. ТОЧНОСТЬ: Называй цены и характеристики строго из базы знаний выше. Запрещено выдумывать несуществующие скидки, акции или товары."
            )

        formatted_system_prompt = system_prompt
        if direct_link:
            formatted_system_prompt = formatted_system_prompt.replace("{direct_link}", direct_link)

        full_system_instruction = (
            f"{formatted_system_prompt}\n"
            f"{knowledge_instruction}"
            f"{known_info}"
            f"{qualification_instruction}"
            f"{comment_instruction}"
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

        # Очередь моделей для вызова: сначала основная, затем настраиваемая резервная
        models_to_try = [model_name]
        chosen_fallback = (fallback_model or "").strip()
        if not chosen_fallback:
            chosen_fallback = "gemini-2.5-flash" if model_name != "gemini-2.5-flash" else "gemini-flash-latest"
        if chosen_fallback and chosen_fallback not in models_to_try:
            models_to_try.append(chosen_fallback)

        last_error: Optional[str] = None

        for cur_model in models_to_try:
            is_fallback = (cur_model != model_name)
            # При переключении на резервную модель отключаем кэш, так как он привязан к конкретной модели
            use_cache = has_active_cache and not is_fallback

            req_body: Dict[str, Any] = {
                "contents": combined_contents,
                "generationConfig": {
                    "temperature": float(temperature),
                    "maxOutputTokens": 400
                }
            }
            if use_cache:
                req_body["cachedContent"] = gemini_cache_name
            else:
                req_body["systemInstruction"] = {
                    "parts": [{"text": full_system_instruction}]
                }

            url = GEMINI_API_URL.format(model=cur_model)
            params = {"key": effective_api_key}

            # До 2 попыток на каждую модель (повтор при 503/429/5xx/сетевом сбое)
            for attempt in range(1, 3):
                try:
                    client = await gemini_http_client.get_client()
                    resp = await client.post(url, params=params, json=req_body)

                    if resp.status_code == 200:
                        data = resp.json()
                        candidates = data.get("candidates", [])
                        if not candidates:
                            logger.warning(f"Gemini {cur_model} вернул 200 без кандидатов: {data}")
                            break

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

                        if is_fallback:
                            logger.info(f"✅ Успешный ответ от резервной модели {cur_model} после сбоя основной {model_name}")

                        return CommunicatorResponse(
                            text=cleaned_text,
                            is_handover_requested=is_handover,
                            media_summary=media_summary
                        )

                    # Обработка временных ошибок перегрузки (503, 429, 500, 502, 504)
                    last_error = f"Gemini {cur_model} HTTP {resp.status_code}: {resp.text[:150]}"
                    if resp.status_code in (503, 429, 500, 502, 504):
                        logger.warning(
                            f"Gemini {cur_model} вернул статус {resp.status_code} (попытка {attempt}/2). "
                            f"{'Ожидание 1с перед повтором...' if attempt == 1 else 'Переключение на резервную модель...'}"
                        )
                        if attempt == 1:
                            await asyncio.sleep(1.0)
                            continue
                        else:
                            break
                    else:
                        logger.error(f"Gemini {cur_model} неустранимая ошибка {resp.status_code}: {resp.text}")
                        break

                except (httpx.TimeoutException, httpx.NetworkError) as net_err:
                    last_error = f"Gemini {cur_model} сетевая ошибка/таймаут: {net_err}"
                    logger.warning(
                        f"Сетевая ошибка/таймаут при вызове {cur_model} (попытка {attempt}/2): {net_err}"
                    )
                    if attempt == 1:
                        await asyncio.sleep(1.0)
                        continue
                    else:
                        break
                except Exception as ex:
                    last_error = f"Исключение при вызове {cur_model}: {ex}"
                    logger.exception(f"Непредвиденное исключение при вызове {cur_model}: {ex}")
                    break

        # Если все попытки и резервные модели исчерпаны
        logger.error(f"Все попытки вызова моделей Gemini ({models_to_try}) завершились сбоем: {last_error}")
        return CommunicatorResponse(
            text="Прошу прощения, сейчас возникла задержка связи. Переключаю вас на специалиста, он уже подключается к диалогу.",
            is_handover_requested=True,
            error=last_error or "Все модели Gemini недоступны"
        )

    async def create_gemini_context_cache(
        self,
        model_name: str,
        system_instruction: str,
        knowledge_content: str,
        ttl_seconds: int = 3600,
        api_key: Optional[str] = None,
    ) -> Optional[str]:
        """
        Создание или обновление кэша контекста (CachedContent) в Google Gemini API.
        Возвращает имя кэша (например, 'cachedContents/abc123xyz') или None при недостатке токенов (<32k) или ошибке.
        """
        effective_api_key = (api_key or "").strip() or self.api_key
        if not effective_api_key or not knowledge_content:
            return None

        clean_model = model_name
        if not clean_model.startswith("models/"):
            clean_model = f"models/{clean_model}"

        url = "https://generativelanguage.googleapis.com/v1beta/cachedContents"
        params = {"key": effective_api_key}
        payload = {
            "model": clean_model,
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": f"БАЗА ЗНАНИЙ И КАТАЛОГ ПРОДУКТОВ:\n{knowledge_content}"}]
                }
            ],
            "systemInstruction": {
                "parts": [{"text": system_instruction}]
            },
            "ttl": f"{ttl_seconds}s"
        }

        try:
            client = await gemini_http_client.get_client()
            resp = await client.post(url, params=params, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                cache_name = data.get("name")
                logger.info(f"Успешно создан Gemini Context Cache: {cache_name}")
                return cache_name
            else:
                logger.info(
                    f"Gemini Context Cache не создан (HTTP {resp.status_code}): {resp.text[:150]}. "
                    "Будет использован надёжный прямой In-Context режим."
                )
                return None
        except Exception as err:
            logger.warning(f"Ошибка при создании Gemini Context Cache: {err}")
            return None


communicator = LLMCommunicator()
