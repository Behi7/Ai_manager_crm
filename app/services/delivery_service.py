import asyncio
import base64
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.core.security import decrypt_token
from app.models.account import Account, AccountStatus
from app.models.lead import Lead, ConversationMessage
from app.models.extraction import ExtractionLog
from app.services.amocrm_client import amocrm_client, AmoCRMAuthOrBillingError
from app.services.debounce_service import debounce_service
from app.services.llm_communicator import communicator
from app.services.llm_extractor import extractor

logger = logging.getLogger("DeliveryService")


class DeliveryService:
    async def process_lead_after_debounce(self, account_id_str: str, lead_id_str: str):
        """
        Фоновая обработка накопленных сообщений лида:
        1. Захват LEAD_BUSY мьютекса
        2. Проверка статуса аккаунта и лида
        3. Генерация ответа ИИ через LLMCommunicator
        4. Запись в поле сделки amoCRM (PATCH)
        5. Запуск Salesbot (POST /api/v4/bots/{id}/run)
        6. Экстракция полей через LLMExtractor под тем же локом
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
        async with AsyncSessionLocal() as session:
                # Загружаем аккаунт со всеми связанными сущностями
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
                    return

                subdomain = account.subdomain

                # Извлекаем все сообщения из буфера дебаунса
                buffered_data = await debounce_service.pop_buffered_messages(account_id_str, lead_id_str)
                if not buffered_data:
                    logger.info(f"Буфер сообщений для лида {lead_id_str} пуст.")
                    return

                if isinstance(buffered_data, dict):
                    buffered_texts = buffered_data.get("texts", [])
                    buffered_attachments = buffered_data.get("attachments", [])
                else:
                    buffered_texts = buffered_data or []
                    buffered_attachments = []

                if not buffered_texts and not buffered_attachments:
                    return

                try:
                    await self._execute_lead_pipeline(
                        session=session,
                        account=account,
                        account_uuid=account_uuid,
                        amo_lead_id=amo_lead_id,
                        account_id_str=account_id_str,
                        lead_id_str=lead_id_str,
                        access_token=access_token,
                        subdomain=subdomain,
                        buffered_texts=buffered_texts,
                        buffered_attachments=buffered_attachments
                    )
                except BaseException as pipe_err:
                    logger.warning(
                        f"Сбой в пайплайне лида {lead_id_str} ({type(pipe_err).__name__}). "
                        f"Восстановление сообщений в буфер Redis (CRITICAL-01)..."
                    )
                    try:
                        await asyncio.shield(
                            debounce_service.restore_buffered_messages(account_id_str, lead_id_str, buffered_data)
                        )
                    except Exception as res_err:
                        logger.error(f"Не удалось восстановить буфер для {account_id_str}:{lead_id_str}: {res_err}")
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
        buffered_attachments: List[Dict[str, Any]]
    ):
        # Скачиваем медиавложения (если есть)
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

        # Загружаем или создаем сделку
        lead_stmt = select(Lead).where(
            Lead.account_id == account_uuid,
            Lead.amo_lead_id == amo_lead_id
        )
        lead = (await session.execute(lead_stmt)).scalar_one_or_none()

        if not lead:
            lead = Lead(
                account_id=account_uuid,
                amo_lead_id=amo_lead_id,
                stuck_count=0,
                handover_required=False,
                last_message_at=datetime.now(timezone.utc)
            )
            session.add(lead)
            await session.flush()

        # Получаем актуальные данные сделки из amoCRM только при необходимости (IMPORTANT-15)
        enabled_pipeline_ids = [p.amo_pipeline_id for p in account.pipelines if p.is_enabled]
        has_enabled_mappings = any(fm.is_enabled for fm in account.field_mappings)

        lead_amo_data = None
        contact_id = None
        contact_amo_data = None
        if enabled_pipeline_ids or has_enabled_mappings:
            lead_amo_data = await amocrm_client.get_lead(subdomain, access_token, amo_lead_id)

            # Получаем связанный контакт для доступа к его полям
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

        if enabled_pipeline_ids and lead_amo_data:
            lead_pipeline_id = lead_amo_data.get("pipeline_id")
            if lead_pipeline_id and lead_pipeline_id not in enabled_pipeline_ids:
                logger.info(
                    f"Лид #{amo_lead_id} находится в воронке #{lead_pipeline_id}, "
                    f"которая не включена в настройках аккаунта ({enabled_pipeline_ids}). ИИ пропускает сообщение."
                )
                return

        # Если сделка уже переведена на оператора, ИИ не отвечает
        if lead.handover_required:
            logger.info(f"Лид {amo_lead_id} уже переведен на оператора (handover_required=True). Пропуск ответа ИИ.")
            return

        # Сохраняем сообщение пользователя
        user_msg = ConversationMessage(
            lead_id=lead.id,
            role="user",
            content=combined_user_text,
            created_at=datetime.now(timezone.utc)
        )
        session.add(user_msg)
        lead.last_message_at = datetime.now(timezone.utc)
        await session.commit()

        # Загружаем историю переписки (до 20 последних реплик)
        history_stmt = (
            select(ConversationMessage)
            .where(ConversationMessage.lead_id == lead.id)
            .order_by(ConversationMessage.created_at.desc())
            .limit(20)
        )
        db_messages = list((await session.execute(history_stmt)).scalars().all())
        recent_messages = list(reversed(db_messages))
        history_payload = [{"role": m.role, "content": m.content} for m in recent_messages]

        # -------------------------------------------------------------
        # Подготовка целевых полей (FieldMapping) и уже заполненных данных сделки
        # -------------------------------------------------------------
        amo_cf_values: Dict[int, str] = {}
        # Сначала поля контакта (приоритет у полей сделки, они перезапишут)
        if contact_amo_data:
            for cf in (contact_amo_data.get("custom_fields_values") or []):
                fid = cf.get("field_id")
                vals = cf.get("values", [])
                if fid and vals:
                    val_str = str(vals[0].get("value") or "").strip()
                    if val_str:
                        amo_cf_values[fid] = val_str
        # Поля сделки перезаписывают поля контакта при совпадении ID
        if lead_amo_data:
            for cf in (lead_amo_data.get("custom_fields_values") or []):
                fid = cf.get("field_id")
                vals = cf.get("values", [])
                if fid and vals:
                    val_str = str(vals[0].get("value") or "").strip()
                    if val_str:
                        amo_cf_values[fid] = val_str

        known_fields: Dict[str, str] = {}
        target_fields: List[Dict[str, str]] = []
        for fm in (account.field_mappings or []):
            if not fm.is_enabled:
                continue
            if fm.amo_field_id == account.ai_reply_field_id:
                continue

            if fm.amo_field_id in amo_cf_values:
                known_fields[fm.field_name] = amo_cf_values[fm.amo_field_id]
            else:
                target_fields.append({
                    "name": fm.field_name,
                    "hint": fm.ai_hint or f"Выяснить {fm.field_name}"
                })

        # Параметры ИИ
        ai_config = account.ai_config
        prompt = ai_config.communicator_prompt if ai_config else "Ты вежливый ИИ-менеджер."
        comm_model = ai_config.communicator_model if ai_config else "gemini-3.1-flash-lite"
        fallback_comm_model = ai_config.fallback_communicator_model if ai_config and ai_config.fallback_communicator_model else "gemini-2.5-flash"
        temperature = float(ai_config.temperature) if ai_config else 0.4
        handover_limit = ai_config.handover_after_stuck if ai_config else 4
        knowledge_base = ai_config.knowledge_base if ai_config else None
        knowledge_mode = ai_config.knowledge_mode if ai_config else "plain_text"
        gemini_cache_name = ai_config.gemini_cache_name if ai_config else None

        # -------------------------------------------------------------
        # 1. КРИТИЧЕСКИЙ ПУТЬ: Генерация ответа и отправка в мессенджер
        # -------------------------------------------------------------
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
            gemini_cache_name=gemini_cache_name
        )

        # Если Gemini распознал медиафайл и вернул выжимку/транскрипцию, обогащаем запись в БД
        if comm_resp.media_summary:
            if combined_user_text == "[Входящее медиасообщение]":
                user_msg.content = comm_resp.media_summary
            else:
                user_msg.content = f"{comm_resp.media_summary}\n{combined_user_text}"
            await session.commit()

        # Проверка условий Handover (явный запрос клиента или лимит застревания)
        is_stuck_threshold = lead.stuck_count >= handover_limit
        if comm_resp.is_handover_requested or is_stuck_threshold:
            logger.info(f"Инициирован Handover для лида {amo_lead_id} (запрос={comm_resp.is_handover_requested}, stuck={lead.stuck_count})")
            lead.handover_required = True
            await session.commit()

            # Создаем задачу оператору
            task_text = (
                "Клиент запросил оператора в чате."
                if comm_resp.is_handover_requested
                else f"ИИ не может решить вопрос клиента после {lead.stuck_count} попыток. Подключитесь к диалогу."
            )
            await amocrm_client.create_operator_task(
                subdomain=subdomain,
                access_token=access_token,
                element_id=amo_lead_id,
                text=task_text
            )

            # Уведомляем клиента вежливым сообщением
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
                asst_msg = ConversationMessage(
                    lead_id=lead.id,
                    role="assistant",
                    content=handover_notice,
                    created_at=datetime.now(timezone.utc)
                )
                session.add(asst_msg)
                await session.commit()
            return

        reply_text = comm_resp.text

        # Строгая последовательность:
        # 1) Запись в поле сделки (основное поле ответа ИИ)
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

        # Авто-восстановление поля ответа, если оно удалено или отсутствует в amoCRM
        if not patch_ok:
            logger.warning(
                f"Не удалось записать ответ ИИ в поле {reply_field_id} лида {amo_lead_id}. "
                "Возможно, поле удалили в amoCRM. Пытаемся автоматически восстановить поле 'AI: Ответ ассистента'..."
            )
            new_field_id = await amocrm_client.ensure_reply_field(subdomain, access_token)
            if new_field_id:
                account.ai_reply_field_id = new_field_id
                reply_field_id = new_field_id
                await session.commit()
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

        # 2) Запуск Salesbot (прочитает поле {{lead.cf.<id>}} и отправит в чат)
        bot_ok = await amocrm_client.run_salesbot(
            subdomain=subdomain,
            access_token=access_token,
            bot_id=account.bot_id,
            entity_id=amo_lead_id
        )

        if not bot_ok:
            logger.warning(f"Salesbot {account.bot_id} вернул ошибку при запуске для лида {amo_lead_id}")

        # Сохраняем ответ ассистента в БД
        asst_msg = ConversationMessage(
            lead_id=lead.id,
            role="assistant",
            content=reply_text,
            created_at=datetime.now(timezone.utc)
        )
        session.add(asst_msg)
        await session.commit()

        logger.info(f"Успешная доставка ответа ИИ лиду {amo_lead_id}: '{reply_text[:60]}...'")

        # -------------------------------------------------------------
        # 2. ЭКСТРАКТОР: Выполняется сразу следом, ПОКА ЕЩЕ АКТИВЕН ЛОК
        # -------------------------------------------------------------
        enabled_mappings = [fm for fm in account.field_mappings if fm.is_enabled]
        if enabled_mappings:
            full_dialog = history_payload + [{"role": "assistant", "content": reply_text}]
            ext_model = ai_config.extractor_model if ai_config else "gemini-3.1-flash-lite"
            fallback_ext_model = ai_config.fallback_extractor_model if ai_config and ai_config.fallback_extractor_model else "gemini-2.5-flash"
            ext_result = await extractor.extract_lead_fields(
                field_mappings=account.field_mappings,
                messages=full_dialog,
                current_lead_values=amo_cf_values,
                model_name=ext_model,
                fallback_model=fallback_ext_model,
                media_parts=media_parts
            )

            if ext_result.fields_to_update:
                logger.info(f"Экстрактор нашел поля для лида {amo_lead_id}: {ext_result.field_name_values}")

                # Разделяем поля по типу сущности: сделка vs контакт
                fm_entity_map = {fm.amo_field_id: getattr(fm, "entity_type", "lead") for fm in account.field_mappings}
                lead_fields_to_update = []
                contact_fields_to_update = []
                for item in ext_result.fields_to_update:
                    fid = item.get("field_id")
                    if fm_entity_map.get(fid, "lead") == "contact":
                        contact_fields_to_update.append(item)
                    else:
                        lead_fields_to_update.append(item)

                # Записываем поля сделки
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
                                logger.error(f"Поле #{fid} отклонено amoCRM (вероятно, удалено). Автоматически отключаем маппинг.")
                                for fm in account.field_mappings:
                                    if fm.amo_field_id == fid:
                                        fm.is_enabled = False
                        await session.commit()

                # Записываем поля контакта
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
                                    logger.error(f"Поле контакта #{fid} отклонено amoCRM. Отключаем маппинг.")
                                    for fm in account.field_mappings:
                                        if fm.amo_field_id == fid:
                                            fm.is_enabled = False
                            await session.commit()
                    else:
                        logger.warning(f"Нет contact_id для записи полей контакта в лиде #{amo_lead_id}.")

                # Логируем экстракцию
                ext_log = ExtractionLog(
                    lead_id=lead.id,
                    raw_response=ext_result.raw_response,
                    applied_fields=ext_result.field_name_values,
                    created_at=datetime.now(timezone.utc)
                )
                session.add(ext_log)
                lead.stuck_count = 0
                await session.commit()
            else:
                # Если данные не удалось извлечь и клиент пишет короткие/непонятные фразы (IMPORTANT-17)
                lead.stuck_count += 1
                await session.commit()


delivery_service = DeliveryService()
