import asyncio
import logging
import uuid
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.auth import verify_admin_key
from app.core.database import AsyncSessionLocal
from app.core.security import encrypt_token, decrypt_token
from app.models.account import Account, AccountStatus, Pipeline, FieldMapping, AIConfig, DEFAULT_COMMENT_PROMPT
from app.services.amocrm_client import amocrm_client, AmoCRMAuthOrBillingError
from app.services.debounce_service import debounce_service

logger = logging.getLogger("AccountsRouter")

def _default_enabled_stages(stages: list, current_enabled: Optional[list]) -> List[int]:
    valid_ids = [int(s["id"]) for s in (stages or []) if isinstance(s, dict) and s.get("id")]
    if current_enabled is None:
        return valid_ids[:4]
    valid_set = set(valid_ids)
    return [int(sid) for sid in current_enabled if int(sid) in valid_set]

def _norm_entity(val) -> str:
    return val if isinstance(val, str) and val in ("lead", "contact") else "lead"

router = APIRouter(prefix="/accounts", tags=["Accounts & Onboarding"], dependencies=[Depends(verify_admin_key)])


# --- Pydantic Schemas ---

class CreateAccountRequest(BaseModel):
    subdomain: str = Field(..., description="Поддомен в amoCRM (например, 'company' для company.amocrm.ru)")
    token: str = Field(..., description="Долгосрочный токен доступа amoCRM")
    name: Optional[str] = Field(None, description="Название аккаунта/компании")


class LinkBotRequest(BaseModel):
    bot_id: int = Field(..., description="ID выбранного Salesbot")


class TestConnectionRequest(BaseModel):
    lead_id: Optional[int] = Field(None, description="ID тестовой сделки в amoCRM (если не передан, берется последняя)")


class UpdatePipelinesRequest(BaseModel):
    enabled_amo_pipeline_ids: List[int] = Field(..., description="Список ID воронок amoCRM, в которых активен ИИ")
    enabled_stages_by_pipeline: Optional[Dict[str, List[int]]] = Field(
        None, description="Карта {amo_pipeline_id: [status_id, ...]} разрешённых этапов для ответов ИИ"
    )


class FieldMappingUpdate(BaseModel):
    amo_field_id: int
    entity_type: Optional[str] = "lead"
    is_enabled: bool
    ai_hint: Optional[str] = None
    overwrite_if_filled: bool = False


class UpdateFieldsRequest(BaseModel):
    fields: List[FieldMappingUpdate]


class UpdateAIConfigRequest(BaseModel):
    communicator_prompt: Optional[str] = None
    communicator_model: Optional[str] = None
    fallback_communicator_model: Optional[str] = None
    extractor_model: Optional[str] = None
    fallback_extractor_model: Optional[str] = None
    temperature: Optional[float] = None
    handover_after_stuck: Optional[int] = None
    knowledge_base: Optional[str] = None
    knowledge_mode: Optional[str] = None
    comment_prompt: Optional[str] = None
    direct_link: Optional[str] = None


