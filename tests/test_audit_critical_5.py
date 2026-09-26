import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from app.models.lead import Lead
from app.models.account import Account, FieldMapping
from app.services.amocrm_client import AmoCRMClient, amocrm_client
from app.services.delivery_service import DeliveryService
from app.services.llm_communicator import LLMCommunicator


class TestAuditCritical5(unittest.IsolatedAsyncioTestCase):
    def test_lead_relationships_use_noload(self):
        """Пункт 3: Связи messages и extraction_logs в модели Lead должны иметь lazy='noload'"""
        self.assertEqual(Lead.messages.property.lazy, "noload")
        self.assertEqual(Lead.extraction_logs.property.lazy, "noload")

    async def test_transient_503_does_not_disable_field_mapping(self):
        """Пункт 1: При временной ошибке (503/429/timeout) поле НЕ отключается в БД"""
        delivery = DeliveryService()
        fm_valid = MagicMock(spec=FieldMapping)
        fm_valid.amo_field_id = 100
        fm_valid.entity_type = "lead"
        fm_valid.is_enabled = True

        mock_account = MagicMock(spec=Account)
        mock_account.field_mappings = [fm_valid]
        mock_account.ai_config = None

        mock_res = MagicMock()
        mock_res.scalars.return_value.all.return_value = []
        mock_res.scalar_one_or_none.return_value = MagicMock()
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.execute = AsyncMock(return_value=mock_res)
        mock_session.commit = AsyncMock()

        mock_ext_res = MagicMock()
        mock_ext_res.fields_to_update = [{"field_id": 100, "entity_type": "lead", "values": [{"value": "Тест"}]}]
        mock_ext_res.field_name_values = {"Поле": "Тест"}
        mock_ext_res.raw_response = {"field_lead_100": "Тест"}
        mock_ext_res.error = None

        async def fake_patch_503(*args, **kwargs):
            amocrm_client.last_status_code = 503
            amocrm_client.last_error_transient = True
            return False

        try:
            with patch("app.services.delivery_service.extractor.extract_lead_fields", return_value=mock_ext_res),                  patch("app.services.delivery_service.amocrm_client.patch_lead_custom_fields", side_effect=fake_patch_503):
                await delivery._run_extractor_step(
                    session=mock_session,
                    account=mock_account,
                    account_uuid=uuid.uuid4(),
                    amo_lead_id=123,
                    lead=MagicMock(),
                    lead_db_id=uuid.uuid4(),
                    current_stuck_count=0,
                    subdomain="testsub",
                    access_token="tok",
                    ai_config=None,
                    history_payload=[],
                    reply_text="",
                    amo_cf_values={},
                    media_parts=None,
                    contact_id=None,
                    contact_amo_data=None,
                )
                self.assertTrue(fm_valid.is_enabled)
        finally:
            amocrm_client.last_status_code = 400
            amocrm_client.last_error_transient = False

    async def test_salesbot_failure_raises_and_creates_operator_task(self):
        """Пункт 2: Если run_salesbot вернул False, ставится задача оператору и выбрасывается RuntimeError"""
        delivery = DeliveryService()
        mock_lead = MagicMock()
        mock_lead.id = uuid.uuid4()
        mock_lead.stuck_count = 0
        mock_lead.handover_required = False

        mock_account = MagicMock(spec=Account)
        mock_account.field_mappings = []
        mock_account.pipelines = []
        mock_account.ai_reply_field_id = 999
        mock_account.bot_id = 777
        mock_account.ai_config = None
        mock_account.subdomain = "testsub"

        mock_res = MagicMock()
        mock_res.scalars.return_value.all.return_value = []
        mock_res.scalar_one_or_none.return_value = mock_lead

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.execute = AsyncMock(return_value=mock_res)
        mock_session.commit = AsyncMock()

        mock_comm_resp = MagicMock()
        mock_comm_resp.text = "Ответ клиенту"
        mock_comm_resp.is_handover_requested = False
        mock_comm_resp.media_summary = None

        with patch("app.services.delivery_service.communicator.generate_reply", return_value=mock_comm_resp),              patch("app.services.delivery_service.amocrm_client.get_lead", return_value={}),              patch("app.services.delivery_service.amocrm_client.patch_lead_custom_fields", return_value=True),              patch("app.services.delivery_service.amocrm_client.run_salesbot", return_value=False),              patch("app.services.delivery_service.amocrm_client.create_operator_task", new_callable=AsyncMock) as mock_task:

            with self.assertRaises(RuntimeError):
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
                    buffered_attachments=[],
                )
            mock_task.assert_awaited_once()

    async def test_gemini_api_key_sent_in_header_not_params(self):
        """Пункт 4: GEMINI_API_KEY передаётся в заголовке x-goog-api-key, а не в URL params"""
        comm = LLMCommunicator(api_key="secret_gemini_key_123")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"candidates": [{"content": {"parts": [{"text": "Привет!"}]}}]}
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp

        with patch("app.services.llm_communicator.gemini_http_client.get_client", new_callable=AsyncMock, return_value=mock_client):
            await comm.generate_reply(system_prompt="Тест", messages=[{"role": "user", "content": "Привет"}])
            call_kwargs = mock_client.post.call_args.kwargs
            self.assertNotIn("params", call_kwargs)
            self.assertEqual(call_kwargs["headers"]["x-goog-api-key"], "secret_gemini_key_123")

    async def test_download_attachment_blocks_redirect_to_localhost(self):
        """Пункт 5: download_attachment блокирует HTTP 302 редирект на внутренний адрес 127.0.0.1"""
        client_instance = AmoCRMClient()
        mock_stream_resp = MagicMock()
        mock_stream_resp.status_code = 302
        mock_stream_resp.headers = {"location": "http://127.0.0.1:8080/health"}

        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_stream_resp
        mock_cm.__aexit__.return_value = None

        mock_http = MagicMock()
        mock_http.stream.return_value = mock_cm

        with patch.object(client_instance, "get_client", new_callable=AsyncMock, return_value=mock_http),              patch("asyncio.base_events.BaseEventLoop.getaddrinfo", new_callable=AsyncMock, return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            res = await client_instance.download_attachment("https://example.com/file.jpg")
            self.assertIsNone(res)
            self.assertFalse(mock_http.stream.call_args.kwargs.get("follow_redirects", True))

    async def test_extractor_catches_up_buffered_messages_before_communicator(self):
        """Проверка: если во время работы Экстрактора в буфер падает 2-е сообщение ('fargonada'),
        Экстрактор запускается повторно по склеенному тексту, а Общитель запускается ровно 1 раз со всеми полями"""
        delivery = DeliveryService()

        fm_service = MagicMock(spec=FieldMapping)
        fm_service.amo_field_id = 101
        fm_service.field_name = "Услуга"
        fm_service.entity_type = "lead"
        fm_service.is_enabled = True
        fm_service.ai_hint = "Какая услуга"

        fm_city = MagicMock(spec=FieldMapping)
        fm_city.amo_field_id = 102
        fm_city.field_name = "Город"
        fm_city.entity_type = "lead"
        fm_city.is_enabled = True
        fm_city.ai_hint = "В каком городе"

        mock_account = MagicMock(spec=Account)
        mock_account.field_mappings = [fm_service, fm_city]
        mock_account.pipelines = []
        mock_account.ai_reply_field_id = 999
        mock_account.bot_id = 777
        mock_account.ai_config = None
        mock_account.subdomain = "testsub"

        mock_lead = MagicMock()
        mock_lead.id = uuid.uuid4()
        mock_lead.stuck_count = 0
        mock_lead.handover_required = False

        mock_user_msg = MagicMock()
        mock_user_msg.id = 1
        mock_user_msg.role = "user"
        mock_user_msg.content = "men sotuv bo'limi qurib call center qilmoqchiman"

        mock_res = MagicMock()
        mock_res.scalars.return_value.all.return_value = [mock_user_msg]
        mock_res.scalar_one_or_none.return_value = mock_lead

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.execute = AsyncMock(return_value=mock_res)
        mock_session.commit = AsyncMock()

        ext_res_1 = MagicMock()
        ext_res_1.fields_to_update = [{"field_id": 101, "entity_type": "lead", "values": [{"value": "call center"}]}]
        ext_res_1.field_name_values = {"Услуга": "call center"}
        ext_res_1.raw_response = {"field_lead_101": "call center"}
        ext_res_1.error = None

        ext_res_2 = MagicMock()
        ext_res_2.fields_to_update = [{"field_id": 102, "entity_type": "lead", "values": [{"value": "fargonada"}]}]
        ext_res_2.field_name_values = {"Город": "fargonada"}
        ext_res_2.raw_response = {"field_lead_102": "fargonada"}
        ext_res_2.error = None

        mock_comm_resp = MagicMock()
        mock_comm_resp.text = "Отлично, настраиваем колл-центры и в Фергане! Сколько сотрудников?"
        mock_comm_resp.is_handover_requested = False
        mock_comm_resp.media_summary = None

        with patch("app.services.delivery_service.extractor.extract_lead_fields", side_effect=[ext_res_1, ext_res_2]) as mock_ext,              patch("app.services.delivery_service.debounce_service.has_buffered_messages", side_effect=[True, False]),              patch("app.services.delivery_service.debounce_service.pop_buffered_messages", return_value={"texts": ["fargonada"], "attachments": []}),              patch("app.services.delivery_service.communicator.generate_reply", return_value=mock_comm_resp) as mock_comm,              patch("app.services.delivery_service.amocrm_client.get_lead", return_value={}),              patch("app.services.delivery_service.amocrm_client.patch_lead_custom_fields", return_value=True),              patch("app.services.delivery_service.amocrm_client.run_salesbot", return_value=True) as mock_bot:

            await delivery._execute_lead_pipeline(
                session=mock_session,
                account=mock_account,
                account_uuid=uuid.uuid4(),
                amo_lead_id=38409053,
                account_id_str="acc1",
                lead_id_str="38409053",
                access_token="tok",
                subdomain="testsub",
                buffered_texts=["men sotuv bo'limi qurib call center qilmoqchiman"],
                buffered_attachments=[],
            )

            # Экстрактор вызвался 2 раза (сначала по 1-му сообщению, затем по склеенным 1+2)
            self.assertEqual(mock_ext.await_count, 2)
            # Общитель вызвался РОВНО 1 РАЗ и получил ОБА поля в known_fields!
            self.assertEqual(mock_comm.await_count, 1)
            comm_kwargs = mock_comm.call_args.kwargs
            self.assertEqual(comm_kwargs["known_fields"], {"Услуга": "call center", "Город": "fargonada"})
            self.assertEqual(comm_kwargs["target_fields"], [])
            self.assertIn("fargonada", comm_kwargs["messages"][-1]["content"])
            # И Salesbot запустился ровно 1 раз
            self.assertEqual(mock_bot.await_count, 1)

    async def test_download_attachment_without_hint_filename_does_not_crash(self):
        """Проверка: download_attachment без hint_filename корректно берет имя файла из URL без NameError"""
        client = AmoCRMClient()
        mock_stream_resp = MagicMock()
        mock_stream_resp.status_code = 200
        mock_stream_resp.headers = {"content-type": "audio/ogg"}
        mock_stream_resp.__aenter__ = AsyncMock(return_value=mock_stream_resp)
        mock_stream_resp.__aexit__ = AsyncMock(return_value=None)

        async def _fake_bytes(chunk_size=65536):
            yield b"OggS\x00\x02\x00\x00\x00\x00\x00\x00"

        mock_stream_resp.aiter_bytes = _fake_bytes
        mock_http = MagicMock()
        mock_http.stream = MagicMock(return_value=mock_stream_resp)

        with patch.object(client, "get_client", new=AsyncMock(return_value=mock_http)), \
             patch("app.services.amocrm_client.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]):
            res = await client.download_attachment(
                url="https://cdn.amocrm.ru/attachments/voice_note_123.ogg",
                hint_filename="",
                hint_type="voice",
            )
            self.assertIsNotNone(res)
            self.assertEqual(res["file_name"], "voice_note_123.ogg")
            self.assertEqual(res["mime_type"], "audio/ogg")

    async def test_secret_encryption_and_contextvar_isolation(self):
        """Проверка шифрования/маскирования gemini_api_key и изоляции last_status_code между корутинами"""
        import asyncio
        from app.core.security import encrypt_secret_str, decrypt_secret_str, mask_secret_str

        raw_key = "AIzaSyTestKey1234567890"
        enc = encrypt_secret_str(raw_key)
        self.assertTrue(enc.startswith("enc:v1:"))
        self.assertEqual(decrypt_secret_str(enc), raw_key)
        self.assertEqual(mask_secret_str(enc), "***7890")
        # Обратная совместимость с открытой строкой
        self.assertEqual(decrypt_secret_str(raw_key), raw_key)

        client = AmoCRMClient()
        results = {}

        async def worker_a():
            client.last_status_code = 503
            await asyncio.sleep(0.02)
            results["a"] = client.last_status_code

        async def worker_b():
            await asyncio.sleep(0.01)
            client.last_status_code = 400
            results["b"] = client.last_status_code

        await asyncio.gather(worker_a(), worker_b())
        self.assertEqual(results["a"], 503)
        self.assertEqual(results["b"], 400)

