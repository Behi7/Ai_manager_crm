import unittest
import uuid
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.main import app
from app.models.account import Account
from app.core.config import settings
from app.core.auth import verify_admin_key


class TestAuditFixesV3(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_account_leads_lazy_noload(self):
        """Issue 1: Account.leads must be configured with lazy='noload' to prevent OOM DB crashes."""
        prop = Account.__mapper__.relationships["leads"]
        self.assertEqual(prop.lazy, "noload")

    async def test_auth_verify_admin_key_success_header(self):
        """Issue 5: verify_admin_key succeeds with valid X-Admin-Key header."""
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/accounts",
            "headers": [(b"x-admin-key", settings.ADMIN_API_KEY.encode("utf-8"))],
            "query_string": b"",
        }
        req = Request(scope)
        res = await verify_admin_key(req)
        self.assertTrue(res)

    async def test_auth_verify_admin_key_success_bearer(self):
        """Issue 5: verify_admin_key succeeds with Authorization: Bearer <key>."""
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/accounts",
            "headers": [(b"authorization", f"Bearer {settings.ADMIN_API_KEY}".encode("utf-8"))],
            "query_string": b"",
        }
        req = Request(scope)
        res = await verify_admin_key(req)
        self.assertTrue(res)

    async def test_auth_verify_admin_key_success_cookie(self):
        """Issue 5: verify_admin_key succeeds with cookie."""
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/accounts",
            "headers": [(b"cookie", f"admin_key={settings.ADMIN_API_KEY}".encode("utf-8"))],
            "query_string": b"",
        }
        req = Request(scope)
        res = await verify_admin_key(req)
        self.assertTrue(res)

    async def test_auth_verify_admin_key_failure(self):
        """Issue 5: verify_admin_key raises 403 when key is missing or invalid."""
        scope_wrong = {
            "type": "http",
            "method": "GET",
            "path": "/accounts",
            "headers": [(b"x-admin-key", b"wrong_key")],
            "query_string": b"",
        }
        with self.assertRaises(HTTPException) as ctx:
            await verify_admin_key(Request(scope_wrong))
        self.assertEqual(ctx.exception.status_code, 403)

        scope_empty = {
            "type": "http",
            "method": "GET",
            "path": "/accounts",
            "headers": [],
            "query_string": b"",
        }
        with self.assertRaises(HTTPException) as ctx2:
            await verify_admin_key(Request(scope_empty))
        self.assertEqual(ctx2.exception.status_code, 403)

    def test_routes_accounts_protected_by_auth(self):
        """Issue 5: /accounts endpoints return 403 Forbidden if unauthenticated."""
        response = self.client.get("/accounts")
        self.assertEqual(response.status_code, 403)

        response_authed = self.client.get(
            "/accounts",
            headers={"X-Admin-Key": settings.ADMIN_API_KEY}
        )
        self.assertNotEqual(response_authed.status_code, 403)

    async def test_debounce_atomic_pop_lua_script(self):
        """Issue 2: pop_buffered_messages uses atomic Lua script."""
        from app.services.debounce_service import DebounceService

        service = DebounceService()
        mock_redis = AsyncMock()
        mock_redis.eval = AsyncMock(return_value=[b'{"text": "msg1"}', b'{"text": "msg2"}'])

        with patch.object(service, "get_redis", return_value=mock_redis):
            result = await service.pop_buffered_messages(uuid.uuid4(), 123)
            self.assertEqual(len(result["texts"]), 2)
            self.assertEqual(result["texts"], ["msg1", "msg2"])
            mock_redis.eval.assert_called_once()
            lua_arg = mock_redis.eval.call_args[0][0]
            self.assertIn("LRANGE", lua_arg)
            self.assertIn("DEL", lua_arg)

    def test_webhook_rate_limiter_atomic_pipeline(self):
        """Issue 4: Webhook rate limiter uses pipeline for atomic INCR and EXPIRE."""
        from app.api.routes_webhook import handle_amocrm_webhook
        import inspect
        source = inspect.getsource(handle_amocrm_webhook)
        self.assertIn("async with r_redis.pipeline(transaction=True) as pipe:", source)
        self.assertIn("pipe.incr(rl_key)", source)
        self.assertIn("pipe.expire(rl_key, 60)", source)