# --- Endpoints ---

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_or_connect_account(payload: CreateAccountRequest):
    """
    Шаг 1: Подключение аккаунта.
    1. Валидация токена через amoCRM API
    2. Создание/поиск скрытого поля 'AI: Ответ ассистента'
    3. Попытка автоматической регистрации глобального вебхука
    4. Сохранение аккаунта и вывод инструкций для Salesbot
    """
    raw_sub = payload.subdomain.strip().lower()
    if raw_sub.startswith("https://"):
        raw_sub = raw_sub[8:]
    elif raw_sub.startswith("http://"):
        raw_sub = raw_sub[7:]
    raw_sub = raw_sub.rstrip("/")
    if "." in raw_sub:
        allowed_domains = (".amocrm.ru", ".amocrm.com", ".kommo.com")
        if not any(raw_sub.endswith(d) for d in allowed_domains):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Поддерживаются только официальные домены amoCRM (.amocrm.ru, .amocrm.com) и Kommo (.kommo.com)"
            )
        if raw_sub.endswith(".amocrm.ru"):
            subdomain = raw_sub[:-10]
        else:
            subdomain = raw_sub
    else:
        subdomain = raw_sub

    token = payload.token.strip()

    # 1. Валидация токена
    token_check = await amocrm_client.validate_token(subdomain, token)
    if not token_check.get("is_valid"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=token_check.get("error", "Неверный поддомен или недействительный токен amoCRM")
        )

    amo_account_id = token_check.get("account_id")
    account_name = payload.name or token_check.get("account_name") or token_check.get("name") or subdomain

    # 2. Создание/проверка поля 'AI: Ответ ассистента'
    try:
        field_id = await amocrm_client.ensure_reply_field(subdomain, token)
    except AmoCRMAuthOrBillingError as auth_err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=auth_err.detail)
    if not field_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось создать или получить скрытое поле сделки 'AI: Ответ ассистента' в amoCRM"
        )

    # 3. Сохраняем в БД в короткой сессии (IMPORTANT-11, IMPORTANT-12)
    encrypted_token = encrypt_token(token)
    account_id = None
    account_status_val = None
    auto_registered = False

    async with AsyncSessionLocal() as session:
        try:
            acc_stmt = select(Account).where(Account.subdomain == subdomain)
            account = (await session.execute(acc_stmt)).scalar_one_or_none()

            if not account:
                account = Account(
                    subdomain=subdomain,
                    name=account_name,
                    amo_account_id=amo_account_id,
                    encrypted_token=encrypted_token,
                    ai_reply_field_id=field_id,
                    status=AccountStatus.FIELD_CREATED
                )
                session.add(account)
                await session.flush()
            else:
                account.name = account_name
                account.amo_account_id = amo_account_id
                account.encrypted_token = encrypted_token
                account.ai_reply_field_id = field_id
                account.last_error = None
                account.is_active = True
                if account.status == AccountStatus.ERROR:
                    if account.webhook_verified:
                        account.status = AccountStatus.VERIFIED
                    elif account.bot_id:
                        account.status = AccountStatus.BOT_LINKED
                    else:
                        account.status = AccountStatus.FIELD_CREATED

            # Создаем AIConfig по умолчанию, если нет
            cfg_stmt = select(AIConfig).where(AIConfig.account_id == account.id)
            ai_cfg = (await session.execute(cfg_stmt)).scalar_one_or_none()
            if not ai_cfg:
                ai_cfg = AIConfig(account_id=account.id)
                session.add(ai_cfg)

            await session.commit()
            account_id = account.id
            account_status_val = account.status.value
        except IntegrityError:
            await session.rollback()
            # Гонка создания аккаунта с одинаковым subdomain: обновляем реквизиты под блокировкой
            acc_stmt = select(Account).where(Account.subdomain == subdomain).with_for_update()
            account = (await session.execute(acc_stmt)).scalar_one_or_none()
            if not account:
                raise HTTPException(status_code=409, detail="Конфликт создания аккаунта: повторите попытку.")
            account.name = account_name
            account.amo_account_id = amo_account_id
            account.encrypted_token = encrypted_token
            account.ai_reply_field_id = field_id
            account.last_error = None
            account.is_active = True
            if account.status == AccountStatus.ERROR:
                if account.webhook_verified:
                    account.status = AccountStatus.VERIFIED
                elif account.bot_id:
                    account.status = AccountStatus.BOT_LINKED
                else:
                    account.status = AccountStatus.FIELD_CREATED
            cfg_stmt = select(AIConfig).where(AIConfig.account_id == account.id)
            ai_cfg = (await session.execute(cfg_stmt)).scalar_one_or_none()
            if not ai_cfg:
                session.add(AIConfig(account_id=account.id))
            await session.commit()
            account_id = account.id
            account_status_val = account.status.value

    try:
        r = await debounce_service.get_redis()
        await r.delete(f"acc_disabled:{account_id}")
    except Exception:
        pass

    account_uuid_str = str(account_id)
    webhook_dest_url = f"{settings.BASE_URL}/webhook/{account_uuid_str}"

    # 4. Автоматическая регистрация вебхука (внешний HTTP-вызов БЕЗ удержания транзакции БД)
    try:
        wh_id = await amocrm_client.try_register_webhook(subdomain, token, webhook_dest_url)
    except AmoCRMAuthOrBillingError:
        wh_id = None
    if wh_id:
        auto_registered = True
        async with AsyncSessionLocal() as session:
            acc_stmt = select(Account).where(Account.id == account_id)
            acc_obj = (await session.execute(acc_stmt)).scalar_one_or_none()
            if acc_obj:
                acc_obj.webhook_id = wh_id
                acc_obj.webhook_auto_registered = True
                if acc_obj.webhook_verified:
                    acc_obj.status = AccountStatus.VERIFIED
                elif acc_obj.bot_id:
                    acc_obj.status = AccountStatus.BOT_LINKED
                else:
                    acc_obj.status = AccountStatus.AWAITING_MANUAL_BOT
                account_status_val = acc_obj.status.value
                await session.commit()

    # Очищаем флаг отключения в Redis
    try:
        r = await debounce_service.get_redis()
        await r.delete(f"acc_disabled:{account_id}")
    except Exception:
        pass

    salesbot_tag = f"{{{{lead.cf.{field_id}}}}}"

    return {
        "account_id": account_uuid_str,
        "subdomain": subdomain,
        "amo_account_id": amo_account_id,
        "status": account_status_val,
        "ai_reply_field_id": field_id,
        "webhook_url": webhook_dest_url,
        "webhook_auto_registered": auto_registered,
        "salesbot_tag": salesbot_tag,
        "instructions": {
            "step": "Создайте в конструкторе Salesbot сценарий из двух блоков: [Старт] -> [Отправить сообщение: {{lead.cf." + str(field_id) + "}}]",
            "rule_1": "НЕ добавляйте никаких вебхук-шагов в Salesbot.",
            "rule_2": "НЕ ставьте триггер автозапуска бота в Воронке. Бот вызывается исключительно через наш бэкенд.",
            "webhook_manual": f"Если авторегистрация не удалась, добавьте этот URL в Настройки -> Webhooks (событие add_message): {webhook_dest_url}"
        }
    }


@router.get("")
async def list_accounts():
    """Список всех подключенных аккаунтов"""
    async with AsyncSessionLocal() as session:
        stmt = select(Account).order_by(Account.created_at.desc())
        accounts = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": str(a.id),
                "name": a.name,
                "subdomain": a.subdomain,
                "amo_account_id": a.amo_account_id,
                "status": a.status.value,
                "is_active": a.is_active,
                "last_error": a.last_error,
                "ai_reply_field_id": a.ai_reply_field_id,
                "bot_id": a.bot_id,
                "webhook_verified": a.webhook_verified,
                "created_at": a.created_at.isoformat()
            }
            for a in accounts
        ]


