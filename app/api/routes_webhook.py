import json
import logging
import time
import uuid
from typing import Optional, Dict, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request, HTTPException, status
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.account import Account, AccountStatus
from app.services.debounce_service import debounce_service
from app.services.delivery_service import delivery_service

logger = logging.getLogger("WebhookRouter")

router = APIRouter(tags=["Webhooks"])


def _format_message_text(raw_text: str, attachment: Optional[Dict[str, Any]]) -> str:
    clean_text = raw_text.strip()
    if clean_text.lower() in ("пустое сообщение", "none", "null"):
        clean_text = ""

    if attachment and attachment.get("link"):
        att_type = (attachment.get("type") or "").lower()
        if att_type in ("audio", "voice"):
            label = "[Голосовое сообщение]"
        elif att_type in ("video", "round_video"):
            label = "[Видеосообщение / Кругляшек]"
        elif att_type in ("picture", "image", "photo"):
            label = "[Фотография]"
        else:
            fname = attachment.get("file_name") or "файл"
            label = f"[Вложение: {fname}]"

        if clean_text:
            return f"{label} {clean_text}"
        return label

    return clean_text


def extract_webhook_message(body_bytes: bytes, content_type: str) -> Optional[Dict[str, Any]]:
    """
    Извлекает данные входящего сообщения из тела вебхука amoCRM.
    Поддерживает как JSON, так и application/x-www-form-urlencoded,
    включая медиавложения (голосовые, кругляшки, видео, фото).
    """
    # 1. Попытка распарсить как JSON
    if "application/json" in content_type:
        try:
            data = json.loads(body_bytes.decode("utf-8"))
            if "message" in data and isinstance(data["message"], dict):
                add_list = data["message"].get("add")
                if isinstance(add_list, list) and len(add_list) > 0:
                    msg = add_list[0]
                    attachment = None
                    att_raw = msg.get("attachment") or msg.get("media")
                    if isinstance(att_raw, list) and len(att_raw) > 0:
                        att_raw = att_raw[0]
                    if isinstance(att_raw, dict) and (att_raw.get("link") or att_raw.get("url") or att_raw.get("file_url")):
                        attachment = {
                            "type": att_raw.get("type", "file"),
                            "link": att_raw.get("link") or att_raw.get("url") or att_raw.get("file_url") or "",
                            "file_name": att_raw.get("file_name") or att_raw.get("name") or ""
                        }
                    elif isinstance(att_raw, str) and att_raw.startswith("http"):
                        attachment = {
                            "type": "file",
                            "link": att_raw,
                            "file_name": att_raw.split("/")[-1]
                        }

                    raw_text = str(msg.get("text", "") or "")
                    formatted_text = _format_message_text(raw_text, attachment)

                    return {
                        "id": str(msg.get("id", "")),
                        "entity_id": str(msg.get("entity_id") or msg.get("lead_id") or ""),
                        "author_type": msg.get("author", {}).get("type", ""),
                        "text": formatted_text,
                        "attachment": attachment,
                        "created_at": float(msg.get("created_at") or time.time())
                    }
        except Exception as e:
            logger.debug(f"Не удалось распарсить вебхук как JSON: {e}")

    # 2. Попытка распарсить как form-urlencoded (стандартный системный вебхук amoCRM)
    try:
        raw_str = body_bytes.decode("utf-8", errors="ignore")
        parsed = parse_qs(raw_str)

        # Проверяем ключи message[add][0][...]
        msg_id_list = parsed.get("message[add][0][id]")
        if msg_id_list:
            msg_id = msg_id_list[0]
            entity_id = (
                parsed.get("message[add][0][entity_id]") or
                parsed.get("message[add][0][chat_id]") or
                [""]
            )[0]
            author_type = (parsed.get("message[add][0][author][type]") or [""])[0]
            raw_text = (parsed.get("message[add][0][text]") or [""])[0]
            created_at_raw = (parsed.get("message[add][0][created_at]") or [str(int(time.time()))])[0]

            # Извлечение вложения
            att_link = (
                parsed.get("message[add][0][attachment][link]") or
                parsed.get("message[add][0][attachment][url]") or
                parsed.get("message[add][0][attachment][0][link]") or
                parsed.get("message[add][0][media]") or
                [""]
            )[0]
            att_type = (
                parsed.get("message[add][0][attachment][type]") or
                parsed.get("message[add][0][attachment][0][type]") or
                ["file"]
            )[0]
            att_name = (
                parsed.get("message[add][0][attachment][file_name]") or
                parsed.get("message[add][0][attachment][name]") or
                parsed.get("message[add][0][attachment][0][file_name]") or
                [""]
            )[0]

            attachment = None
            if att_link:
                attachment = {
                    "type": str(att_type),
                    "link": str(att_link),
                    "file_name": str(att_name)
                }

            formatted_text = _format_message_text(raw_text, attachment)

            return {
                "id": str(msg_id),
                "entity_id": str(entity_id),
                "author_type": str(author_type),
                "text": formatted_text,
                "attachment": attachment,
                "created_at": float(created_at_raw)
            }
    except Exception as e:
        logger.error(f"Ошибка парсинга form-urlencoded вебхука: {e}")

    return None


