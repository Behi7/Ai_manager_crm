import logging
import uuid
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.security import encrypt_token, decrypt_token
from app.models.account import Account, AccountStatus, Pipeline, FieldMapping, AIConfig
from app.services.amocrm_client import amocrm_client
from app.services.debounce_service import debounce_service

logger = logging.getLogger("AccountsRouter")

router = APIRouter(prefix="/accounts", tags=["Accounts & Onboarding"])


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


class FieldMappingUpdate(BaseModel):
    amo_field_id: int
    is_enabled: bool
    ai_hint: Optional[str] = None
    overwrite_if_filled: bool = False


class UpdateFieldsRequest(BaseModel):
    fields: List[FieldMappingUpdate]


class UpdateAIConfigRequest(BaseModel):
    communicator_prompt: Optional[str] = None
    communicator_model: Optional[str] = None
    extractor_model: Optional[str] = None
    temperature: Optional[float] = None
    handover_after_stuck: Optional[int] = None


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
    subdomain = payload.subdomain.strip().lower().replace("https://", "").replace(".amocrm.ru", "")
    token = payload.token.strip()

    # 1. Валидация токена
    token_check = await amocrm_client.validate_token(subdomain, token)
    if not token_check.get("is_valid"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=token_check.get("error", "Неверный поддомен или недействительный токен amoCRM")
        )

    amo_account_id = token_check.get("account_id")
    account_name = payload.name or token_check.get("account_name") or subdomain

    # 2. Создание/проверка поля 'AI: Ответ ассистента'
    field_id = await amocrm_client.ensure_reply_field(subdomain, token)
    if not field_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось создать или получить скрытое поле сделки 'AI: Ответ ассистента' в amoCRM"
        )

    # 3. Сохраняем в БД
    async with AsyncSessionLocal() as session:
        acc_stmt = select(Account).where(Account.subdomain == subdomain)
        account = (await session.execute(acc_stmt)).scalar_one_or_none()

        encrypted_token = encrypt_token(token)

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
            account.status = AccountStatus.FIELD_CREATED

        account_uuid_str = str(account.id)
        webhook_dest_url = f"{settings.BASE_URL}/webhook/{account_uuid_str}"

        # 4. Попытка автоматической регистрации системного вебхука
        wh_id = await amocrm_client.try_register_webhook(subdomain, token, webhook_dest_url)
        if wh_id:
            account.webhook_id = wh_id
            account.webhook_auto_registered = True
        else:
            account.webhook_auto_registered = False

        account.status = AccountStatus.AWAITING_MANUAL_BOT

        # Создаем AIConfig по умолчанию, если нет
        cfg_stmt = select(AIConfig).where(AIConfig.account_id == account.id)
        ai_cfg = (await session.execute(cfg_stmt)).scalar_one_or_none()
        if not ai_cfg:
            ai_cfg = AIConfig(account_id=account.id)
            session.add(ai_cfg)

        await session.commit()

        salesbot_tag = f"{{{{lead.cf.{field_id}}}}}"

        return {
            "account_id": account_uuid_str,
            "subdomain": subdomain,
            "amo_account_id": amo_account_id,
            "status": account.status.value,
            "ai_reply_field_id": field_id,
            "webhook_url": webhook_dest_url,
            "webhook_auto_registered": account.webhook_auto_registered,
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
            "ai_reply_field_id": account.ai_reply_field_id,
            "bot_id": account.bot_id,
            "webhook_auto_registered": account.webhook_auto_registered,
            "webhook_verified": account.webhook_verified,
            "webhook_url": f"{settings.BASE_URL}/webhook/{account.id}",
            "created_at": account.created_at.isoformat(),
            "ai_config": {
                "communicator_prompt": account.ai_config.communicator_prompt if account.ai_config else None,
                "communicator_model": account.ai_config.communicator_model if account.ai_config else None,
                "extractor_model": account.ai_config.extractor_model if account.ai_config else None,
                "temperature": float(account.ai_config.temperature) if account.ai_config else 0.4,
                "handover_after_stuck": account.ai_config.handover_after_stuck if account.ai_config else 4
            } if account.ai_config else None
        }