@router.get("/{account_id}")
async def get_account_detail(account_id: uuid.UUID):
    """Детальная информация об аккаунте"""
    async with AsyncSessionLocal() as session:
        stmt = (
            select(Account)
            .options(selectinload(Account.ai_config))
            .where(Account.id == account_id)
        )
        account = (await session.execute(stmt)).scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        return {
            "id": str(account.id),
            "name": account.name,
            "subdomain": account.subdomain,
            "amo_account_id": account.amo_account_id,
            "status": account.status.value,
            "is_active": account.is_active,
            "last_error": account.last_error,
            "ai_reply_field_id": account.ai_reply_field_id,
            "bot_id": account.bot_id,
            "webhook_auto_registered": account.webhook_auto_registered,
            "webhook_verified": account.webhook_verified,
            "webhook_url": f"{settings.BASE_URL}/webhook/{account.id}",
            "created_at": account.created_at.isoformat(),
            "ai_config": {
                "communicator_prompt": account.ai_config.communicator_prompt if account.ai_config else None,
                "communicator_model": account.ai_config.communicator_model if account.ai_config else None,
                "fallback_communicator_model": account.ai_config.fallback_communicator_model if account.ai_config else "gemini-2.5-flash",
                "extractor_model": account.ai_config.extractor_model if account.ai_config else None,
                "fallback_extractor_model": account.ai_config.fallback_extractor_model if account.ai_config else "gemini-2.5-flash",
                "temperature": float(account.ai_config.temperature) if account.ai_config else 0.4,
                "handover_after_stuck": account.ai_config.handover_after_stuck if account.ai_config else 4,
                "comment_prompt": (account.ai_config.comment_prompt or DEFAULT_COMMENT_PROMPT) if account.ai_config else DEFAULT_COMMENT_PROMPT,
                "direct_link": account.ai_config.direct_link if account.ai_config else None
            } if account.ai_config else None
        }


@router.post("/{account_id}/toggle-active")
async def toggle_account_active(account_id: uuid.UUID):
    """
    Переключение активности аккаунта (Старт / Стоп).
    При отключении ИИ перестает отвечать на сообщения данного аккаунта.
    При включении ("Старт") проверяется валидность токена и доступность amoCRM.
    """
    # 1. Читаем статус и реквизиты аккаунта в короткой сессии
    async with AsyncSessionLocal() as session:
        stmt = select(Account).where(Account.id == account_id)
        account = (await session.execute(stmt)).scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")
        subdomain = account.subdomain
        encrypted_token = account.encrypted_token
        target_active = not account.is_active

    if target_active:
        # Внешний HTTP-вызов БЕЗ удержания транзакции БД
        try:
            token = decrypt_token(encrypted_token)
            token_check = await amocrm_client.validate_token(subdomain, token)
        except AmoCRMAuthOrBillingError as auth_err:
            token_check = {"is_valid": False, "error": auth_err.detail}

        async with AsyncSessionLocal() as session:
            stmt = select(Account).where(Account.id == account_id)
            account = (await session.execute(stmt)).scalar_one_or_none()
            if not account:
                raise HTTPException(status_code=404, detail="Аккаунт не найден")

            if not token_check.get("is_valid"):
                err_msg = token_check.get("error", "Ошибка авторизации или подписки amoCRM")
                account.is_active = False
                account.status = AccountStatus.ERROR
                account.last_error = err_msg
                await session.commit()

                try:
                    r = await debounce_service.get_redis()
                    await r.set(f"acc_disabled:{account.id}", "1")
                except Exception:
                    pass

                raise HTTPException(status_code=400, detail=err_msg)

            account.is_active = True
            account.last_error = None
            if account.status == AccountStatus.ERROR:
                if account.webhook_verified:
                    account.status = AccountStatus.VERIFIED
                elif account.bot_id:
                    account.status = AccountStatus.BOT_LINKED
                else:
                    account.status = AccountStatus.AWAITING_MANUAL_BOT

            await session.commit()
            acc_status = account.status.value

        try:
            r = await debounce_service.get_redis()
            await r.delete(f"acc_disabled:{account_id}")
        except Exception as e:
            logger.warning(f"Не удалось удалить Redis-флаг активности аккаунта {account_id}: {e}")

        logger.info(f"Аккаунт {subdomain} запущен: is_active=True")
        return {
            "success": True,
            "is_active": True,
            "status": acc_status,
            "last_error": None,
            "message": "Аккаунт запущен (ИИ активен)"
        }
    else:
        async with AsyncSessionLocal() as session:
            stmt = select(Account).where(Account.id == account_id)
            account = (await session.execute(stmt)).scalar_one_or_none()
            if not account:
                raise HTTPException(status_code=404, detail="Аккаунт не найден")

            account.is_active = False
            await session.commit()
            acc_status = account.status.value
            acc_last_error = account.last_error

        try:
            r = await debounce_service.get_redis()
            await r.set(f"acc_disabled:{account_id}", "1")
        except Exception as e:
            logger.warning(f"Не удалось установить Redis-флаг активности аккаунта {account_id}: {e}")

        logger.info(f"Аккаунт {subdomain} остановлен: is_active=False")
        return {
            "success": True,
            "is_active": False,
            "status": acc_status,
            "last_error": acc_last_error,
            "message": "Аккаунт остановлен (ИИ на паузе)"
        }


@router.get("/{account_id}/status")
async def get_account_status(account_id: uuid.UUID):
    """Легковесный поллинг статуса онбординга"""
    async with AsyncSessionLocal() as session:
        stmt = select(Account.status, Account.webhook_verified, Account.bot_id).where(Account.id == account_id)
        res = (await session.execute(stmt)).first()
        if not res:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        acc_status, wh_verified, bot_id = res
        return {
            "status": acc_status.value,
            "webhook_verified": wh_verified,
            "bot_linked": bot_id is not None
        }


@router.get("/{account_id}/bots")
async def list_amocrm_bots(account_id: uuid.UUID):
    """Получение списка Salesbot из amoCRM для выпадающего меню"""
    async with AsyncSessionLocal() as session:
        stmt = select(Account).where(Account.id == account_id)
        account = (await session.execute(stmt)).scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")
        subdomain = account.subdomain
        encrypted_token = account.encrypted_token

    # Внешний HTTP-вызов БЕЗ удержания транзакции БД
    try:
        token = decrypt_token(encrypted_token)
        bots = await amocrm_client.list_bots(subdomain, token)
        return [{"id": b["id"], "name": b["name"]} for b in bots]
    except AmoCRMAuthOrBillingError as auth_err:
        async with AsyncSessionLocal() as session:
            stmt = select(Account).where(Account.id == account_id)
            account = (await session.execute(stmt)).scalar_one_or_none()
            if account:
                account.status = AccountStatus.ERROR
                account.last_error = auth_err.detail
                account.is_active = False
                await session.commit()
        try:
            r = await debounce_service.get_redis()
            await r.set(f"acc_disabled:{account_id}", "1")
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=auth_err.detail)


