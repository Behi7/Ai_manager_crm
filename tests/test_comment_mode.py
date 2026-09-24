import json
import unittest
from unittest.mock import AsyncMock, patch, MagicMock
import uuid

from app.api.routes_webhook import extract_webhook_message
from app.services.debounce_service import DebounceService
from app.services.llm_communicator import LLMCommunicator


class TestCommentMode(unittest.IsolatedAsyncioTestCase):

    def test_extract_webhook_message_json_comment(self):
        """Проверка детекции комментария из JSON вебхука"""
        payload = {
            "message": {
                "add": [
                    {
                        "id": "msg_123",
                        "entity_id": "lead_456",
                        "text": "Сколько стоит внедрение? +",
                        "origin": "comment",
                        "author": {"type": "external"}
                    }
                ]
            }
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        msg = extract_webhook_message(body_bytes, "application/json")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["id"], "msg_123")
        self.assertEqual(msg["entity_id"], "lead_456")
        self.assertTrue(msg["is_comment"])

    def test_extract_webhook_message_form_comment(self):
        """Проверка детекции комментария из form-urlencoded вебхука"""
        body_str = (
            "message[add][0][id]=msg_789&"
            "message[add][0][entity_id]=lead_999&"
            "message[add][0][text]=+%2B&"
            "message[add][0][origin]=comment&"
            "message[add][0][author][type]=external"
        )
        body_bytes = body_str.encode("utf-8")
        msg = extract_webhook_message(body_bytes, "application/x-www-form-urlencoded")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["id"], "msg_789")
        self.assertEqual(msg["entity_id"], "lead_999")
        self.assertTrue(msg["is_comment"])

    def test_extract_webhook_message_direct_chat_not_comment(self):
        """Проверка обычного сообщения в Direct: is_comment == False"""
        body_str = (
            "message[add][0][id]=msg_001&"
            "message[add][0][entity_id]=lead_111&"
            "message[add][0][text]=Привет&"
            "message[add][0][origin]=direct&"
            "message[add][0][author][type]=external"
        )
        body_bytes = body_str.encode("utf-8")
        msg = extract_webhook_message(body_bytes, "application/x-www-form-urlencoded")
        self.assertIsNotNone(msg)
        self.assertFalse(msg["is_comment"])

    async def test_debounce_service_preserves_is_comment(self):
        """Буфер Redis сохраняет и корректно отдает флаг is_comment"""
        service = DebounceService()
        mock_redis = AsyncMock()
        service.redis = mock_redis

        # Тест pop_buffered_messages
        mock_redis.lrange.return_value = [
            json.dumps({"text": "+", "attachment": None, "is_comment": True})
        ]
        mock_redis.delete.return_value = True

        data = await service.pop_buffered_messages("acc1", "lead1")
        self.assertEqual(data["texts"], ["+"])
        self.assertTrue(data["is_comment"])

    async def test_llm_communicator_comment_prompt_and_direct_link(self):
        """LLMCommunicator включает инструкции комментария и подставляет direct_link"""
        comm = LLMCommunicator(api_key="test_api_key")
        direct_url = "https://ig.me/m/marketingmarkaziuz"
        system_prompt = "Narxlar bo'yicha javob ber. Direct: {direct_link}"

        captured_body = {}

        async def fake_post(url, *args, **kwargs):
            nonlocal captured_body
            captured_body = kwargs.get("json") or (args[0] if args else {})
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Assalomu alaykum! Narx 2000$. Direct ga yozing 👉 https://ig.me/m/marketingmarkaziuz"}
                            ]
                        }
                    }
                ]
            }
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = fake_post

        with patch("app.services.llm_communicator.gemini_http_client.get_client", return_value=mock_client):
            resp = await comm.generate_reply(
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": "+"}],
                is_comment=True,
                direct_link=direct_url
            )

            self.assertIn("Direct ga yozing", resp.text)
            self.assertIsNotNone(captured_body)
            sys_instruction = captured_body["systemInstruction"]["parts"][0]["text"]
            # Проверяем, что direct_link подставился в промпт
            self.assertIn(direct_url, sys_instruction)
            # Проверяем, что блок инструкций для комментариев присутствует
            self.assertIn("РЕЖИМ ОТВЕТА НА ПУБЛИЧНЫЙ КОММЕНТАРИЙ СОЦСЕТИ", sys_instruction)


if __name__ == "__main__":
    unittest.main()