@router.post("/webhook/salesbot")
@router.get("/webhook/salesbot")
async def handle_salesbot_legacy_webhook(request: Request):
    """
    Эндпоинт для совместимости со сценариями Salesbot,
    в которых остался шаг вызова /webhook/salesbot.
    Мгновенно возвращает 200 OK.
    """
    try:
        body = await request.body()
        logger.info(f"Получен вызов /webhook/salesbot от Salesbot ({len(body)} байт)")
    except Exception:
        pass
    return {"status": "ok", "message": "salesbot webhook received"}


@router.post("/webhook/{account_uuid}")
async def handle_amocrm_webhook(account_uuid: uuid.UUID, request: Request):
    """
    Публичный приёмник вебхуков amoCRM (событие add_message).
    Гарантированный быстрый ответ 200 OK (<100мс).
    """
    content_type = request.headers.get("content-type", "")
    body_bytes = await request.body()

    msg = extract_webhook_message(body_bytes, content_type)
    if not msg or not msg["id"]:
        # amoCRM может отправлять проверочный ping или неподдерживаемое событие
        return {"status": "ignored", "reason": "no_valid_message_payload"}

    # Фильтр 1: Отсекаем исходящие сообщения бота и операторов
    if msg["author_type"] != "external":
        logger.debug(f"Игнорируем сообщение {msg['id']}: author_type='{msg['author_type']}' != 'external'")
        return {"status": "ignored", "reason": "not_external"}

    # Фильтр 2: Отсекаем устаревшие ретраи (> 90 секунд)
    now = time.time()
    if (now - msg["created_at"]) > 90:
        logger.info(f"Игнорируем устаревшее сообщение {msg['id']}: возраст {now - msg['created_at']:.1f} сек > 90")
        return {"status": "ignored", "reason": "too_old"}

    # Фильтр 3: Дедупликация в Redis через SETNX (TTL 10 минут)
    is_duplicate = await debounce_service.is_duplicate_message(str(account_uuid), msg["id"])
    if is_duplicate:
        logger.debug(f"Дубликат вебхука для сообщения {msg['id']} отброшен.")
        return {"status": "duplicate"}

    lead_id = msg["entity_id"]
    if not lead_id:
        logger.warning(f"Сообщение {msg['id']} не содержит entity_id (lead_id).")
        return {"status": "ignored", "reason": "missing_entity_id"}

    # Фильтр 3.5: Проверка, активен ли аккаунт (быстрый Redis-кэш)
    r_redis = await debounce_service.get_redis()
    if await r_redis.exists(f"acc_disabled:{account_uuid}"):
        logger.info(f"Аккаунт {account_uuid} остановлен (Стоп). Вебхук проигнорирован.")
        return {"status": "ignored", "reason": "account_disabled"}

    # Фильтр 4: Авто-верификация аккаунта при первом входящем сообщении
    # Выполняется асинхронно без блокировки ответа
    async def _verify_account_if_needed():
        try:
            async with AsyncSessionLocal() as session:
                acc_stmt = select(Account).where(Account.id == account_uuid)
                acc = (await session.execute(acc_stmt)).scalar_one_or_none()
                if acc and not acc.webhook_verified:
                    acc.webhook_verified = True
                    acc.status = AccountStatus.VERIFIED
                    await session.commit()
                    logger.info(f"Аккаунт {account_uuid} успешно верифицирован первым входящим сообщением!")
        except Exception as err:
            logger.error(f"Ошибка авто-верификации аккаунта {account_uuid}: {err}")

    # Запускаем проверку верификации в фоне
    import asyncio
    asyncio.create_task(_verify_account_if_needed())

    # Фильтр 5: Положить сообщение в буфер дебаунса
    await debounce_service.add_message_to_buffer(
        str(account_uuid),
        lead_id,
        msg["text"],
        attachment=msg.get("attachment")
    )

    # Запустить таймер дебаунса (2.5 сек)
    debounce_service.schedule_debounce(
        account_id=str(account_uuid),
        lead_id=lead_id,
        callback=delivery_service.process_lead_after_debounce,
        delay=2.5
    )

    logger.info(f"Сообщение {msg['id']} лида {lead_id} принято в обработку (дебаунс 2.5с)")
    return {"status": "ok"}