@router.post("/{account_id}/link-bot")
async def link_salesbot(account_id: uuid.UUID, payload: LinkBotRequest):
    """Шаг 3: Привязка ID выбранного Salesbot"""
    async with AsyncSessionLocal() as session:
        stmt = select(Account).where(Account.id == account_id)
        account = (await session.execute(stmt)).scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        account.bot_id = payload.bot_id
        if account.status in (AccountStatus.AWAITING_MANUAL_BOT, AccountStatus.FIELD_CREATED):
            account.status = AccountStatus.BOT_LINKED
        await session.commit()

        return {
            "success": True,
            "bot_id": account.bot_id,
            "status": account.status.value
        }


@router.post("/{account_id}/test-connection")
async def test_bot_connection(account_id: uuid.UUID, payload: Optional[TestConnectionRequest] = None):
    """
    Кнопка 'Проверить связку бота':
    Выполняет запись в скрытое поле сделки и запускает Salesbot (POST /api/v4/bots/{id}/run).
    Ожидает статус 202 Accepted.
    """
    # 1. Читаем данные аккаунта в короткой сессии БД
    async with AsyncSessionLocal() as session:
        stmt = select(Account).options(selectinload(Account.pipelines)).where(Account.id == account_id)
        account = (await session.execute(stmt)).scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        if not account.bot_id:
            raise HTTPException(status_code=400, detail="Salesbot еще не привязан к аккаунту (bot_id отсутствует)")
        if not account.ai_reply_field_id:
            raise HTTPException(status_code=400, detail="Скрытое поле ai_reply_field_id отсутствует")

        encrypted_token = account.encrypted_token
        subdomain = account.subdomain
        bot_id = account.bot_id
        ai_reply_field_id = account.ai_reply_field_id
        enabled_pipes = [(p.amo_pipeline_id, p.name) for p in account.pipelines if p.is_enabled]

    lead_id = payload.lead_id if payload and payload.lead_id else None
    lead_pipeline_name = ""

    # 2. Внешние HTTP-вызовы amoCRM БЕЗ сессии БД
    try:
        token = decrypt_token(encrypted_token)
        if not lead_id:
            lead_obj = None
            if enabled_pipes:
                pipe_tasks = [
                    amocrm_client.get_latest_lead(subdomain, token, pipeline_id=ep_id)
                    for ep_id, _ in enabled_pipes
                ]
                results = await asyncio.gather(*pipe_tasks, return_exceptions=True)
                for (ep_id, ep_name), res in zip(enabled_pipes, results):
                    if isinstance(res, dict) and res.get("id"):
                        lead_obj = res
                        lead_pipeline_name = ep_name
                        break

            if not lead_obj:
                lead_obj = await amocrm_client.get_latest_lead(subdomain, token)

            if lead_obj:
                lead_id = lead_obj.get("id")

        if not lead_id:
            raise HTTPException(
                status_code=400,
                detail="В amoCRM не найдено ни одной сделки для проверки. Создайте сделку или укажите lead_id явно."
            )

        test_msg = "Тестовое сообщение от ИИ: связка работает корректно!"
        patch_ok = await amocrm_client.patch_lead_field(
            subdomain=subdomain,
            access_token=token,
            lead_id=lead_id,
            field_id=ai_reply_field_id,
            value=test_msg
        )
        if not patch_ok:
            raise HTTPException(status_code=500, detail=f"Не удалось записать тестовое значение в поле {ai_reply_field_id} сделки {lead_id}")

        bot_ok = await amocrm_client.run_salesbot(
            subdomain=subdomain,
            access_token=token,
            bot_id=bot_id,
            entity_id=lead_id
        )

        if not bot_ok:
            raise HTTPException(
                status_code=500,
                detail=f"Не удалось запустить Salesbot {bot_id} (API вернул статус, отличный от 202 Accepted)"
            )
    except AmoCRMAuthOrBillingError as auth_err:
        async with AsyncSessionLocal() as session:
            stmt = select(Account).where(Account.id == account_id)
            account = (await session.execute(stmt)).scalar_one_or_none()
            if account:
                account.status = AccountStatus.ERROR
                account.last_error = auth_err.detail
                account.is_active = False
                await session.commit()
        try:
            r = await debounce_service.get_redis()
            await r.set(f"acc_disabled:{account_id}", "1")
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=auth_err.detail)

    pipeline_info = f" (воронка «{lead_pipeline_name}»)" if lead_pipeline_name else ""
    return {
        "success": True,
        "tested_lead_id": lead_id,
        "bot_id": bot_id,
        "message": f"Бот #{bot_id} успешно запущен на сделке #{lead_id}{pipeline_info} (202 Accepted)! Проверьте чат сделки в amoCRM."
    }


