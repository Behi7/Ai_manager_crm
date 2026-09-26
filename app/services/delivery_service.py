import asyncio
import base64
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.core.security import decrypt_token
from app.models.account import Account, AccountStatus, FieldMapping
from app.models.lead import Lead, ConversationMessage
from app.models.extraction import ExtractionLog
from app.services.amocrm_client import amocrm_client, AmoCRMAuthOrBillingError
from app.services.debounce_service import debounce_service
from app.services.llm_communicator import communicator
from app.services.llm_extractor import extractor

logger = logging.getLogger("DeliveryService")


class DeliveryService:
    @asynccontextmanager
    async def _session_scope(self, existing_session=None):
        """
        Контекстный менеджер коротких сессий БД:
        открывает транзакцию только на время чтения/записи в БД (<15мс)
        и не удерживает соединение во время внешних HTTP-запросов (Gemini, amoCRM).
        Поддерживает инъекцию существующей сессии для обратной совместимости с unit-тестами.
        """
        if existing_session is not None:
            yield existing_session
        else:
            async with AsyncSessionLocal() as s:
                yield s

    async def process_lead_after_debounce(self, account_id_str: str, lead_id_str: str):
        """
        Фоновая обработка накопленных сообщений лида:
        1. Захват LEAD_BUSY мьютекса
        2. Проверка статуса аккаунта и лида (короткая сессия БД)
        3. Генерация ответа ИИ через LLMCommunicator (БЕЗ сессии БД)
        4. Запись в поле сделки amoCRM (PATCH, БЕЗ сессии БД)
        5. Запуск Salesbot (POST /api/v4/bots/{id}/run, БЕЗ сессии БД)
        6. Экстракция полей через LLMExtractor под тем же локом (БЕЗ сессии БД)
        7. Освобождение мьютекса
        """
        lock_acquired = await debounce_service.acquire_lead_lock(account_id_str, lead_id_str)
        if not lock_acquired:
            logger.info(
                f"Лид {lead_id_str} аккаунта {account_id_str} уже обрабатывается (LEAD_BUSY). "
                f"Откладываем запуск на 1.0с..."
            )
            debounce_service.schedule_debounce(
                account_id=account_id_str,
                lead_id=lead_id_str,
                callback=self.process_lead_after_debounce,
                delay=1.0
            )
            return

        try:
            try:
                account_uuid = uuid.UUID(account_id_str)
                amo_lead_id = int(lead_id_str)
            except ValueError as val_err:
                logger.error(f"Некорректный ID аккаунта '{account_id_str}' или лида '{lead_id_str}': {val_err}")
                return

            async with asyncio.timeout(75):
                await self._process_lead_pipeline(account_uuid, amo_lead_id, account_id_str, lead_id_str)
        except TimeoutError:
            logger.error(f"⏱️ Таймаут обработки лида {lead_id_str} аккаунта {account_id_str} (>75с). Пайплайн прерван.")
        except AmoCRMAuthOrBillingError as auth_err:
            logger.error(f"🚨 Ошибка авторизации/подписки amoCRM: {auth_err.detail}")
            try:
                account_uuid = uuid.UUID(account_id_str)
                async with AsyncSessionLocal() as session:
                    acc_stmt = select(Account).where(Account.id == account_uuid)
                    acc_obj = (await session.execute(acc_stmt)).scalar_one_or_none()
                    if acc_obj:
                        acc_obj.status = AccountStatus.ERROR
                        acc_obj.last_error = auth_err.detail
                        acc_obj.is_active = False
                        await session.commit()

                # Устанавливаем флаг acc_disabled в Redis, чтобы входящие вебхуки не тратили ресурсы
                r_redis = await debounce_service.get_redis()
                await r_redis.set(f"acc_disabled:{account_id_str}", "1")
                logger.warning(f"🛑 Аккаунт {account_id_str} переведен в статус ERROR и остановлен (is_active=False).")
            except Exception as save_err:
                logger.exception(f"Ошибка сохранения статуса ERROR для аккаунта {account_id_str}: {save_err}")
        except Exception as e:
            logger.exception(f"Непредвиденная ошибка в process_lead_after_debounce: {e}")
        finally:
            # 1. Лок снимается только после завершения и чата, и экстрактора
            await debounce_service.release_lead_lock(account_id_str, lead_id_str)

            # 2. Проверяем, не нападали ли новые сообщения в буфер, пока ИИ генерировал ответ
            try:
                if await debounce_service.has_buffered_messages(account_id_str, lead_id_str):
                    logger.info(
                        f"Обнаружены новые сообщения в буфере лида {lead_id_str} "
                        f"(пришли во время генерации ответа). Запускаем обработку следующего пакета через 0.5с..."
                    )
                    debounce_service.schedule_debounce(
                        account_id=account_id_str,
                        lead_id=lead_id_str,
                        callback=self.process_lead_after_debounce,
                        delay=0.5
                    )
            except Exception as ex:
                logger.error(f"Ошибка проверки буфера сообщений лида {lead_id_str}: {ex}")

    async def _process_lead_pipeline(
        self,
        account_uuid: uuid.UUID,
        amo_lead_id: int,
        account_id_str: str,
        lead_id_str: str
    ):
        # 1. Короткая сессия БД для чтения конфигурации аккаунта (соединение возвращается в пул до сетевых вызовов)
        async with AsyncSessionLocal() as session:
            stmt = (
                select(Account)
                .options(
                    selectinload(Account.ai_config),
                    selectinload(Account.field_mappings),
                    selectinload(Account.pipelines),
                )
                .where(Account.id == account_uuid)
            )
            account = (await session.execute(stmt)).scalar_one_or_none()

            if not account:
                logger.error(f"Аккаунт {account_id_str} не найден в БД.")
                return

            if not account.is_active:
                logger.info(f"Аккаунт {account_id_str} остановлен пользователем (is_active=False). Пропуск.")
                return

            if account.status == AccountStatus.ERROR:
                logger.warning(f"Аккаунт {account_id_str} в статусе ERROR. Пропуск.")
                return

            if not account.bot_id or not account.ai_reply_field_id:
                logger.warning(f"Аккаунт {account_id_str} не сконфигурирован: bot_id={account.bot_id}, field_id={account.ai_reply_field_id}")
                return

            try:
                access_token = decrypt_token(account.encrypted_token)
            except Exception as e:
                logger.error(f"Не удалось расшифровать токен аккаунта {account_id_str}: {e}")
                account.status = AccountStatus.ERROR
                account.last_error = "Ошибка расшифровки токена amoCRM. Переподключите аккаунт."
                account.is_active = False
                await session.commit()
                try:
                    r_redis = await debounce_service.get_redis()
                    await r_redis.set(f"acc_disabled:{account_id_str}", "1")
                    await debounce_service.pop_buffered_messages(account_id_str, lead_id_str)
                except Exception:
                    pass
                return

            subdomain = account.subdomain

        # 2. Извлекаем сообщения из Redis-буфера (вне сессии БД)
        buffered_data = await debounce_service.pop_buffered_messages(account_id_str, lead_id_str)
        if not buffered_data:
            logger.info(f"Буфер сообщений для лида {lead_id_str} пуст.")
            return

        is_comment = False
        if isinstance(buffered_data, dict):
            buffered_texts = buffered_data.get("texts", [])
            buffered_attachments = buffered_data.get("attachments", [])
            is_comment = bool(buffered_data.get("is_comment", False))
        else:
            buffered_texts = buffered_data or []
            buffered_attachments = []

        if not buffered_texts and not buffered_attachments:
            return

        # 3. Запуск пайплайна без удержания глобальной сессии БД
        delivery_state: Dict[str, Any] = {"bot_ok": False, "user_msg_id": None}
        try:
            await self._execute_lead_pipeline(
                session=None,
                account=account,
                account_uuid=account_uuid,
                amo_lead_id=amo_lead_id,
                account_id_str=account_id_str,
                lead_id_str=lead_id_str,
                access_token=access_token,
                subdomain=subdomain,
                buffered_texts=buffered_texts,
                buffered_attachments=buffered_attachments,
                is_comment=is_comment,
                delivery_state=delivery_state,
            )
        except BaseException as pipe_err:
            if delivery_state.get("user_msg_id") and not delivery_state.get("bot_ok"):
                try:
                    async with AsyncSessionLocal() as cleanup_db:
                        msg_to_del = (
                            await cleanup_db.execute(
                                select(ConversationMessage).where(ConversationMessage.id == delivery_state["user_msg_id"])
                            )
                        ).scalar_one_or_none()
                        if msg_to_del:
                            await cleanup_db.delete(msg_to_del)
                            await cleanup_db.commit()
                except Exception as del_err:
                    logger.warning(f"Не удалось откатить user_msg {delivery_state['user_msg_id']}: {del_err}")

            if isinstance(pipe_err, AmoCRMAuthOrBillingError) or delivery_state.get("bot_ok"):
                raise

            retries = int(buffered_data.get("retry_count", 0) or 0) if isinstance(buffered_data, dict) else 0
            if retries < 2:
                if isinstance(buffered_data, dict):
                    buffered_data["retry_count"] = retries + 1
                logger.warning(
                    f"Сбой в пайплайне лида {lead_id_str} ({type(pipe_err).__name__}). "
                    f"Восстановление сообщений в буфер Redis (попытка {retries + 1}/2)..."
                )
                try:
                    await asyncio.shield(
                        debounce_service.restore_buffered_messages(account_id_str, lead_id_str, buffered_data)
                    )
                except Exception as res_err:
                    logger.error(f"Не удалось восстановить буфер для {account_id_str}:{lead_id_str}: {res_err}")
            else:
                logger.error(
                    f"Превышен лимит попыток восстановления буфера (2/2) для лида {lead_id_str}. Сообщения сброшены."
                )
            raise

    async def _execute_lead_pipeline(
        self,
        session,
        account,
        account_uuid: uuid.UUID,
        amo_lead_id: int,
        account_id_str: str,
        lead_id_str: str,
        access_token: str,
        subdomain: str,
        buffered_texts: List[str],
        buffered_attachments: List[Dict[str, Any]],
        is_comment: bool = False,
        delivery_state: Optional[Dict[str, Any]] = None,
    ):
        # 1. Внешний HTTP-вызов: Скачиваем медиавложения (БЕЗ удержания сессии БД)
        media_parts: List[Dict[str, Any]] = []
        if buffered_attachments:
            for att in buffered_attachments:
                link = att.get("link")
                if not link:
                    continue
                downloaded = await amocrm_client.download_attachment(
                    url=link,
                    token=access_token,
                    subdomain=subdomain,
                    hint_type=att.get("type"),
                    hint_filename=att.get("file_name")
                )
                if downloaded and downloaded.get("data_bytes"):
                    b64_data = base64.b64encode(downloaded["data_bytes"]).decode("utf-8")
                    media_parts.append({
                        "mime_type": downloaded["mime_type"],
                        "data_b64": b64_data,
                        "file_name": downloaded["file_name"]
                    })

        combined_user_text = "\n".join(buffered_texts).strip()
        if not combined_user_text and media_parts:
            combined_user_text = "[Входящее медиасообщение]"
        elif not combined_user_text:
            return

        # 2. Внешний HTTP-вызов: Получаем актуальные данные сделки и контакта из amoCRM (БЕЗ сессии БД)
        enabled_pipeline_ids = [p.amo_pipeline_id for p in (account.pipelines or []) if p.is_enabled]
        has_enabled_mappings = any(fm.is_enabled for fm in (account.field_mappings or []))

        lead_amo_data = None
        contact_id = None
        contact_amo_data = None
        if enabled_pipeline_ids or has_enabled_mappings:
            lead_amo_data = await amocrm_client.get_lead(subdomain, access_token, amo_lead_id)

            if lead_amo_data:
                contact_id = (
                    lead_amo_data.get("contact_id")
                    or next(
                        iter(
                            (lead_amo_data.get("_embedded") or {}).get("contacts") or []
                        ),
                        {}
                    ).get("id")
                )
                if contact_id:
                    try:
                        contact_amo_data = await amocrm_client.get_contact(
                            subdomain, contact_id, access_token=access_token
                        )
                        logger.info(f"Загружен контакт #{contact_id} для лида #{amo_lead_id}")
                    except Exception as e:
                        logger.warning(f"Не удалось загрузить контакт #{contact_id}: {e}")

        active_pipeline = None
        if lead_amo_data and getattr(account, "pipelines", None):
            lead_pipeline_id = lead_amo_data.get("pipeline_id")
            for p_obj in (account.pipelines or []):
                if getattr(p_obj, "amo_pipeline_id", None) == lead_pipeline_id:
                    active_pipeline = p_obj
                    break

        if enabled_pipeline_ids and lead_amo_data:
            lead_pipeline_id = lead_amo_data.get("pipeline_id")
            if lead_pipeline_id and lead_pipeline_id not in enabled_pipeline_ids:
                logger.info(
                    f"Лид #{amo_lead_id} находится в воронке #{lead_pipeline_id}, "
                    f"которая не включена в настройках аккаунта ({enabled_pipeline_ids}). ИИ пропускает сообщение."
                )
                return

            # Проверка: разрешено ли ИИ отвечать на текущем этапе (status_id) данной воронки
            lead_status_id = lead_amo_data.get("status_id")
            if active_pipeline and lead_status_id:
                p_stages = getattr(active_pipeline, "stages_json", None)
                p_enabled_stages = getattr(active_pipeline, "enabled_stage_ids", None)
                if isinstance(p_stages, list) and p_stages and isinstance(p_enabled_stages, list):
                    all_work_stage_ids = {int(s["id"]) for s in p_stages if isinstance(s, dict) and s.get("id")}
                    enabled_stage_set = {int(sid) for sid in p_enabled_stages}
                    if (int(lead_status_id) in all_work_stage_ids or int(lead_status_id) in (142, 143)) and int(lead_status_id) not in enabled_stage_set:
                        logger.info(
                            f"Лид #{amo_lead_id} находится на этапе #{lead_status_id} воронки '{getattr(active_pipeline, 'name', '')}', "
                            f"где ответы ИИ отключены ({list(enabled_stage_set)}). ИИ пропускает сообщение."
                        )
                        return

        # 3. Короткая сессия БД №1: Чтение/создание лида, запись сообщения пользователя и чтение истории
        async with self._session_scope(session) as db:
            lead_stmt = select(Lead).where(
                Lead.account_id == account_uuid,
                Lead.amo_lead_id == amo_lead_id
            )
            lead = (await db.execute(lead_stmt)).scalar_one_or_none()

            if not lead:
                try:
                    nested_ctx = db.begin_nested() if hasattr(db, "begin_nested") else None
                    if nested_ctx is not None and hasattr(nested_ctx, "__aenter__"):
                        async with nested_ctx:
                            lead = Lead(
                                account_id=account_uuid,
                                amo_lead_id=amo_lead_id,
                                stuck_count=0,
                                handover_required=False,
                                last_message_at=datetime.now(timezone.utc)
                            )
                            db.add(lead)
                            await db.flush()
                    else:
                        lead = Lead(
                            account_id=account_uuid,
                            amo_lead_id=amo_lead_id,
                            stuck_count=0,
                            handover_required=False,
                            last_message_at=datetime.now(timezone.utc)
                        )
                        db.add(lead)
                        await db.flush()
                except IntegrityError:
                    lead = (await db.execute(lead_stmt)).scalar_one()

            if lead.handover_required:
                logger.info(f"Лид {amo_lead_id} уже переведен на оператора (handover_required=True). Пропуск ответа ИИ.")
                return

            lead_db_id = lead.id
            current_stuck_count = lead.stuck_count or 0

            user_msg = ConversationMessage(
                lead_id=lead_db_id,
                role="user",
                content=combined_user_text,
                created_at=datetime.now(timezone.utc)
            )
            db.add(user_msg)
            await db.flush()
            user_msg_id = user_msg.id
            if delivery_state is not None:
                delivery_state['user_msg_id'] = user_msg_id
            lead.last_message_at = datetime.now(timezone.utc)
            await db.commit()

            history_stmt = (
                select(ConversationMessage)
                .where(ConversationMessage.lead_id == lead_db_id)
                .order_by(ConversationMessage.created_at.desc())
                .limit(20)
            )
            all_m = (await db.execute(history_stmt)).scalars().all()
            db_messages = list(all_m) if isinstance(all_m, (list, tuple)) else []
            recent_messages = list(reversed(db_messages))
            history_payload = [{"role": m.role, "content": m.content} for m in recent_messages]

            ext_hist_stmt = (
                select(ExtractionLog)
                .where(ExtractionLog.lead_id == lead_db_id)
                .order_by(ExtractionLog.created_at.desc())
                .limit(30)
            )
            ext_hist_all = (await db.execute(ext_hist_stmt)).scalars().all()
            ext_hist_rows = list(reversed(ext_hist_all)) if isinstance(ext_hist_all, (list, tuple)) else []
            previously_extracted_raw: Dict[str, str] = {}
            if isinstance(ext_hist_rows, (list, tuple)):
                for er in ext_hist_rows:
                    raw_r = getattr(er, "raw_response", None) or {}
                    if isinstance(raw_r, dict):
                        for rk, rv in raw_r.items():
                            if rv is not None and str(rv).strip() and str(rv).strip() != "null":
                                previously_extracted_raw[str(rk)] = str(rv).strip()

        # 4. Подготовка целевых полей (FieldMapping) в памяти (БЕЗ сессии БД)
        amo_cf_values: Dict[Any, str] = {}
        if contact_amo_data:
            raw_c_name = str(contact_amo_data.get("name") or "").strip()
            if raw_c_name:
                amo_cf_values["raw_contact_name"] = raw_c_name
        if lead_amo_data:
            raw_l_name = str(lead_amo_data.get("name") or "").strip()
            if raw_l_name:
                amo_cf_values["raw_lead_name"] = raw_l_name
        if "field_contact_sys_name" in previously_extracted_raw or "field_contact_-1" in previously_extracted_raw:
            c_sys_val = previously_extracted_raw.get("field_contact_sys_name") or previously_extracted_raw.get("field_contact_-1")
            if c_sys_val:
                amo_cf_values[("contact", -1)] = c_sys_val
                amo_cf_values[-1] = c_sys_val
        if "field_lead_sys_name" in previously_extracted_raw or "field_lead_-2" in previously_extracted_raw:
            l_sys_val = previously_extracted_raw.get("field_lead_sys_name") or previously_extracted_raw.get("field_lead_-2")
            if l_sys_val:
                amo_cf_values[("lead", -2)] = l_sys_val
                amo_cf_values[-2] = l_sys_val
        if contact_amo_data:
            for cf in (contact_amo_data.get("custom_fields_values") or []):
                fid = cf.get("field_id")
                vals = cf.get("values", [])
                if fid and vals:
                    val_str = str(vals[0].get("value") or "").strip()
                    if val_str:
                        amo_cf_values[("contact", fid)] = val_str
                        amo_cf_values.setdefault(fid, val_str)
        if lead_amo_data:
            for cf in (lead_amo_data.get("custom_fields_values") or []):
                fid = cf.get("field_id")
                vals = cf.get("values", [])
                if fid and vals:
                    val_str = str(vals[0].get("value") or "").strip()
                    if val_str:
                        amo_cf_values[("lead", fid)] = val_str
                        amo_cf_values[fid] = val_str

        ai_config = account.ai_config

        # 4.1. СНАЧАЛА запускаем Экстрактор по входящему сообщению и медиа ДО генерации ответа Общителя,
        # чтобы все данные, которые клиент сообщил в текущем сообщении, сразу попали в known_fields и ушли из target_fields!
        extracted_items_pre: List[Dict[str, Any]] = []
        disabled_field_keys_pre: set = set()
        try:
            extracted_items_pre, disabled_field_keys_pre = await self._run_extractor_step(
                session=session,
                account=account,
                account_uuid=account_uuid,
                amo_lead_id=amo_lead_id,
                lead=lead,
                lead_db_id=lead_db_id,
                current_stuck_count=current_stuck_count,
                subdomain=subdomain,
                access_token=access_token,
                ai_config=ai_config,
                history_payload=history_payload,
                reply_text="",
                amo_cf_values=amo_cf_values,
                media_parts=media_parts,
                contact_id=contact_id,
                contact_amo_data=contact_amo_data,
                active_pipeline=active_pipeline,
                lead_amo_data=lead_amo_data,
                skip_stage_advance=True,
            )
            if extracted_items_pre:
                current_stuck_count = 0
        except BaseException as ext_err:
            logger.error(f"Ошибка/таймаут на этапе предварительной работы Экстрактора для лида {amo_lead_id}: {ext_err}")

        # 4.2. Формируем known_fields и target_fields уже с учётом только что извлечённых данных
        known_fields: Dict[str, str] = {}
        target_fields: List[Dict[str, str]] = []
        for fm in (account.field_mappings or []):
            if not fm.is_enabled:
                continue
            if fm.amo_field_id == account.ai_reply_field_id:
                continue

            fm_entity = (fm.entity_type if isinstance(getattr(fm, "entity_type", None), str) and fm.entity_type in ("lead", "contact") else "lead")
            fm_key = (fm_entity, fm.amo_field_id)
            if fm_key in amo_cf_values:
                known_fields[fm.field_name] = amo_cf_values[fm_key]
            elif fm.amo_field_id in amo_cf_values and fm_entity == "lead":
                known_fields[fm.field_name] = amo_cf_values[fm.amo_field_id]
            else:
                target_fields.append({
                    "name": fm.field_name,
                    "hint": fm.ai_hint or f"Выяснить {fm.field_name}"
                })

        is_comment_lead = is_comment
        if not is_comment_lead and lead_amo_data:
            lead_tags = [t.get("name", "").lower() for t in ((lead_amo_data.get("_embedded") or {}).get("tags") or [])]
            if any("comment" in t or "коммент" in t for t in lead_tags):
                is_comment_lead = True
        prompt = ai_config.communicator_prompt if ai_config else "Ты вежливый ИИ-менеджер."
        if is_comment_lead and ai_config and ai_config.comment_prompt:
            prompt = ai_config.comment_prompt
        direct_link = ai_config.direct_link if ai_config else None

        account_gemini_key = (getattr(ai_config, "gemini_api_key", None) or "").strip() or None
        comm_model = ai_config.communicator_model if ai_config else "gemini-3.1-flash-lite"
        fallback_comm_model = ai_config.fallback_communicator_model if ai_config and ai_config.fallback_communicator_model else "gemini-2.5-flash"
        temperature = float(ai_config.temperature) if ai_config else 0.4
        handover_limit = ai_config.handover_after_stuck if ai_config else 4
        knowledge_base = ai_config.knowledge_base if ai_config else None
        knowledge_mode = ai_config.knowledge_mode if ai_config else "plain_text"
        now_utc = datetime.now(timezone.utc)
        gemini_cache_name = (
            ai_config.gemini_cache_name
            if (
                ai_config
                and ai_config.gemini_cache_name
                and getattr(ai_config, "gemini_cache_expires_at", None)
                and ai_config.gemini_cache_expires_at > now_utc
            )
            else None
        )

        if (
            not gemini_cache_name
            and knowledge_mode == "gemini_cache"
            and knowledge_base
            and len(knowledge_base) >= 32000
            and hasattr(communicator, "create_gemini_context_cache")
        ):
            try:
                created_cache = await communicator.create_gemini_context_cache(
                    model_name=comm_model,
                    system_instruction=prompt,
                    knowledge_content=knowledge_base,
                    ttl_seconds=3600,
                    api_key=account_gemini_key,
                )
                if isinstance(created_cache, str) and created_cache:
                    gemini_cache_name = created_cache
                    async with self._session_scope(session) as db:
                        cfg_obj = (
                            await db.execute(select(AIConfig).where(AIConfig.account_id == account_uuid))
                        ).scalar_one_or_none()
                        if cfg_obj:
                            cfg_obj.gemini_cache_name = created_cache
                            cfg_obj.gemini_cache_expires_at = now_utc + timedelta(seconds=3500)
                            await db.commit()
            except Exception as cache_err:
                logger.warning(f"Не удалось создать/обновить Gemini Context Cache: {cache_err}")

        # 5. Внешний HTTP-вызов: Генерация ответа в Gemini (БЕЗ сессии БД!)
        comm_qual_prompt = getattr(ai_config, "communicator_qualification_prompt", None) if ai_config else None
        comm_rules_prompt = getattr(ai_config, "communicator_rules_prompt", None) if ai_config else None
        comm_resp = await communicator.generate_reply(
            system_prompt=prompt,
            messages=history_payload,
            model_name=comm_model,
            fallback_model=fallback_comm_model,
            temperature=temperature,
            media_parts=media_parts,
            target_fields=target_fields,
            known_fields=known_fields,
            knowledge_base=knowledge_base,
            knowledge_mode=knowledge_mode,
            gemini_cache_name=gemini_cache_name,
            is_comment=is_comment_lead,
            direct_link=direct_link,
            api_key=account_gemini_key,
            qualification_prompt=comm_qual_prompt,
            rules_prompt=comm_rules_prompt
        )

        if comm_resp.media_summary:
            async with self._session_scope(session) as db:
                msg_obj = (await db.execute(select(ConversationMessage).where(ConversationMessage.id == user_msg_id))).scalar_one_or_none()
                if msg_obj:
                    if combined_user_text == "[Входящее медиасообщение]":
                        msg_obj.content = comm_resp.media_summary
                    else:
                        msg_obj.content = f"{comm_resp.media_summary}\n{combined_user_text}"
                    await db.commit()

        # Проверка условий Handover
        is_stuck_threshold = current_stuck_count >= handover_limit
        if comm_resp.is_handover_requested or is_stuck_threshold:
            logger.info(f"Инициирован Handover для лида {amo_lead_id} (запрос={comm_resp.is_handover_requested}, stuck={current_stuck_count})")
            async with self._session_scope(session) as db:
                lead_obj = (await db.execute(select(Lead).where(Lead.id == lead_db_id))).scalar_one_or_none()
                if lead_obj:
                    lead_obj.handover_required = True
                    await db.commit()

            task_text = (
                "Клиент запросил оператора в чате."
                if comm_resp.is_handover_requested
                else f"ИИ не может решить вопрос клиента после {current_stuck_count} попыток. Подключитесь к диалогу."
            )
            # Перевод сделки на 4-й этап (индекс 4 — Handover / Требуется менеджер), если этапы синхронизированы
            if active_pipeline:
                p_stages = getattr(active_pipeline, "stages_json", None)
                if isinstance(p_stages, list) and p_stages:
                    handover_idx = min(4, len(p_stages) - 1)
                    target_status_id = int(p_stages[handover_idx]["id"])
                    cur_status_id = int((lead_amo_data or {}).get("status_id") or 0)
                    if cur_status_id != target_status_id:
                        await amocrm_client.patch_lead_status(
                            subdomain=subdomain,
                            access_token=access_token,
                            lead_id=amo_lead_id,
                            status_id=target_status_id,
                        )

            await amocrm_client.create_operator_task(
                subdomain=subdomain,
                access_token=access_token,
                element_id=amo_lead_id,
                text=task_text
            )

            handover_notice = "Переключаю вас на менеджера. Специалист скоро подключится к диалогу!"
            reply_field_id = account.ai_reply_field_id
            if not reply_field_id:
                logger.error(f"У аккаунта {account_id_str} отсутствует ai_reply_field_id! Доставка отменена.")
                return
            patch_items = [{"field_id": reply_field_id, "values": [{"value": handover_notice}]}]
            patch_ok = await amocrm_client.patch_lead_custom_fields(
                subdomain=subdomain,
                access_token=access_token,
                lead_id=amo_lead_id,
                fields=patch_items
            )
            if patch_ok:
                await amocrm_client.run_salesbot(
                    subdomain=subdomain,
                    access_token=access_token,
                    bot_id=account.bot_id,
                    entity_id=amo_lead_id
                )
                async with self._session_scope(session) as db:
                    asst_msg = ConversationMessage(
                        lead_id=lead_db_id,
                        role="assistant",
                        content=handover_notice,
                        created_at=datetime.now(timezone.utc)
                    )
                    db.add(asst_msg)
                    await db.commit()
            return

        reply_text = comm_resp.text

        # 5.8. Автоматическое продвижение сделки по этапам воронки (0 -> 1 -> 2 -> 3) ДО записи Ответ ИИ и запуска Salesbot
        try:
            await self._advance_pipeline_stage(
                account=account,
                amo_lead_id=amo_lead_id,
                subdomain=subdomain,
                access_token=access_token,
                amo_cf_values=amo_cf_values,
                extracted_items=extracted_items_pre,
                disabled_field_keys=disabled_field_keys_pre,
                active_pipeline=active_pipeline,
                lead_amo_data=lead_amo_data,
            )
        except Exception as stg_err:
            logger.error(f"Ошибка при продвижении этапа сделки #{amo_lead_id}: {stg_err}")

        # 6. Внешние HTTP-вызовы: Запись в поле сделки и запуск Salesbot (БЕЗ сессии БД)
        reply_field_id = account.ai_reply_field_id
        patch_ok = False
        if reply_field_id:
            patch_items = [{"field_id": reply_field_id, "values": [{"value": reply_text}]}]
            patch_ok = await amocrm_client.patch_lead_custom_fields(
                subdomain=subdomain,
                access_token=access_token,
                lead_id=amo_lead_id,
                fields=patch_items
            )

        if not patch_ok:
            logger.warning(
                f"Не удалось записать ответ ИИ в поле {reply_field_id} лида {amo_lead_id}. "
                "Возможно, поле удалили в amoCRM. Пытаемся автоматически восстановить поле 'AI: Ответ ассистента'..."
            )
            new_field_id = await amocrm_client.ensure_reply_field(subdomain, access_token)
            if new_field_id:
                account.ai_reply_field_id = new_field_id
                reply_field_id = new_field_id
                async with self._session_scope(session) as db:
                    acc_obj = (await db.execute(select(Account).where(Account.id == account_uuid))).scalar_one_or_none()
                    if acc_obj:
                        acc_obj.ai_reply_field_id = new_field_id
                    await db.commit()

                patch_items = [{"field_id": new_field_id, "values": [{"value": reply_text}]}]
                patch_ok = await amocrm_client.patch_lead_custom_fields(
                    subdomain=subdomain,
                    access_token=access_token,
                    lead_id=amo_lead_id,
                    fields=patch_items
                )
                if patch_ok:
                    logger.info(f"✅ Поле ответа 'AI: Ответ ассистента' успешно пересоздано (ID: {new_field_id}) и ответ доставлен!")

        if not patch_ok:
            logger.error(f"Не удалось записать ответ ИИ в поле {reply_field_id} лида {amo_lead_id}. Создаем задачу оператору.")
            await amocrm_client.create_operator_task(
                subdomain=subdomain,
                access_token=access_token,
                element_id=amo_lead_id,
                text="Ошибка доставки ответа ИИ в поле сделки (поле удалено или недоступно). Проверьте сделку вручную."
            )
            return

        bot_ok = await amocrm_client.run_salesbot(
            subdomain=subdomain,
            access_token=access_token,
            bot_id=account.bot_id,
            entity_id=amo_lead_id
        )

        if delivery_state is not None:
            delivery_state['bot_ok'] = bool(bot_ok)
        if not bot_ok:
            logger.error(f"Salesbot {account.bot_id} вернул ошибку при запуске для лида {amo_lead_id}")
            await amocrm_client.create_operator_task(
                subdomain=subdomain,
                access_token=access_token,
                element_id=amo_lead_id,
                text=f"Ошибка запуска Salesbot #{account.bot_id} при отправке ответа ИИ. Проверьте привязку бота."
            )
            raise RuntimeError(f"Salesbot {account.bot_id} execution failed for lead {amo_lead_id}")

        # 7. Короткая сессия БД №2: Сохраняем ответ ассистента
        async with self._session_scope(session) as db:
            asst_msg = ConversationMessage(
                lead_id=lead_db_id,
                role="assistant",
                content=reply_text,
                created_at=datetime.now(timezone.utc)
            )
            db.add(asst_msg)
            await db.commit()

        logger.info(f"Успешная доставка ответа ИИ лиду {amo_lead_id}: '{reply_text[:60]}...'")

        # (Экстрактор и продвижение этапа уже выполнены на шагах 4.1 и 5.8)

    async def _run_extractor_step(
        self,
        session,
        account,
        account_uuid: uuid.UUID,
        amo_lead_id: int,
        lead,
        lead_db_id,
        current_stuck_count: int,
        subdomain: str,
        access_token: str,
        ai_config,
        history_payload,
        reply_text: str,
        amo_cf_values,
        media_parts,
        contact_id,
        contact_amo_data,
        active_pipeline=None,
        lead_amo_data=None,
        skip_stage_advance: bool = False,
    ):
        enabled_mappings = [fm for fm in (account.field_mappings or []) if fm.is_enabled]
        extracted_items: List[Dict[str, Any]] = []
        disabled_field_keys: set = set()
        if enabled_mappings:
            full_dialog = (history_payload + [{"role": "assistant", "content": reply_text}]) if reply_text else list(history_payload)
            account_gemini_key = (getattr(ai_config, "gemini_api_key", None) or "").strip() or None
            ext_model = ai_config.extractor_model if ai_config else "gemini-3.1-flash-lite"
            fallback_ext_model = ai_config.fallback_extractor_model if ai_config and ai_config.fallback_extractor_model else "gemini-2.5-flash"
            ext_sys_prompt = getattr(ai_config, "extractor_system_prompt", None) if ai_config else None
            ext_result = await extractor.extract_lead_fields(
                field_mappings=account.field_mappings,
                messages=full_dialog,
                current_lead_values=amo_cf_values,
                model_name=ext_model,
                fallback_model=fallback_ext_model,
                media_parts=media_parts,
                api_key=account_gemini_key,
                system_prompt=ext_sys_prompt
            )

            if ext_result.fields_to_update:
                extracted_items = list(ext_result.fields_to_update)
                logger.info(f"Экстрактор нашел поля для лида {amo_lead_id}: {ext_result.field_name_values}")

                fm_entity_map = {fm.amo_field_id: (fm.entity_type if isinstance(getattr(fm, "entity_type", None), str) and fm.entity_type in ("lead", "contact") else "lead") for fm in account.field_mappings}
                lead_fields_to_update = []
                contact_fields_to_update = []
                for item in ext_result.fields_to_update:
                    fid = item.get("field_id")
                    ent = item.get("entity_type") or fm_entity_map.get(fid, "lead")
                    clean_item = {"field_id": fid, "values": item.get("values", [])}
                    if ent == "contact":
                        contact_fields_to_update.append(clean_item)
                    else:
                        lead_fields_to_update.append(clean_item)

                if lead_fields_to_update:
                    ext_patch_ok = await amocrm_client.patch_lead_custom_fields(
                        subdomain=subdomain,
                        access_token=access_token,
                        lead_id=amo_lead_id,
                        fields=lead_fields_to_update
                    )
                    if not ext_patch_ok:
                        logger.warning(
                            f"Пакетное обновление полей сделки #{amo_lead_id} не удалось. "
                            "Пробуем сохранить поля поштучно, изолируя удалённые..."
                        )
                        for item in lead_fields_to_update:
                            fid = item.get("field_id")
                            single_ok = await amocrm_client.patch_lead_custom_fields(
                                subdomain=subdomain,
                                access_token=access_token,
                                lead_id=amo_lead_id,
                                fields=[item]
                            )
                            if not single_ok:
                                last_code = getattr(amocrm_client, "last_status_code", 400)
                                if last_code == 400:
                                    logger.error(f"Поле #{fid} отклонено amoCRM (HTTP 400, вероятно, удалено). Автоматически отключаем маппинг.")
                                    disabled_field_keys.add(("lead", fid))
                                    for fm in account.field_mappings:
                                        fm_ent = fm.entity_type if isinstance(getattr(fm, "entity_type", None), str) and fm.entity_type in ("lead", "contact") else "lead"
                                        if fm.amo_field_id == fid and fm_ent == "lead":
                                            fm.is_enabled = False
                                else:
                                    logger.warning(f"Временная ошибка amoCRM (status={last_code}) при сохранении поля сделки #{fid}. Маппинг НЕ отключаем.")

                target_contact_id = contact_id or (contact_amo_data.get("id") if contact_amo_data else None)
                if contact_fields_to_update:
                    if target_contact_id:
                        contact_patch_ok = await amocrm_client.patch_contact_custom_fields(
                            subdomain=subdomain,
                            access_token=access_token,
                            contact_id=target_contact_id,
                            fields=contact_fields_to_update
                        )
                        if not contact_patch_ok:
                            logger.warning(
                                f"Пакетное обновление полей контакта #{target_contact_id} не удалось. "
                                "Пробуем поштучно..."
                            )
                            for item in contact_fields_to_update:
                                fid = item.get("field_id")
                                single_ok = await amocrm_client.patch_contact_custom_fields(
                                    subdomain=subdomain,
                                    access_token=access_token,
                                    contact_id=target_contact_id,
                                    fields=[item]
                                )
                                if not single_ok:
                                    last_code = getattr(amocrm_client, "last_status_code", 400)
                                    if last_code == 400:
                                        logger.error(f"Поле контакта #{fid} отклонено amoCRM (HTTP 400). Отключаем маппинг.")
                                        disabled_field_keys.add(("contact", fid))
                                        for fm in account.field_mappings:
                                            fm_ent = fm.entity_type if isinstance(getattr(fm, "entity_type", None), str) and fm.entity_type in ("lead", "contact") else "lead"
                                            if fm.amo_field_id == fid and fm_ent == "contact":
                                                fm.is_enabled = False
                                    else:
                                        logger.warning(f"Временная ошибка amoCRM (status={last_code}) при сохранении поля контакта #{fid}. Маппинг НЕ отключаем.")
                    else:
                        logger.warning(f"Нет contact_id для записи полей контакта в лиде #{amo_lead_id}.")

                # 9. Короткая сессия БД №3: Сохранение логов экстрактора и отключение удаленных полей
                async with self._session_scope(session) as db:
                    if disabled_field_keys:
                        disabled_ids = [fid for _, fid in disabled_field_keys]
                        fms = (await db.execute(
                            select(FieldMapping).where(
                                FieldMapping.account_id == account_uuid,
                                FieldMapping.amo_field_id.in_(disabled_ids)
                            )
                        )).scalars().all()
                        if isinstance(fms, (list, tuple)):
                            for f_obj in fms:
                                f_ent = f_obj.entity_type if isinstance(getattr(f_obj, "entity_type", None), str) and f_obj.entity_type in ("lead", "contact") else "lead"
                                if (f_ent, f_obj.amo_field_id) in disabled_field_keys:
                                    f_obj.is_enabled = False

                    ext_log = ExtractionLog(
                        lead_id=lead_db_id,
                        raw_response=ext_result.raw_response,
                        applied_fields=ext_result.field_name_values,
                        created_at=datetime.now(timezone.utc)
                    )
                    db.add(ext_log)

                    lead_obj = (await db.execute(select(Lead).where(Lead.id == lead_db_id))).scalar_one_or_none()
                    if lead_obj:
                        lead_obj.stuck_count = 0
                    lead.stuck_count = 0
                    await db.commit()
            else:
                async with self._session_scope(session) as db:
                    lead_obj = (await db.execute(select(Lead).where(Lead.id == lead_db_id))).scalar_one_or_none()
                    if lead_obj:
                        lead_obj.stuck_count = (lead_obj.stuck_count or 0) + 1
                    lead.stuck_count = current_stuck_count + 1
                    await db.commit()

        # Обновляем amo_cf_values в оперативной памяти для немедленного использования в Общителе
        if extracted_items and isinstance(amo_cf_values, dict):
            fm_id_to_ent_mem = {
                fm.amo_field_id: (
                    fm.entity_type
                    if isinstance(getattr(fm, "entity_type", None), str) and fm.entity_type in ("lead", "contact")
                    else "lead"
                )
                for fm in enabled_mappings
            }
            for item in extracted_items:
                fid = item.get("field_id")
                vals = item.get("values") or []
                val_str = str((vals[0] or {}).get("value") or "").strip() if vals else ""
                if fid and val_str:
                    ent = item.get("entity_type") or fm_id_to_ent_mem.get(fid, "lead")
                    if (ent, int(fid)) not in disabled_field_keys:
                        amo_cf_values[(ent, int(fid))] = val_str
                        amo_cf_values[int(fid)] = val_str

        if not skip_stage_advance:
            await self._advance_pipeline_stage(
                account=account,
                amo_lead_id=amo_lead_id,
                subdomain=subdomain,
                access_token=access_token,
                amo_cf_values=amo_cf_values,
                extracted_items=extracted_items,
                disabled_field_keys=disabled_field_keys,
                active_pipeline=active_pipeline,
                lead_amo_data=lead_amo_data,
            )
        return extracted_items, disabled_field_keys

    async def _advance_pipeline_stage(
        self,
        account,
        amo_lead_id: int,
        subdomain: str,
        access_token: str,
        amo_cf_values,
        extracted_items: List[Dict[str, Any]],
        disabled_field_keys: set,
        active_pipeline=None,
        lead_amo_data=None,
    ):
        enabled_mappings = [fm for fm in (account.field_mappings or []) if fm.is_enabled]
        # 10. Автоматическое продвижение сделки по этапам воронки (0 -> 1 -> 2 -> 3) БЕЗ отключения ИИ на 3-м этапе
        if active_pipeline and lead_amo_data:
            p_stages = getattr(active_pipeline, "stages_json", None)
            if isinstance(p_stages, list) and p_stages:
                filled_keys = set()
                for k, v in (amo_cf_values or {}).items():
                    if isinstance(k, tuple) and len(k) == 2 and str(v or "").strip():
                        filled_keys.add((str(k[0]), int(k[1])))

                fm_id_to_ent = {
                    fm.amo_field_id: (
                        fm.entity_type
                        if isinstance(getattr(fm, "entity_type", None), str) and fm.entity_type in ("lead", "contact")
                        else "lead"
                    )
                    for fm in enabled_mappings
                }
                for item in extracted_items:
                    fid = item.get("field_id")
                    vals = item.get("values") or []
                    val_str = str((vals[0] or {}).get("value") or "").strip() if vals else ""
                    if fid and val_str:
                        ent = item.get("entity_type") or fm_id_to_ent.get(fid, "lead")
                        if (ent, int(fid)) not in disabled_field_keys:
                            filled_keys.add((ent, int(fid)))

                reply_fid = getattr(account, "ai_reply_field_id", None)
                active_fms = [
                    fm for fm in enabled_mappings
                    if fm.is_enabled and fm.amo_field_id != reply_fid
                ]
                contact_fms = [
                    fm for fm in active_fms
                    if (fm.entity_type if isinstance(getattr(fm, "entity_type", None), str) and fm.entity_type in ("lead", "contact") else "lead") == "contact"
                ]
                lead_fms = [
                    fm for fm in active_fms
                    if (fm.entity_type if isinstance(getattr(fm, "entity_type", None), str) and fm.entity_type in ("lead", "contact") else "lead") == "lead"
                ]

                has_contact_group = len(contact_fms) > 0
                has_lead_group = len(lead_fms) > 0
                contact_ready = has_contact_group and all(("contact", int(fm.amo_field_id)) in filled_keys for fm in contact_fms)
                lead_ready = has_lead_group and all(("lead", int(fm.amo_field_id)) in filled_keys for fm in lead_fms)

                if (
                    (has_contact_group and has_lead_group and contact_ready and lead_ready)
                    or (has_contact_group and not has_lead_group and contact_ready)
                    or (has_lead_group and not has_contact_group and lead_ready)
                ):
                    target_idx = 3  # Этап 3: Все поля (Контакты + Сделка) готовы
                elif lead_ready:
                    target_idx = 2  # Этап 2: Все поля сделки заполнены
                elif contact_ready:
                    target_idx = 1  # Этап 1: Контакты заполнены
                else:
                    target_idx = 0  # Этап 0: Стартовый рабочий этап

                bounded_idx = min(target_idx, len(p_stages) - 1)
                cur_status_id = int(lead_amo_data.get("status_id") or 0)
                cur_idx = next(
                    (idx for idx, s in enumerate(p_stages) if isinstance(s, dict) and int(s.get("id") or 0) == cur_status_id),
                    -1,
                )
                # Двигаем только вперёд (или из Неразобранного cur_idx == -1 в рабочий этап)
                if cur_idx < bounded_idx:
                    new_status_id = int(p_stages[bounded_idx]["id"])
                    stage_title = p_stages[bounded_idx].get("name", "")
                    await amocrm_client.patch_lead_status(
                        subdomain=subdomain,
                        access_token=access_token,
                        lead_id=amo_lead_id,
                        status_id=new_status_id,
                    )
                    logger.info(
                        f"📈 Авто-переход лида #{amo_lead_id} на этап [{bounded_idx}] '{stage_title}' (status_id={new_status_id})"
                    )


delivery_service = DeliveryService()
