import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import httpx

from app.models.account import Account, AccountStatus, FieldMapping
from app.models.lead import Lead
from app.services.amocrm_client import AmoCRMClient
from app.services.debounce_service import DebounceService
from app.services.delivery_service import DeliveryService
from app.services.gemini_client import gemini_http_client
from app.main import lifespan


class TestAuditFixesV2(unittest.IsolatedAsyncioTestCase):

    async def test_critical_01_restore_buffered_messages(self):
        """CRITICAL-01: restore_buffered_messages восстанавливает сообщения в начало очереди Redis (LPUSH)"""
        debounce = DebounceService()
        mock_redis = AsyncMock()
        debounce.redis = mock_redis

        buffered_data = {
            "texts": ["Привет", "Сколько стоит?"],
            "attachments": [{"link": "https://example.com/audio.ogg", "type": "audio"}]
        }

        await debounce.restore_buffered_messages("acc1", "lead10", buffered_data)
        mock_redis.lpush.assert_called_once()
        args, kwargs = mock_redis.lpush.call_args
        self.assertEqual(args[0], "debounce_msgs:acc1:lead10")
        # Должно восстановить 3 элемента
        self.assertEqual(len(args[1:]), 3)
        mock_redis.expire.assert_called_with("debounce_msgs:acc1:lead10", 300)

    async def test_critical_02_stream_attachment_content_length_limit(self):
        """CRITICAL-02: download_attachment отсекает файлы с Content-Length превышающим лимит до чтения тела"""
        client = AmoCRMClient()
        mock_client = MagicMock()
        mock_client.is_closed = False
        client._client = mock_client

        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=MagicMock(
            status_code=200,
            headers={"content-length": str(30 * 1024 * 1024)}  # 30 МБ > 20 МБ
        ))
        mock_stream.__aexit__ = AsyncMock(return_value=None)
        mock_client.stream = MagicMock(return_value=mock_stream)

        res = await client.download_attachment("https://example.amocrm.ru/files/big.mp4")
        self.assertIsNone(res)

    async def test_critical_02_stream_attachment_chunk_limit(self):
        """CRITICAL-02: download_attachment прерывает чтение, если суммарный размер чанков превысил лимит"""
        client = AmoCRMClient()
        mock_client = MagicMock()
        mock_client.is_closed = False
        client._client = mock_client

        async def _iter_chunks(*args, **kwargs):
            yield b"x" * 60000
            yield b"x" * 60000  # total 120KB > 100KB limit

        mock_resp = MagicMock(
            status_code=200,
            headers={},
            aiter_bytes=_iter_chunks
        )
        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream.__aexit__ = AsyncMock(return_value=None)
        mock_client.stream = MagicMock(return_value=mock_stream)

        res = await client.download_attachment("https://example.amocrm.ru/files/audio.ogg", max_size_bytes=100000)
        self.assertIsNone(res)

    async def test_critical_04_amocrm_network_error_handling(self):
        """CRITICAL-04: AmoCRMClient перехватывает сетевые ошибки httpx и не падает с необработанным исключением"""
        client = AmoCRMClient()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        client._client = mock_client

        mock_client.get.side_effect = httpx.ConnectError("Connection refused")
        res = await client.validate_token("sub", "tok")
        self.assertFalse(res["is_valid"])
        self.assertIn("Сетевая ошибка", res["error"])

        field_res = await client.ensure_reply_field("sub", "tok")
        self.assertIsNone(field_res)

        bots = await client.list_bots("sub", "tok")
        self.assertEqual(bots, [])

    async def test_important_08_amocrm_client_lock(self):
        """IMPORTANT-08: Инициализация и закрытие get_client() защищены блокировкой"""
        client = AmoCRMClient()
        c1 = await client.get_client()
        c2 = await client.get_client()
        self.assertIs(c1, c2)
        await client.close()
        self.assertIsNone(client._client)

    async def test_important_09_debounce_background_tasks_tracked(self):
        """IMPORTANT-09: DebounceService сохраняет сильную ссылку на таску в _background_tasks"""
        debounce = DebounceService()

        executed = False
        async def dummy_cb(acc, lead):
            nonlocal executed
            executed = True

        debounce.schedule_debounce("acc1", "lead1", dummy_cb, delay=0.01)
        await asyncio.sleep(0.05)
        self.assertTrue(executed)
        # Таска должна завершиться и удалиться из _background_tasks
        self.assertEqual(len(debounce._background_tasks), 0)

    async def test_important_13_gemini_client_pooling(self):
        """IMPORTANT-13: GeminiHTTPClient повторно использует один и тот же AsyncClient"""
        c1 = await gemini_http_client.get_client()
        c2 = await gemini_http_client.get_client()
        self.assertIs(c1, c2)
        self.assertFalse(c1.is_closed)

    async def test_important_15_skip_get_lead_when_not_needed(self):
        """IMPORTANT-15: get_lead не вызывается, если нет включенных воронок и нет экстрактора"""
        delivery = DeliveryService()
        account_uuid = uuid.uuid4()

        mock_account = MagicMock(spec=Account)
        mock_account.id = account_uuid
        mock_account.subdomain = "testsub"
        mock_account.is_active = True
        mock_account.status = AccountStatus.VERIFIED
        mock_account.last_error = None
        mock_account.encrypted_token = b"enc"
        mock_account.ai_reply_field_id = 999
        mock_account.bot_id = 888
        mock_account.pipelines = []  # Нет воронок
        mock_account.field_mappings = []  # Нет экстрактора
        mock_account.ai_config = None

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_account
        mock_session.execute.return_value = mock_result

        with patch("app.services.delivery_service.AsyncSessionLocal") as mock_session_local, \
             patch("app.services.delivery_service.amocrm_client.get_lead") as mock_get_lead, \
             patch("app.services.delivery_service.decrypt_token", return_value="tok"), \
             patch("app.services.delivery_service.debounce_service.pop_buffered_messages", return_value={"texts": ["Привет"], "attachments": []}), \
             patch.object(delivery, "_execute_lead_pipeline", new_callable=AsyncMock) as mock_exec:

            mock_session_local.return_value.__aenter__.return_value = mock_session
            await delivery._process_lead_pipeline(account_uuid, 123, str(account_uuid), "123")

            # _execute_lead_pipeline вызван
            mock_exec.assert_called_once()
            # amocrm_client.get_lead НЕ должен вызываться
            mock_get_lead.assert_not_called()

    async def test_important_17_extractor_increments_stuck_count_on_no_fields(self):
        """IMPORTANT-17: При отсутствии извлеченных полей lead.stuck_count инкрементируется"""
        delivery = DeliveryService()
        mock_lead = MagicMock(spec=Lead)
        mock_lead.id = uuid.uuid4()
        mock_lead.stuck_count = 2
        mock_lead.handover_required = False

        mock_fm = MagicMock(spec=FieldMapping)
        mock_fm.is_enabled = True
        mock_fm.amo_field_id = 100
        mock_fm.field_name = "Бюджет"

        mock_account = MagicMock(spec=Account)
        mock_account.field_mappings = [mock_fm]
        mock_account.pipelines = []
        mock_account.ai_reply_field_id = 999
        mock_account.bot_id = 888
        mock_account.ai_config = None

        mock_ext_result = MagicMock()
        mock_ext_result.fields_to_update = []  # Ничего не найдено!

        mock_res = MagicMock()
        mock_res.scalars.return_value.all.return_value = []
        mock_res.scalar_one_or_none.return_value = mock_lead

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.execute = AsyncMock(return_value=mock_res)

        with patch("app.services.delivery_service.extractor.extract_lead_fields", return_value=mock_ext_result), \
             patch("app.services.delivery_service.communicator.generate_reply") as mock_gen, \
             patch("app.services.delivery_service.amocrm_client.get_lead", return_value={}), \
             patch("app.services.delivery_service.amocrm_client.patch_lead_custom_fields", return_value=True), \
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

            self.assertEqual(mock_lead.stuck_count, 3)

    async def test_important_20_kommo_domain_support(self):
        """IMPORTANT-20: Поддержка доменов .kommo.com и .amocrm.com с защитой от недоверенных хостов"""
        client = AmoCRMClient()
        self.assertEqual(client._base_url("mycompany"), "https://mycompany.amocrm.ru")
        self.assertEqual(client._base_url("mycompany.kommo.com"), "https://mycompany.kommo.com")
        self.assertEqual(client._base_url("mycompany.amocrm.com"), "https://mycompany.amocrm.com")
        # Недоверенный домен безопасно нормализуется в amocrm.ru без SSRF
        self.assertEqual(client._base_url("hack.example"), "https://hack.amocrm.ru")

    async def test_important_07_10_lifespan_fail_fast(self):
        """IMPORTANT-07 & 10: Fail-fast при сбое подключения к PostgreSQL или Redis при старте"""
        mock_app = MagicMock()

        # Тест падения при нерабочем PostgreSQL
        with patch("app.main.AsyncSessionLocal") as mock_session_local:
            mock_session = AsyncMock()
            mock_session.execute.side_effect = ConnectionRefusedError("DB is down")
            mock_session_local.return_value.__aenter__.return_value = mock_session

            with self.assertRaises(RuntimeError) as ctx:
                async with lifespan(mock_app):
                    pass
            self.assertIn("PostgreSQL", str(ctx.exception))

        # Тест падения при нерабочем Redis
        with patch("app.main.AsyncSessionLocal") as mock_session_local, \
             patch("app.main.debounce_service.get_redis") as mock_get_redis:

            mock_session = AsyncMock()
            mock_session_local.return_value.__aenter__.return_value = mock_session

            mock_redis = AsyncMock()
            mock_redis.ping.side_effect = ConnectionRefusedError("Redis is down")
            mock_get_redis.return_value = mock_redis

            with self.assertRaises(RuntimeError) as ctx:
                async with lifespan(mock_app):
                    pass
            self.assertIn("Redis", str(ctx.exception))