@router.get("/{account_id}/pipelines")
async def get_account_pipelines(account_id: uuid.UUID):
    """Синхронизация и получение списка воронок amoCRM"""
    # Сессия 1: Чтение реквизитов аккаунта в короткой сессии
    async with AsyncSessionLocal() as session:
        stmt = select(Account).where(Account.id == account_id)
        account = (await session.execute(stmt)).scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")
        subdomain = account.subdomain
        encrypted_token = account.encrypted_token

    # Внешний HTTP-вызов БЕЗ удержания транзакции БД
    try:
        token = decrypt_token(encrypted_token)
        amo_pipelines = await amocrm_client.list_pipelines(subdomain, token)
    except AmoCRMAuthOrBillingError as auth_err:
        async with AsyncSessionLocal() as session:
            stmt = select(Account).where(Account.id == account_id)
            account = (await session.execute(stmt)).scalar_one_or_none()
            if account:
                account.status = AccountStatus.ERROR
                account.last_error = auth_err.detail
                account.is_active = False
                await session.commit()
        try:
            r = await debounce_service.get_redis()
            await r.set(f"acc_disabled:{account_id}", "1")
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=auth_err.detail)

    # Сессия 2: Синхронизация с БД в короткой сессии (с row-level блокировкой от гонок INSERT)
    async with AsyncSessionLocal() as session:
        stmt = select(Account).options(selectinload(Account.pipelines)).where(Account.id == account_id).with_for_update()
        account = (await session.execute(stmt)).scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")
        existing_map = {p.amo_pipeline_id: p for p in account.pipelines}
        amo_pipeline_ids = set()
        for ap in amo_pipelines:
            ap_id = ap["id"]
            ap_stages = ap.get("statuses") or []
            amo_pipeline_ids.add(ap_id)
            if ap_id in existing_map:
                existing_map[ap_id].name = ap["name"]
                existing_map[ap_id].stages_json = ap_stages
                cur_en = getattr(existing_map[ap_id], "enabled_stage_ids", None)
                existing_map[ap_id].enabled_stage_ids = _default_enabled_stages(
                    ap_stages, cur_en if isinstance(cur_en, list) else None
                )
            else:
                new_p = Pipeline(
                    account_id=account.id,
                    amo_pipeline_id=ap_id,
                    name=ap["name"],
                    is_enabled=False,
                    stages_json=ap_stages,
                    enabled_stage_ids=_default_enabled_stages(ap_stages, None),
                )
                session.add(new_p)

        # Если воронка удалена в amoCRM, отключаем её
        for p in account.pipelines:
            if p.amo_pipeline_id not in amo_pipeline_ids and p.is_enabled:
                logger.warning(f"Воронка {p.name} (ID: {p.amo_pipeline_id}) удалена в amoCRM. Отключаем.")
                p.is_enabled = False

        await session.commit()

        # Загружаем обновленный список
        p_stmt = select(Pipeline).where(Pipeline.account_id == account_id).order_by(Pipeline.amo_pipeline_id.asc())
        all_pipelines = (await session.execute(p_stmt)).scalars().all()

        return [
            {
                "id": str(p.id),
                "amo_pipeline_id": p.amo_pipeline_id,
                "name": p.name,
                "is_enabled": p.is_enabled,
                "stages": (p.stages_json if isinstance(getattr(p, "stages_json", None), list) else []),
                "enabled_stage_ids": (
                    p.enabled_stage_ids
                    if isinstance(getattr(p, "enabled_stage_ids", None), list)
                    else _default_enabled_stages(
                        p.stages_json if isinstance(getattr(p, "stages_json", None), list) else [], None
                    )
                ),
                "is_deleted_in_amo": p.amo_pipeline_id not in amo_pipeline_ids
            }
            for p in all_pipelines
        ]


@router.patch("/{account_id}/pipelines")
async def update_account_pipelines(account_id: uuid.UUID, payload: UpdatePipelinesRequest):
    """Включение/выключение воронок для работы ИИ"""
    async with AsyncSessionLocal() as session:
        stmt = select(Pipeline).where(Pipeline.account_id == account_id)
        pipelines = (await session.execute(stmt)).scalars().all()
        if not pipelines:
            raise HTTPException(status_code=404, detail="Воронки не найдены. Сначала выполните GET /pipelines")

        enabled_set = set(payload.enabled_amo_pipeline_ids)
        stages_map = payload.enabled_stages_by_pipeline or {}
        for p in pipelines:
            p.is_enabled = (p.amo_pipeline_id in enabled_set)
            pid_str = str(p.amo_pipeline_id)
            if pid_str in stages_map and isinstance(stages_map[pid_str], list):
                p.enabled_stage_ids = [int(sid) for sid in stages_map[pid_str]]
            elif p.is_enabled and not isinstance(getattr(p, "enabled_stage_ids", None), list):
                p.enabled_stage_ids = _default_enabled_stages(
                    p.stages_json if isinstance(getattr(p, "stages_json", None), list) else [], None
                )

        acc_stmt = select(Account).where(Account.id == account_id).with_for_update()
        account = (await session.execute(acc_stmt)).scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")
        if account.status not in (AccountStatus.VERIFIED, AccountStatus.CONFIGURED):
            account.status = AccountStatus.CONFIGURED

        await session.commit()

        return {
            "success": True,
            "enabled_pipeline_ids": list(enabled_set),
            "account_status": account.status.value
        }


