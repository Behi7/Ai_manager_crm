import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from app.models.account import Account, AccountStatus, Pipeline, FieldMapping
from app.models.lead import Lead, ConversationMessage
from app.services.delivery_service import DeliveryService
from app.api.routes_accounts import get_account_fields, sync_account_with_amocrm, delete_account_field_mapping


class TestSyncAndResilience(unittest.IsolatedAsyncioTestCase):

    async def test_delivery_reply_field_autorecovery(self):
        """Проверка авто-восстановления поля ответа ИИ при его удалении из amoCRM"""
        delivery = DeliveryService()

        mock_lead = MagicMock(spec=Lead)
        mock_lead.id = uuid.uuid4()
        mock_lead.stuck_count = 0
        mock_lead.handover_required = False

        mock_account = MagicMock(spec=Account)
        mock_account.field_mappings = []
        mock_account.pipelines = []
        mock_account.ai_reply_field_id = 999  # Старое (удаленное) поле
        mock_account.bot_id = 888
        mock_account.ai_config = None
        mock_account.subdomain = "testsub"

        mock_res = MagicMock()
        mock_res.scalars.return_value.all.return_value = []
        mock_res.scalar_one_or_none.return_value = mock_lead

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.execute = AsyncMock(return_value=mock_res)
        mock_session.commit = AsyncMock()

        mock_ext_result = MagicMock()
        mock_ext_result.fields_to_update = []

        # Первый вызов patch_lead_custom_fields вернет False (поле удалено в CRM),
        # затем ensure_reply_field вернет 1001,
        # второй вызов patch_lead_custom_fields вернет True
        patch_calls = []

        async def fake_patch(*args, **kwargs):
            fields = kwargs.get("fields", [])
            patch_calls.append(fields)
            if fields and fields[0].get("field_id") == 999:
                return False  # Ошибка записи в удаленное поле
            return True  # Успех в новое поле 1001

        with patch("app.services.delivery_service.extractor.extract_lead_fields", return_value=mock_ext_result), \
             patch("app.services.delivery_service.communicator.generate_reply") as mock_gen, \
             patch("app.services.delivery_service.amocrm_client.get_lead", return_value={}), \
             patch("app.services.delivery_service.amocrm_client.patch_lead_custom_fields", side_effect=fake_patch), \
             patch("app.services.delivery_service.amocrm_client.ensure_reply_field", return_value=1001) as mock_ensure, \
             patch("app.services.delivery_service.amocrm_client.run_salesbot", return_value=True) as mock_run_bot:

            mock_comm_resp = MagicMock()
            mock_comm_resp.text = "Здравствуйте! Это ответ от ИИ."
            mock_comm_resp.is_handover_requested = False
            mock_comm_resp.media_summary = None
            mock_gen.return_value = mock_comm_resp

            await delivery._execute_lead_pipeline(
                session=mock_session,
                account=mock_account,
                account_uuid=uuid.uuid4(),
                amo_lead_id=123,
                account_id_str="acc1",
                lead_id_str="123",
                access_token="tok",
                subdomain="testsub",
                buffered_texts=["Привет"],
                buffered_attachments=[]
            )

            # Проверяем, что ensure_reply_field был вызван для пересоздания поля
            mock_ensure.assert_called_once_with("testsub", "tok")
            # Проверяем, что ID поля у аккаунта обновился на новый 1001
            self.assertEqual(mock_account.ai_reply_field_id, 1001)
            # Проверяем, что бот был запущен после успешного авто-восстановления
            mock_run_bot.assert_called_once()

    async def test_delivery_extractor_fallback_disables_deleted_field(self):
        """Проверка изоляции ошибок Экстрактора: удаленное поле отключается, остальные сохраняются"""
        delivery = DeliveryService()

        mock_lead = MagicMock(spec=Lead)
        mock_lead.id = uuid.uuid4()
        mock_lead.stuck_count = 0
        mock_lead.handover_required = False

        fm_valid = MagicMock(spec=FieldMapping)
        fm_valid.amo_field_id = 100
        fm_valid.is_enabled = True

        fm_deleted = MagicMock(spec=FieldMapping)
        fm_deleted.amo_field_id = 200
        fm_deleted.is_enabled = True

        mock_account = MagicMock(spec=Account)
        mock_account.field_mappings = [fm_valid, fm_deleted]
        mock_account.pipelines = []
        mock_account.ai_reply_field_id = 999
        mock_account.bot_id = 888
        mock_account.ai_config = None
        mock_account.subdomain = "testsub"

        mock_res = MagicMock()
        mock_res.scalars.return_value.all.return_value = []
        mock_res.scalar_one_or_none.return_value = mock_lead

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.execute = AsyncMock(return_value=mock_res)
        mock_session.commit = AsyncMock()

        mock_ext_result = MagicMock()
        mock_ext_result.fields_to_update = [
            {"field_id": 100, "values": [{"value": "Клиент"}]},
            {"field_id": 200, "values": [{"value": "Удаленное поле"}]}
        ]
        mock_ext_result.field_name_values = {"Имя": "Клиент", "Удаленное": "val"}

        async def fake_patch(*args, **kwargs):
            fields = kwargs.get("fields", [])
            # Если пакетный запрос (len > 1) -> фейлим (amoCRM 400 validation error)
            if len(fields) > 1:
                return False
            # Если поштучный запрос
            fid = fields[0].get("field_id")
            if fid == 999:
                return True  # Запись ответа успешна
            if fid == 100:
                return True  # Поле 100 валидно
            if fid == 200:
                return False  # Поле 200 удалено в CRM
            return True

        with patch("app.services.delivery_service.extractor.extract_lead_fields", return_value=mock_ext_result), \
             patch("app.services.delivery_service.communicator.generate_reply") as mock_gen, \
             patch("app.services.delivery_service.amocrm_client.get_lead", return_value={}), \
             patch("app.services.delivery_service.amocrm_client.patch_lead_custom_fields", side_effect=fake_patch), \
             patch("app.services.delivery_service.amocrm_client.run_salesbot", return_value=True):

            mock_comm_resp = MagicMock()
            mock_comm_resp.text = "Здравствуйте!"
            mock_comm_resp.is_handover_requested = False
            mock_comm_resp.media_summary = None
            mock_gen.return_value = mock_comm_resp

            await delivery._execute_lead_pipeline(
                session=mock_session,
                account=mock_account,
                account_uuid=uuid.uuid4(),
                amo_lead_id=123,
                account_id_str="acc1",
                lead_id_str="123",
                access_token="tok",
                subdomain="testsub",
                buffered_texts=["Привет"],
                buffered_attachments=[]
            )

            # Валидное поле 100 осталось активным
            self.assertTrue(fm_valid.is_enabled)
            # Удаленное поле 200 было автоматически отключено
            self.assertFalse(fm_deleted.is_enabled)

    async def test_get_account_fields_detects_deleted_in_amo(self):
        """Проверка того, что get_account_fields отключает удаленные поля и ставит флаг is_deleted_in_amo"""
        acc_id = uuid.uuid4()
        mock_account = MagicMock(spec=Account)
        mock_account.id = acc_id
        mock_account.subdomain = "testsub"
        mock_account.encrypted_token = "mock_enc"
        mock_account.ai_reply_field_id = 999

        fm1 = MagicMock(spec=FieldMapping)
        fm1.id = uuid.uuid4()
        fm1.amo_field_id = 100
        fm1.field_name = "Имя"
        fm1.field_type = "text"
        fm1.is_enabled = True
        fm1.ai_hint = "Имя клиента"
        fm1.overwrite_if_filled = False

        fm2 = MagicMock(spec=FieldMapping)
        fm2.id = uuid.uuid4()
        fm2.amo_field_id = 200
        fm2.field_name = "Удаленное поле"
        fm2.field_type = "text"
        fm2.is_enabled = True
        fm2.ai_hint = "Подсказка"
        fm2.overwrite_if_filled = False

        mock_account.field_mappings = [fm1, fm2]

        mock_session = AsyncMock()
        res1 = MagicMock()
        res1.scalar_one_or_none.return_value = mock_account

        res2 = MagicMock()
        res2.scalars.return_value.all.return_value = [fm1, fm2]

        mock_session.execute = AsyncMock(side_effect=[res1, res2])
        mock_session.commit = AsyncMock()

        # amoCRM возвращает только поле 100 (поле 200 удалено)
        amo_fields_resp = [{"id": 100, "name": "Имя", "type": "text", "entity_type": "lead"}]

        with patch("app.api.routes_accounts.AsyncSessionLocal", return_value=mock_session), \
             patch("app.api.routes_accounts.decrypt_token", return_value="plain_tok"), \
             patch("app.api.routes_accounts.amocrm_client.list_custom_fields", return_value=amo_fields_resp), \
             patch("app.api.routes_accounts.amocrm_client.list_contact_custom_fields", return_value=[]):

            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)

            result = await get_account_fields(acc_id)

            self.assertEqual(len(result), 2)
            # fm2 отключено в БД
            self.assertFalse(fm2.is_enabled)
            # fm1 не удалено
            self.assertFalse(result[0]["is_deleted_in_amo"])
            # fm2 помечено как удаленное
            self.assertTrue(result[1]["is_deleted_in_amo"])

    async def test_sync_account_endpoint(self):
        """Проверка полного эндпоинта синхронизации POST /accounts/{account_id}/sync"""
        acc_id = uuid.uuid4()
        mock_account = MagicMock(spec=Account)
        mock_account.id = acc_id
        mock_account.subdomain = "testsub"
        mock_account.name = "Старое имя"
        mock_account.encrypted_token = "mock_enc"
        mock_account.ai_reply_field_id = 999
        mock_account.bot_id = 55
        mock_account.status = AccountStatus.ERROR
        mock_account.last_error = "Какая-то ошибка"
        mock_account.is_active = True
        mock_account.webhook_verified = True

        pipe1 = MagicMock(spec=Pipeline)
        pipe1.amo_pipeline_id = 1
        pipe1.name = "Воронка 1"
        pipe1.is_enabled = True

        pipe2 = MagicMock(spec=Pipeline)
        pipe2.amo_pipeline_id = 2
        pipe2.name = "Удаленная воронка"
        pipe2.is_enabled = True

        fm1 = MagicMock(spec=FieldMapping)
        fm1.amo_field_id = 10
        fm1.field_name = "Поле 1"
        fm1.is_enabled = True

        fm2 = MagicMock(spec=FieldMapping)
        fm2.amo_field_id = 20
        fm2.field_name = "Удаленное поле"
        fm2.is_enabled = True

        mock_account.pipelines = [pipe1, pipe2]
        mock_account.field_mappings = [fm1, fm2]
        mock_account.ai_config = None

        mock_session = AsyncMock()
        res = MagicMock()
        res.scalar_one_or_none.return_value = mock_account
        mock_session.execute = AsyncMock(return_value=res)
        mock_session.commit = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        token_valid_resp = {"is_valid": True, "account_name": "Новое Имя Компании", "account_id": 12345}
        amo_pipelines = [{"id": 1, "name": "Воронка 1 (обновлено)"}]
        amo_fields = [{"id": 10, "name": "Поле 1", "type": "text"}]
        amo_bots = [{"id": 55, "name": "Salesbot AI"}]

        with patch("app.api.routes_accounts.AsyncSessionLocal", return_value=mock_session), \
             patch("app.api.routes_accounts.decrypt_token", return_value="plain_tok"), \
             patch("app.api.routes_accounts.amocrm_client.validate_token", return_value=token_valid_resp), \
             patch("app.api.routes_accounts.amocrm_client.ensure_reply_field", return_value=999), \
             patch("app.api.routes_accounts.amocrm_client.list_pipelines", return_value=amo_pipelines), \
             patch("app.api.routes_accounts.amocrm_client.list_custom_fields", return_value=amo_fields), \
             patch("app.api.routes_accounts.amocrm_client.list_bots", return_value=amo_bots), \
             patch("app.api.routes_accounts.debounce_service.get_redis") as mock_get_redis:

            mock_redis = AsyncMock()
            mock_get_redis.return_value = mock_redis

            resp = await sync_account_with_amocrm(acc_id)

            self.assertTrue(resp["success"])
            self.assertEqual(resp["account"]["name"], "Новое Имя Компании")
            self.assertEqual(resp["account"]["deleted_fields_disabled"], 1)
            self.assertEqual(mock_account.status, AccountStatus.VERIFIED)
            self.assertIsNone(mock_account.last_error)

            # Проверяем, что удаленная воронка pipe2 и удаленное поле fm2 были выключены
            self.assertFalse(pipe2.is_enabled)
            self.assertTrue(pipe1.is_enabled)
            self.assertFalse(fm2.is_enabled)
            self.assertTrue(fm1.is_enabled)

    async def test_delete_account_field_mapping(self):
        """Проверка удаления маппинга поля через DELETE /accounts/{account_id}/fields/{amo_field_id}"""
        acc_id = uuid.uuid4()
        mock_fm = MagicMock(spec=FieldMapping)
        mock_fm.amo_field_id = 755179

        mock_session = AsyncMock()
        res = MagicMock()
        res.scalar_one_or_none.return_value = mock_fm
        mock_session.execute = AsyncMock(return_value=res)
        mock_session.delete = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("app.api.routes_accounts.AsyncSessionLocal", return_value=mock_session):
            resp = await delete_account_field_mapping(acc_id, 755179)
            self.assertTrue(resp["success"])
            mock_session.delete.assert_called_once_with(mock_fm)
            mock_session.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()


