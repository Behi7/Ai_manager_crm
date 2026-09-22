import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from app.core.config import settings
from app.models.account import Account, FieldMapping, AccountStatus
from app.services.amocrm_client import AmoCRMClient
from app.services.debounce_service import DebounceService
from app.services.llm_extractor import LLMExtractor


class TestAuditFixes(unittest.IsolatedAsyncioTestCase):
    async def test_c2_extractor_respects_overwrite_if_filled_false(self):
        """
        Тест C-2: Проверка, что при передаче current_lead_values экстрактор НЕ перезаписывает
        поля с overwrite_if_filled=False, если значение в amoCRM уже заполнено.
        """
        extractor = LLMExtractor(api_key="test_key")
        fm1 = FieldMapping(
            amo_field_id=101,
            field_name="Бюджет",
            field_type="numeric",
            is_enabled=True,
            overwrite_if_filled=False
        )
        fm2 = FieldMapping(
            amo_field_id=102,
            field_name="Город",
            field_type="text",
            is_enabled=True,
            overwrite_if_filled=True
        )

        current_values = {
            101: "50000",  # Уже заполнено, перезапись ЗАПРЕЩЕНА
            102: "Москва"   # Уже заполнено, перезапись РАЗРЕШЕНА
        }

        # Мокаем ответ Gemini через httpx.AsyncClient.post
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": '{"field_101": 90000, "field_102": "Санкт-Петербург"}'}
                        ]
                    }
                }
            ]
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp

        with patch("app.services.llm_extractor.gemini_http_client.get_client", new_callable=AsyncMock) as mock_get_client:
            mock_get_client.return_value = mock_client

            res = await extractor.extract_lead_fields(
                field_mappings=[fm1, fm2],
                messages=[{"role": "user", "content": "Бюджет 90000, переехал в Санкт-Петербург"}],
                current_lead_values=current_values,
                model_name="gemini-3.1-flash-lite"
            )

            # Поле 101 не должно попасть в список обновления!
            updated_ids = [f["field_id"] for f in res.fields_to_update]
            self.assertNotIn(101, updated_ids, "Поле 101 с overwrite_if_filled=False не должно перезаписываться")
            # Поле 102 должно обновиться
            self.assertIn(102, updated_ids, "Поле 102 с overwrite_if_filled=True должно обновиться")
            self.assertEqual(res.field_name_values.get("Город"), "Санкт-Петербург")

    @patch("httpx.AsyncClient.get")
    async def test_c3_validate_token_returns_account_name(self, mock_get):
        """
        Тест C-3: Проверка, что validate_token возвращает account_name вместе с name.
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": 999,
            "name": "Моя CRM Компания",
            "subdomain": "mycrm"
        }
        mock_get.return_value = mock_resp

        client = AmoCRMClient()
        res = await client.validate_token("mycrm", "dummy_token")
        self.assertTrue(res["is_valid"])
        self.assertEqual(res["name"], "Моя CRM Компания")
        self.assertEqual(res["account_name"], "Моя CRM Компания")

    async def test_a2_distributed_lock_token_safe_release(self):
        """
        Тест A-2: Проверка распределенного лока с токеном и Lua-скриптом.
        Чужой/истёкший лок не должен сниматься чужой задачей.
        """
        debounce = DebounceService()
        mock_redis = AsyncMock()
        debounce.redis = mock_redis

        # Успешный захват
        mock_redis.set.return_value = True
        acquired = await debounce.acquire_lead_lock("acc1", "lead1", ttl=90)
        self.assertTrue(acquired)
        self.assertIn("acc1:lead1", debounce._active_lock_tokens)

        # Освобождение с правильным токеном вызывает eval (Lua-скрипт)
        await debounce.release_lead_lock("acc1", "lead1")
        mock_redis.eval.assert_called_once()

    async def test_o2_amocrm_client_reuses_persistent_async_client(self):
        """
        Тест O-2: Проверка переиспользования единого httpx.AsyncClient в AmoCRMClient.
        """
        client = AmoCRMClient()
        c1 = await client.get_client()
        c2 = await client.get_client()
        self.assertIs(c1, c2, "AmoCRMClient должен повторно использовать один и тот же AsyncClient")

        # Проверка закрытия
        await client.close()
        self.assertTrue(c1.is_closed)
        self.assertIsNone(client._client)

    def test_a5_allowed_origins_configuration(self):
        """
        Тест A-5: Проверка, что ALLOWED_ORIGINS корректно сконфигурирован и не равен wildcard при credentials.
        """
        origins = settings.ALLOWED_ORIGINS
        self.assertIsInstance(origins, list)
        self.assertNotIn("*", origins, "При allow_credentials=True allow_origins не должен содержать '*'")
        self.assertTrue(len(origins) > 0)

    @patch("app.services.delivery_service.debounce_service.acquire_lead_lock")
    @patch("app.services.delivery_service.debounce_service.release_lead_lock")
    async def test_a4_delivery_fails_cleanly_without_ai_reply_field(self, mock_release, mock_acquire):
        """
        Тест A-4: Проверка, что при отсутствии ai_reply_field_id доставка не использует magic id 1000463.
        """
        from app.services.delivery_service import DeliveryService
        mock_acquire.return_value = True

        acc_id = uuid.uuid4()
        mock_acc = MagicMock(spec=Account)
        mock_acc.id = acc_id
        mock_acc.is_active = True
        mock_acc.status = AccountStatus.CONFIGURED
        mock_acc.bot_id = 12345
        mock_acc.ai_reply_field_id = None  # Не задан!

        with patch("app.services.delivery_service.AsyncSessionLocal") as mock_session_local:
            mock_session = AsyncMock()
            mock_session_local.return_value.__aenter__.return_value = mock_session
            mock_res = MagicMock()
            mock_res.scalar_one_or_none.return_value = mock_acc
            mock_session.execute = AsyncMock(return_value=mock_res)

            delivery = DeliveryService()
            # Должен безопасно завершиться без исключения и освободить лок
            await delivery.process_lead_after_debounce(str(acc_id), "123")
            mock_release.assert_called_once()

    @patch("app.services.delivery_service.debounce_service.acquire_lead_lock")
    @patch("app.services.delivery_service.debounce_service.release_lead_lock")
    async def test_a6_timeout_handling(self, mock_release, mock_acquire):
        """
        Тест A-6: Проверка, что таймаут фоновой задачи перехватывается, логируется и освобождает лок.
        """
        from app.services.delivery_service import DeliveryService
        mock_acquire.return_value = True

        delivery = DeliveryService()
        acc_id = str(uuid.uuid4())

        async def _hanging_pipeline(*args, **kwargs):
            raise TimeoutError("Simulated 75s timeout")

        with patch.object(delivery, "_process_lead_pipeline", side_effect=_hanging_pipeline):
            await delivery.process_lead_after_debounce(acc_id, "123")
            mock_release.assert_called_once()