@router.get("/{account_id}/fields")
async def get_account_fields(account_id: uuid.UUID):
    """Синхронизация и получение списка кастомных полей сделок и контактов"""
    # Сессия 1: Чтение реквизитов аккаунта в короткой сессии
    async with AsyncSessionLocal() as session:
        stmt = select(Account).where(Account.id == account_id)
        account = (await session.execute(stmt)).scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")
        subdomain = account.subdomain
        encrypted_token = account.encrypted_token

    # Внешние HTTP-вызовы БЕЗ удержания транзакции БД
    try:
        token = decrypt_token(encrypted_token)
        lead_fields = await amocrm_client.list_custom_fields(subdomain, token)
        contact_fields = await amocrm_client.list_contact_custom_fields(subdomain, token)
    except AmoCRMAuthOrBillingError as auth_err:
        async with AsyncSessionLocal() as session:
            stmt = select(Account).where(Account.id == account_id)
            account = (await session.execute(stmt)).scalar_one_or_none()
            if account:
                account.status = AccountStatus.ERROR
                account.last_error = auth_err.detail
                account.is_active = False
                await session.commit()
        try:
            r = await debounce_service.get_redis()
            await r.set(f"acc_disabled:{account_id}", "1")
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=auth_err.detail)

    amo_fields = lead_fields + contact_fields
    amo_field_keys = {(_norm_entity(af.get("entity_type", "lead")), af["id"]) for af in amo_fields}

    # Сессия 2: Синхронизация с БД в короткой сессии (с row-level блокировкой от гонок INSERT)
    async with AsyncSessionLocal() as session:
        stmt = select(Account).options(selectinload(Account.field_mappings)).where(Account.id == account_id).with_for_update()
        account = (await session.execute(stmt)).scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        # Очищаем из FieldMapping любые поля ответа ИИ (они настраиваются в онбординге/Salesbot)
        reply_ids = {account.ai_reply_field_id} if account.ai_reply_field_id else set()
        to_remove = [
            fm for fm in account.field_mappings
            if fm.amo_field_id in reply_ids or fm.field_name in ("AI: Ответ ассистента", "Ответ ИИ")
        ]
        for fm in to_remove:
            await session.delete(fm)
            account.field_mappings.remove(fm)

        existing_map = {(_norm_entity(getattr(fm, "entity_type", None)), fm.amo_field_id): fm for fm in account.field_mappings}
        for af in amo_fields:
            af_id = af["id"]
            if af_id in reply_ids:
                continue  # Пропускаем служебное поле ответа ИИ
            if af.get("name") in ("AI: Ответ ассистента", "Ответ ИИ"):
                continue

            entity = _norm_entity(af.get("entity_type", "lead"))
            if (entity, af_id) in existing_map:
                existing_map[(entity, af_id)].field_name = af["name"]
                existing_map[(entity, af_id)].field_type = af.get("type", "text")
                existing_map[(entity, af_id)].entity_type = entity
            else:
                new_fm = FieldMapping(
                    account_id=account.id,
                    amo_field_id=af_id,
                    field_name=af["name"],
                    field_type=af.get("type", "text"),
                    entity_type=entity,
                    is_enabled=False,
                    ai_hint=f"Значение поля {af['name']}",
                    overwrite_if_filled=False
                )
                session.add(new_fm)

        # Автоматически отключаем поля, которые удалили в amoCRM
        for fm in account.field_mappings:
            fm_entity = _norm_entity(getattr(fm, "entity_type", None))
            if (fm_entity, fm.amo_field_id) not in amo_field_keys:
                if fm.is_enabled:
                    logger.warning(
                        f"Поле {fm.field_name} (ID: {fm.amo_field_id}) удалено в amoCRM. "
                        f"Отключаем маппинг для аккаунта {account.subdomain}."
                    )
                    fm.is_enabled = False

        await session.commit()

        # Возвращаем все поля Экстрактора (без служебных полей ответа ИИ)
        fm_stmt = (
            select(FieldMapping)
            .where(
                FieldMapping.account_id == account_id,
                FieldMapping.amo_field_id != account.ai_reply_field_id,
                FieldMapping.field_name.notin_(["AI: Ответ ассистента", "Ответ ИИ"])
            )
            .order_by(FieldMapping.entity_type.asc(), FieldMapping.amo_field_id.asc())
        )
        all_mappings = (await session.execute(fm_stmt)).scalars().all()

        return [
            {
                "id": str(fm.id),
                "amo_field_id": fm.amo_field_id,
                "field_name": fm.field_name,
                "field_type": fm.field_type,
                "entity_type": getattr(fm, "entity_type", "lead"),
                "is_enabled": fm.is_enabled,
                "ai_hint": fm.ai_hint,
                "overwrite_if_filled": fm.overwrite_if_filled,
                "is_deleted_in_amo": ((_norm_entity(getattr(fm, "entity_type", None))), fm.amo_field_id) not in amo_field_keys
            }
            for fm in all_mappings
        ]


@router.delete("/{account_id}/fields/{amo_field_id}")
async def delete_account_field_mapping(account_id: uuid.UUID, amo_field_id: int):
    """Удаление маппинга поля из базы данных (для удаленных полей amoCRM)"""
    async with AsyncSessionLocal() as session:
        stmt = select(FieldMapping).where(
            FieldMapping.account_id == account_id,
            FieldMapping.amo_field_id == amo_field_id
        )
        fm = (await session.execute(stmt)).scalar_one_or_none()
        if not fm:
            raise HTTPException(status_code=404, detail="Маппинг поля не найден")
        await session.delete(fm)
        await session.commit()
        return {"success": True, "message": f"Поле #{amo_field_id} удалено из списка"}


@router.patch("/{account_id}/fields")
async def update_account_fields(account_id: uuid.UUID, payload: UpdateFieldsRequest):
    """Настройка полей для Экстрактора (включение, подсказка для ИИ, перезапись)"""
    async with AsyncSessionLocal() as session:
        stmt = select(FieldMapping).where(FieldMapping.account_id == account_id)
        mappings = (await session.execute(stmt)).scalars().all()
        mapping_by_tuple = {((_norm_entity(getattr(m, "entity_type", None))), m.amo_field_id): m for m in mappings}
        mapping_by_id = {m.amo_field_id: m for m in mappings}

        for item in payload.fields:
            m = None
            norm_ent = _norm_entity(getattr(item, "entity_type", None))
            if (norm_ent, item.amo_field_id) in mapping_by_tuple:
                m = mapping_by_tuple[(norm_ent, item.amo_field_id)]
            elif item.amo_field_id in mapping_by_id:
                m = mapping_by_id[item.amo_field_id]
            if m is not None:
                m.is_enabled = item.is_enabled
                if item.ai_hint is not None:
                    m.ai_hint = item.ai_hint
                m.overwrite_if_filled = item.overwrite_if_filled

        await session.commit()
        return {"success": True, "updated_count": len(payload.fields)}