@router.post("/{account_id}/toggle-active")
async def toggle_account_active(account_id: uuid.UUID):
    """
    Переключение активности аккаунта (Старт / Стоп).
    При отключении ИИ перестает отвечать на сообщения данного аккаунта.
    """
    async with AsyncSessionLocal() as session:
        stmt = select(Account).where(Account.id == account_id)
        account = (await session.execute(stmt)).scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        account.is_active = not account.is_active
        await session.commit()

        # Синхронизация с быстрым кэшем в Redis
        try:
            r = await debounce_service.get_redis()
            cache_key = f"acc_disabled:{account.id}"
            if not account.is_active:
                await r.set(cache_key, "1")
            else:
                await r.delete(cache_key)
        except Exception as e:
            logger.warning(f"Не удалось обновить Redis-флаг активности аккаунта {account.id}: {e}")

        logger.info(f"Аккаунт {account.subdomain} переключен: is_active={account.is_active}")
        return {
            "success": True,
            "is_active": account.is_active,
            "status": "active" if account.is_active else "paused",
            "message": "Аккаунт запущен (ИИ активен)" if account.is_active else "Аккаунт остановлен (ИИ на паузе)"
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

        token = decrypt_token(account.encrypted_token)
        bots = await amocrm_client.list_bots(account.subdomain, token)
        return [{"id": b["id"], "name": b["name"]} for b in bots]


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
    async with AsyncSessionLocal() as session:
        stmt = select(Account).options(selectinload(Account.pipelines)).where(Account.id == account_id)
        account = (await session.execute(stmt)).scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        if not account.bot_id:
            raise HTTPException(status_code=400, detail="Salesbot еще не привязан к аккаунту (bot_id отсутствует)")
        if not account.ai_reply_field_id:
            raise HTTPException(status_code=400, detail="Скрытое поле ai_reply_field_id отсутствует")

        token = decrypt_token(account.encrypted_token)
        subdomain = account.subdomain

        lead_id = payload.lead_id if payload and payload.lead_id else None
        lead_pipeline_name = ""

        # Если ID сделки не передан, ищем сделку в одной из включенных воронок
        if not lead_id:
            enabled_pipes = [p for p in account.pipelines if p.is_enabled]
            lead_obj = None
            for ep in enabled_pipes:
                lead_obj = await amocrm_client.get_latest_lead(subdomain, token, pipeline_id=ep.amo_pipeline_id)
                if lead_obj:
                    lead_pipeline_name = ep.name
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

        # 1. Запись проверочного ответа в поле сделки
        test_msg = "Тестовое сообщение от ИИ: связка работает корректно!"
        patch_ok = await amocrm_client.patch_lead_field(
            subdomain=subdomain,
            access_token=token,
            lead_id=lead_id,
            field_id=account.ai_reply_field_id,
            value=test_msg
        )
        if not patch_ok:
            raise HTTPException(status_code=500, detail=f"Не удалось записать тестовое значение в поле {account.ai_reply_field_id} сделки {lead_id}")

        # 2. Запуск бота
        bot_ok = await amocrm_client.run_salesbot(
            subdomain=subdomain,
            access_token=token,
            bot_id=account.bot_id,
            entity_id=lead_id
        )

        if not bot_ok:
            raise HTTPException(
                status_code=500,
                detail=f"Не удалось запустить Salesbot {account.bot_id} (API вернул статус, отличный от 202 Accepted)"
            )

        pipeline_info = f" (воронка «{lead_pipeline_name}»)" if lead_pipeline_name else ""
        return {
            "success": True,
            "tested_lead_id": lead_id,
            "bot_id": account.bot_id,
            "message": f"Бот #{account.bot_id} успешно запущен на сделке #{lead_id}{pipeline_info} (202 Accepted)! Проверьте чат сделки в amoCRM."
        }


@router.get("/{account_id}/pipelines")
async def get_account_pipelines(account_id: uuid.UUID):
    """Синхронизация и получение списка воронок amoCRM"""
    async with AsyncSessionLocal() as session:
        stmt = select(Account).options(selectinload(Account.pipelines)).where(Account.id == account_id)
        account = (await session.execute(stmt)).scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        token = decrypt_token(account.encrypted_token)
        amo_pipelines = await amocrm_client.list_pipelines(account.subdomain, token)

        # Синхронизация с БД
        existing_map = {p.amo_pipeline_id: p for p in account.pipelines}
        for ap in amo_pipelines:
            ap_id = ap["id"]
            if ap_id in existing_map:
                existing_map[ap_id].name = ap["name"]
            else:
                new_p = Pipeline(
                    account_id=account.id,
                    amo_pipeline_id=ap_id,
                    name=ap["name"],
                    is_enabled=False
                )
                session.add(new_p)

        await session.commit()

        # Загружаем обновленный список
        p_stmt = select(Pipeline).where(Pipeline.account_id == account_id).order_by(Pipeline.amo_pipeline_id.asc())
        all_pipelines = (await session.execute(p_stmt)).scalars().all()

        return [
            {
                "id": str(p.id),
                "amo_pipeline_id": p.amo_pipeline_id,
                "name": p.name,
                "is_enabled": p.is_enabled
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
        for p in pipelines:
            p.is_enabled = (p.amo_pipeline_id in enabled_set)

        acc_stmt = select(Account).where(Account.id == account_id)
        account = (await session.execute(acc_stmt)).scalar_one()
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
    """Синхронизация и получение списка кастомных полей сделок"""
    async with AsyncSessionLocal() as session:
        stmt = select(Account).options(selectinload(Account.field_mappings)).where(Account.id == account_id)
        account = (await session.execute(stmt)).scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        token = decrypt_token(account.encrypted_token)
        amo_fields = await amocrm_client.list_custom_fields(account.subdomain, token)

        existing_map = {fm.amo_field_id: fm for fm in account.field_mappings}
        for af in amo_fields:
            af_id = af["id"]
            if af_id == account.ai_reply_field_id:
                continue  # Пропускаем служебное поле ответа ИИ

            if af_id in existing_map:
                existing_map[af_id].field_name = af["name"]
                existing_map[af_id].field_type = af.get("type", "text")
            else:
                new_fm = FieldMapping(
                    account_id=account.id,
                    amo_field_id=af_id,
                    field_name=af["name"],
                    field_type=af.get("type", "text"),
                    is_enabled=False,
                    ai_hint=f"Значение поля {af['name']}",
                    overwrite_if_filled=False
                )
                session.add(new_fm)

        await session.commit()

        fm_stmt = select(FieldMapping).where(FieldMapping.account_id == account_id).order_by(FieldMapping.amo_field_id.asc())
        all_mappings = (await session.execute(fm_stmt)).scalars().all()

        return [
            {
                "id": str(fm.id),
                "amo_field_id": fm.amo_field_id,
                "field_name": fm.field_name,
                "field_type": fm.field_type,
                "is_enabled": fm.is_enabled,
                "ai_hint": fm.ai_hint,
                "overwrite_if_filled": fm.overwrite_if_filled
            }
            for fm in all_mappings
        ]


@router.patch("/{account_id}/fields")
async def update_account_fields(account_id: uuid.UUID, payload: UpdateFieldsRequest):
    """Настройка полей для Экстрактора (включение, подсказка для ИИ, перезапись)"""
    async with AsyncSessionLocal() as session:
        stmt = select(FieldMapping).where(FieldMapping.account_id == account_id)
        mappings = (await session.execute(stmt)).scalars().all()
        mapping_dict = {m.amo_field_id: m for m in mappings}

        for item in payload.fields:
            if item.amo_field_id in mapping_dict:
                m = mapping_dict[item.amo_field_id]
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
            "extractor_model": cfg.extractor_model,
            "temperature": float(cfg.temperature),
            "handover_after_stuck": cfg.handover_after_stuck
        }


@router.patch("/{account_id}/ai-config")
async def update_ai_config(account_id: uuid.UUID, payload: UpdateAIConfigRequest):
    """Обновление настроек ИИ (системный промпт, модели, температура, лимит застревания)"""
    async with AsyncSessionLocal() as session:
        stmt = select(AIConfig).where(AIConfig.account_id == account_id)
        cfg = (await session.execute(stmt)).scalar_one_or_none()
        if not cfg:
            cfg = AIConfig(account_id=account_id)
            session.add(cfg)

        if payload.communicator_prompt is not None:
            cfg.communicator_prompt = payload.communicator_prompt
        if payload.communicator_model is not None:
            cfg.communicator_model = payload.communicator_model
        if payload.extractor_model is not None:
            cfg.extractor_model = payload.extractor_model
        if payload.temperature is not None:
            cfg.temperature = payload.temperature
        if payload.handover_after_stuck is not None:
            cfg.handover_after_stuck = payload.handover_after_stuck

        await session.commit()
        return {"success": True, "message": "Настройки ИИ успешно обновлены"}
