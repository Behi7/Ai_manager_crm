import unittest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
from app.services.amocrm_client import amocrm_client, AmoCRMAuthOrBillingError
from app.models.account import Account, AccountStatus
from app.services.delivery_service import DeliveryService


class TestAmoCRMAuthErrors(unittest.IsolatedAsyncioTestCase):
    def test_check_http_auth_401(self):
        resp = MagicMock()
        resp.status_code = 401
        with self.assertRaises(AmoCRMAuthOrBillingError) as ctx:
            amocrm_client._check_http_auth_or_billing(resp, "testsub")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("Токен amoCRM истёк или отозван (HTTP 401)", ctx.exception.detail)

    def test_check_http_billing_402(self):
        resp = MagicMock()
        resp.status_code = 402
        with self.assertRaises(AmoCRMAuthOrBillingError) as ctx:
            amocrm_client._check_http_auth_or_billing(resp, "testsub")
        self.assertEqual(ctx.exception.status_code, 402)
        self.assertIn("Подписка amoCRM закончилась или доступ заблокирован (HTTP 402)", ctx.exception.detail)

    def test_check_http_billing_403(self):
        resp = MagicMock()
        resp.status_code = 403
        with self.assertRaises(AmoCRMAuthOrBillingError) as ctx:
            amocrm_client._check_http_auth_or_billing(resp, "testsub")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("Подписка amoCRM закончилась или доступ заблокирован (HTTP 403)", ctx.exception.detail)

    def test_check_http_success_200(self):
        resp = MagicMock()
        resp.status_code = 200
        # Не должно вызывать исключение
        amocrm_client._check_http_auth_or_billing(resp, "testsub")

    @patch("httpx.AsyncClient.get")
    async def test_validate_token_401(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_get.return_value = mock_resp

        res = await amocrm_client.validate_token("testsub", "expired_token")
        self.assertFalse(res["is_valid"])
        self.assertIn("Неверный поддомен или токен истёк (HTTP 401)", res["error"])

    @patch("httpx.AsyncClient.get")
    async def test_validate_token_402(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 402
        mock_get.return_value = mock_resp

        res = await amocrm_client.validate_token("testsub", "valid_token_but_expired_billing")
        self.assertFalse(res["is_valid"])
        self.assertIn("Подписка amoCRM закончилась или доступ заблокирован (HTTP 402)", res["error"])

    @patch("httpx.AsyncClient.patch")
    async def test_patch_lead_field_raises_on_401(self, mock_patch):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_patch.return_value = mock_resp

        with self.assertRaises(AmoCRMAuthOrBillingError) as ctx:
            await amocrm_client.patch_lead_field("testsub", "token", 123, 456, "value")
        self.assertEqual(ctx.exception.status_code, 401)

    @patch("httpx.AsyncClient.post")
    async def test_run_salesbot_raises_on_403(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_post.return_value = mock_resp

        with self.assertRaises(AmoCRMAuthOrBillingError) as ctx:
            await amocrm_client.run_salesbot("testsub", "token", 123, 456)
        self.assertEqual(ctx.exception.status_code, 403)


class TestDeliveryServiceAuthHandling(unittest.IsolatedAsyncioTestCase):
    @patch("app.services.delivery_service.debounce_service.acquire_lead_lock")
    @patch("app.services.delivery_service.debounce_service.release_lead_lock")
    @patch("app.services.delivery_service.debounce_service.pop_buffered_messages")
    @patch("app.services.delivery_service.debounce_service.has_buffered_messages")
    @patch("app.services.delivery_service.amocrm_client.get_lead")
    @patch("app.services.delivery_service.debounce_service.get_redis")
    @patch("app.services.delivery_service.AsyncSessionLocal")
    async def test_auth_error_deactivates_account(
        self,
        mock_session_local,
        mock_get_redis,
        mock_get_lead,
        mock_has_buffered,
        mock_pop_buffered,
        mock_release_lock,
        mock_acquire_lock,
    ):
        mock_acquire_lock.return_value = True
        mock_pop_buffered.return_value = {"texts": ["Здравствуйте"], "attachments": []}
        mock_has_buffered.return_value = False

        mock_redis = AsyncMock()
        mock_get_redis.return_value = mock_redis

        # Mock database session and account
        mock_account = MagicMock(spec=Account)
        mock_account.id = "14a93685-a715-4291-aa77-aa98e14d5a77"
        mock_account.subdomain = "marketingmarkaziuz"
        mock_account.is_active = True
        mock_account.status = AccountStatus.VERIFIED
        mock_account.last_error = None
        mock_account.encrypted_token = "encrypted"
        mock_account.ai_reply_field_id = 999
        mock_account.bot_id = 888
        mock_account.pipelines = []
        mock_account.field_mappings = []

        mock_session = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_account
        mock_session.execute = AsyncMock(return_value=mock_result)

        # Mock amocrm_client to raise AmoCRMAuthOrBillingError
        mock_get_lead.side_effect = AmoCRMAuthOrBillingError(
            401, "Токен amoCRM истёк или отозван (HTTP 401)"
        )

        with patch("app.services.delivery_service.decrypt_token", return_value="decrypted_token"):
            delivery = DeliveryService()
            await delivery.process_lead_after_debounce(str(mock_account.id), "123")

        # Verify account was marked ERROR and deactivated
        self.assertEqual(mock_account.status, AccountStatus.ERROR)
        self.assertIn("Токен amoCRM истёк или отозван", mock_account.last_error)
        self.assertFalse(mock_account.is_active)
        mock_session.commit.assert_called()

        # Verify Redis disabled cache was set
        mock_redis.set.assert_called_with(f"acc_disabled:{mock_account.id}", "1")


class TestAccountToggleAuthHandling(unittest.IsolatedAsyncioTestCase):
    @patch("app.api.routes_accounts.debounce_service.get_redis")
    @patch("app.api.routes_accounts.amocrm_client.validate_token")
    @patch("app.api.routes_accounts.decrypt_token")
    @patch("app.api.routes_accounts.AsyncSessionLocal")
    async def test_toggle_start_with_invalid_token_fails(
        self, mock_session_local, mock_decrypt, mock_validate, mock_get_redis
    ):
        from app.api.routes_accounts import toggle_account_active
        from fastapi import HTTPException
        import uuid

        acc_id = uuid.uuid4()
        mock_account = MagicMock(spec=Account)
        mock_account.id = acc_id
        mock_account.subdomain = "testsub"
        mock_account.encrypted_token = "enc"
        mock_account.is_active = False
        mock_account.status = AccountStatus.VERIFIED
        mock_account.last_error = None

        mock_session = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_account
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_redis = AsyncMock()
        mock_get_redis.return_value = mock_redis
        mock_decrypt.return_value = "token123"

        # amoCRM returns 401
        mock_validate.return_value = {
            "is_valid": False,
            "error": "Неверный поддомен или токен истёк (HTTP 401)"
        }

        with self.assertRaises(HTTPException) as ctx:
            await toggle_account_active(acc_id)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Неверный поддомен или токен истёк (HTTP 401)", ctx.exception.detail)
        self.assertFalse(mock_account.is_active)
        self.assertEqual(mock_account.status, AccountStatus.ERROR)
        self.assertEqual(mock_account.last_error, "Неверный поддомен или токен истёк (HTTP 401)")
        mock_redis.set.assert_called_with(f"acc_disabled:{acc_id}", "1")

    @patch("app.api.routes_accounts.debounce_service.get_redis")
    @patch("app.api.routes_accounts.amocrm_client.validate_token")
    @patch("app.api.routes_accounts.decrypt_token")
    @patch("app.api.routes_accounts.AsyncSessionLocal")
    async def test_toggle_start_with_valid_token_recovers_account(
        self, mock_session_local, mock_decrypt, mock_validate, mock_get_redis
    ):
        from app.api.routes_accounts import toggle_account_active
        import uuid

        acc_id = uuid.uuid4()
        mock_account = MagicMock(spec=Account)
        mock_account.id = acc_id
        mock_account.subdomain = "testsub"
        mock_account.encrypted_token = "enc"
        mock_account.is_active = False
        mock_account.status = AccountStatus.ERROR
        mock_account.last_error = "Неверный поддомен или токен истёк (HTTP 401)"
        mock_account.webhook_verified = True
        mock_account.bot_id = 123

        mock_session = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session
        mock_result2 = MagicMock()
        mock_result2.scalar_one_or_none.return_value = mock_account
        mock_session.execute = AsyncMock(return_value=mock_result2)

        mock_redis = AsyncMock()
        mock_get_redis.return_value = mock_redis
        mock_decrypt.return_value = "token123"

        # amoCRM returns valid
        mock_validate.return_value = {
            "is_valid": True,
            "account_id": 999
        }

        res = await toggle_account_active(acc_id)
        self.assertTrue(res["success"])
        self.assertTrue(res["is_active"])
        self.assertEqual(res["status"], "verified")
        self.assertIsNone(res["last_error"])
        self.assertTrue(mock_account.is_active)
        self.assertEqual(mock_account.status, AccountStatus.VERIFIED)
        self.assertIsNone(mock_account.last_error)
        mock_redis.delete.assert_called_with(f"acc_disabled:{acc_id}")