@router.get("/{account_id}/ai-config")
async def get_ai_config(account_id: uuid.UUID):
    """Получение настроек ИИ"""
    async with AsyncSessionLocal() as session:
        stmt = select(AIConfig).where(AIConfig.account_id == account_id)
        cfg = (await session.execute(stmt)).scalar_one_or_none()
        if not cfg:
            raise HTTPException(status_code=404, detail="Настройки ИИ не найдены")

        return {
            "account_id": str(cfg.account_id),
            "communicator_prompt": cfg.communicator_prompt,
            "communicator_model": cfg.communicator_model,
            "fallback_communicator_model": cfg.fallback_communicator_model or "gemini-2.5-flash",
            "extractor_model": cfg.extractor_model,
            "fallback_extractor_model": cfg.fallback_extractor_model or "gemini-2.5-flash",
            "temperature": float(cfg.temperature),
            "handover_after_stuck": cfg.handover_after_stuck,
            "knowledge_base": cfg.knowledge_base or "",
            "knowledge_mode": cfg.knowledge_mode or "plain_text",
            "gemini_cache_name": cfg.gemini_cache_name,
            "comment_prompt": cfg.comment_prompt or DEFAULT_COMMENT_PROMPT,
            "direct_link": cfg.direct_link or ""
        }


@router.patch("/{account_id}/ai-config")
async def update_ai_config(account_id: uuid.UUID, payload: UpdateAIConfigRequest):
    """Обновление настроек ИИ (системный промпт, модели, температура, база знаний, комментарии)"""
    async with AsyncSessionLocal() as session:
        acc_exists = (await session.execute(
            select(Account.id).where(Account.id == account_id).with_for_update()
        )).scalar_one_or_none()
        if not acc_exists:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        stmt = select(AIConfig).where(AIConfig.account_id == account_id)
        cfg = (await session.execute(stmt)).scalar_one_or_none()
        if not cfg:
            cfg = AIConfig(account_id=account_id)
            session.add(cfg)

        if payload.communicator_prompt is not None:
            cfg.communicator_prompt = payload.communicator_prompt
        if payload.communicator_model is not None:
            cfg.communicator_model = payload.communicator_model
        if payload.fallback_communicator_model is not None:
            cfg.fallback_communicator_model = payload.fallback_communicator_model.strip()
        if payload.extractor_model is not None:
            cfg.extractor_model = payload.extractor_model
        if payload.fallback_extractor_model is not None:
            cfg.fallback_extractor_model = payload.fallback_extractor_model.strip()
        if payload.temperature is not None:
            cfg.temperature = payload.temperature
        if payload.handover_after_stuck is not None:
            cfg.handover_after_stuck = payload.handover_after_stuck
        if payload.knowledge_base is not None:
            if cfg.knowledge_base != payload.knowledge_base:
                cfg.gemini_cache_name = None
                cfg.gemini_cache_expires_at = None
            cfg.knowledge_base = payload.knowledge_base
        if payload.knowledge_mode is not None:
            cfg.knowledge_mode = payload.knowledge_mode
        if payload.comment_prompt is not None:
            cfg.comment_prompt = payload.comment_prompt.strip() if payload.comment_prompt.strip() else None
        if payload.direct_link is not None:
            cfg.direct_link = payload.direct_link.strip() if payload.direct_link.strip() else None

        await session.commit()
        return {"success": True, "message": "Настройки ИИ успешно обновлены"}


@router.post("/{account_id}/sync")
async def sync_account_with_amocrm(account_id: uuid.UUID):
    """
    Полная синхронизация данных аккаунта с amoCRM:
    1. Проверка токена и доступности amoCRM (обновление имени компании и очистка ошибок).
    2. Проверка и пересоздание скрытого поля 'AI: Ответ ассистента', если оно удалено.
    3. Синхронизация списка воронок (актуализация названий, добавление новых, авто-отключение удаленных).
    4. Синхронизация кастомных полей сделок и контактов (актуализация типов/названий, авто-отключение удаленных).
    5. Проверка доступных Salesbot (проверка наличия привязанного бота).
    """
    # 1. Читаем реквизиты аккаунта в короткой сессии БД
    async with AsyncSessionLocal() as session:
        stmt = select(Account).where(Account.id == account_id)
        account = (await session.execute(stmt)).scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        encrypted_token = account.encrypted_token
        subdomain = account.subdomain
        account_bot_id = account.bot_id

    # 2. Внешние HTTP-вызовы amoCRM БЕЗ сессии БД
    try:
        token = decrypt_token(encrypted_token)
        token_check = await amocrm_client.validate_token(subdomain, token)
    except AmoCRMAuthOrBillingError as auth_err:
        async with AsyncSessionLocal() as session:
            stmt = select(Account).where(Account.id == account_id)
            acc = (await session.execute(stmt)).scalar_one_or_none()
            if acc:
                acc.status = AccountStatus.ERROR
                acc.last_error = auth_err.detail
                acc.is_active = False
                await session.commit()
        try:
            r = await debounce_service.get_redis()
            await r.set(f"acc_disabled:{account_id}", "1")
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=auth_err.detail)

    if not token_check.get("is_valid"):
        err_msg = token_check.get("error", "Неверный токен amoCRM")
        async with AsyncSessionLocal() as session:
            stmt = select(Account).where(Account.id == account_id)
            acc = (await session.execute(stmt)).scalar_one_or_none()
            if acc:
                acc.status = AccountStatus.ERROR
                acc.last_error = err_msg
                acc.is_active = False
                await session.commit()
        try:
            r = await debounce_service.get_redis()
            await r.set(f"acc_disabled:{account_id}", "1")
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=err_msg)

    new_account_name = token_check.get("account_name")
    new_account_id = token_check.get("account_id")

    try:
        reply_field_id = await amocrm_client.ensure_reply_field(subdomain, token)
    except AmoCRMAuthOrBillingError:
        reply_field_id = None

    pipelines_synced = False
    try:
        amo_pipelines = await amocrm_client.list_pipelines(subdomain, token)
        pipelines_synced = True
    except Exception as e:
        logger.warning(f"Ошибка получения воронок при синхронизации {subdomain}: {e}")
        amo_pipelines = []

    fields_synced = False
    try:
        lead_fields = await amocrm_client.list_custom_fields(subdomain, token)
        contact_fields = await amocrm_client.list_contact_custom_fields(subdomain, token)
        amo_fields = lead_fields + contact_fields
        fields_synced = True
    except Exception as e:
        logger.warning(f"Ошибка получения полей при синхронизации {subdomain}: {e}")
        amo_fields = []

    bot_still_exists = True
    try:
        bots = await amocrm_client.list_bots(subdomain, token)
        if account_bot_id:
            bot_ids = {b["id"] for b in bots}
            if account_bot_id not in bot_ids:
                bot_still_exists = False
                logger.warning(f"Привязанный бот #{account_bot_id} не найден в amoCRM для {subdomain}")
    except Exception as e:
        logger.warning(f"Ошибка получения ботов при синхронизации {subdomain}: {e}")
        bots = []

    # 3. Синхронизация данных в БД в короткой сессии под блокировкой строки Account
    async with AsyncSessionLocal() as session:
        stmt = (
            select(Account)
            .options(
                selectinload(Account.pipelines),
                selectinload(Account.field_mappings),
                selectinload(Account.ai_config)
            )
            .where(Account.id == account_id)
            .with_for_update()
        )
        account = (await session.execute(stmt)).scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        if new_account_name:
            account.name = new_account_name
        if new_account_id:
            account.amo_account_id = new_account_id
        account.last_error = None

        if reply_field_id:
            account.ai_reply_field_id = reply_field_id

        if pipelines_synced:
            existing_pipelines = {p.amo_pipeline_id: p for p in account.pipelines}
            amo_pipeline_ids = set()
            for ap in amo_pipelines:
                ap_id = ap["id"]
                ap_stages = ap.get("statuses") or []
                amo_pipeline_ids.add(ap_id)
                if ap_id in existing_pipelines:
                    existing_pipelines[ap_id].name = ap["name"]
                    existing_pipelines[ap_id].stages_json = ap_stages
                    cur_en = getattr(existing_pipelines[ap_id], "enabled_stage_ids", None)
                    existing_pipelines[ap_id].enabled_stage_ids = _default_enabled_stages(
                        ap_stages, cur_en if isinstance(cur_en, list) else None
                    )
                else:
                    new_p = Pipeline(
                        account_id=account.id,
                        amo_pipeline_id=ap_id,
                        name=ap["name"],
                        is_enabled=False,
                        stages_json=ap_stages,
                        enabled_stage_ids=_default_enabled_stages(ap_stages, None),
                    )
                    session.add(new_p)

            for p in account.pipelines:
                if p.amo_pipeline_id not in amo_pipeline_ids and p.is_enabled:
                    p.is_enabled = False

        deleted_fields_count = 0
        if fields_synced:
            amo_field_keys = {(_norm_entity(af.get("entity_type", "lead")), af["id"]) for af in amo_fields}

            reply_ids = {account.ai_reply_field_id} if account.ai_reply_field_id else set()
            to_remove = [
                fm for fm in account.field_mappings
                if fm.amo_field_id in reply_ids or fm.field_name in ("AI: Ответ ассистента", "Ответ ИИ")
            ]
            for fm in to_remove:
                await session.delete(fm)
                account.field_mappings.remove(fm)

            existing_fields = {(_norm_entity(getattr(fm, "entity_type", None)), fm.amo_field_id): fm for fm in account.field_mappings}
            for af in amo_fields:
                af_id = af["id"]
                if af_id in reply_ids:
                    continue
                if af.get("name") in ("AI: Ответ ассистента", "Ответ ИИ"):
                    continue

                entity = _norm_entity(af.get("entity_type", "lead"))
                if (entity, af_id) in existing_fields:
                    existing_fields[(entity, af_id)].field_name = af["name"]
                    existing_fields[(entity, af_id)].field_type = af.get("type", "text")
                    existing_fields[(entity, af_id)].entity_type = entity
                else:
                    new_fm = FieldMapping(
                        account_id=account.id,
                        amo_field_id=af_id,
                        field_name=af["name"],
                        field_type=af.get("type", "text"),
                        entity_type=entity,
                        is_enabled=False,
                        ai_hint=f"Значение поля {af['name']}",
                        overwrite_if_filled=False
                    )
                    session.add(new_fm)

            for fm in account.field_mappings:
                fm_entity = _norm_entity(getattr(fm, "entity_type", None))
                if (fm_entity, fm.amo_field_id) not in amo_field_keys:
                    if fm.is_enabled:
                        fm.is_enabled = False
                        deleted_fields_count += 1

        if account.status == AccountStatus.ERROR:
            if account.webhook_verified:
                account.status = AccountStatus.VERIFIED
            elif account.bot_id and bot_still_exists:
                account.status = AccountStatus.BOT_LINKED
            else:
                account.status = AccountStatus.AWAITING_MANUAL_BOT

        await session.commit()

        account_name_out = account.name or account.subdomain
        account_status_val = account.status.value
        account_is_active = account.is_active
        account_ai_reply_field_id = account.ai_reply_field_id
        account_bot_id_out = account.bot_id

    if account_is_active:
        try:
            r = await debounce_service.get_redis()
            await r.delete(f"acc_disabled:{account_id}")
        except Exception:
            pass

    return {
        "success": True,
        "message": f"Данные аккаунта «{account_name_out}» успешно синхронизированы с amoCRM",
        "account": {
            "id": str(account_id),
            "name": account_name_out,
            "subdomain": subdomain,
            "status": account_status_val,
            "is_active": account_is_active,
            "ai_reply_field_id": account_ai_reply_field_id,
            "bot_id": account_bot_id_out,
            "bot_exists": bot_still_exists,
            "pipelines_count": len(amo_pipelines),
            "custom_fields_count": len(amo_fields),
            "deleted_fields_disabled": deleted_fields_count
        }
    }
