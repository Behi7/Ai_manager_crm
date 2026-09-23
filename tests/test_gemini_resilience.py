import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.llm_communicator import LLMCommunicator
from app.services.llm_extractor import LLMExtractor
from app.models.account import FieldMapping


class TestGeminiResilience(unittest.IsolatedAsyncioTestCase):

    async def test_communicator_retry_on_503_success(self):
        """Проверка автоматического повтора при 503: 1-я попытка 503, 2-я 200"""
        comm = LLMCommunicator(api_key="test_key")

        resp_503 = MagicMock()
        resp_503.status_code = 503
        resp_503.text = '{"error": {"code": 503, "message": "High demand"}}'

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "Ответ после повтора"}]}}]
        }

        mock_client = AsyncMock()
        mock_client.post.side_effect = [resp_503, resp_200]

        with patch("app.services.llm_communicator.gemini_http_client.get_client", new_callable=AsyncMock) as mock_get_client, \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_get_client.return_value = mock_client

            result = await comm.generate_reply(
                system_prompt="Тест",
                messages=[{"role": "user", "content": "Привет"}],
                model_name="gemini-3.1-flash-lite"
            )

            self.assertEqual(result.text, "Ответ после повтора")
            self.assertFalse(result.is_handover_requested)
            self.assertEqual(mock_client.post.call_count, 2)
            mock_sleep.assert_called_once_with(1.0)

    async def test_communicator_fallback_to_gemini_3_5_flash(self):
        """Проверка переключения на резервную модель gemini-3.5-flash при отказе gemini-3.1-flash-lite"""
        comm = LLMCommunicator(api_key="test_key")

        resp_503 = MagicMock()
        resp_503.status_code = 503
        resp_503.text = '{"error": {"code": 503, "message": "High demand"}}'

        resp_200_fallback = MagicMock()
        resp_200_fallback.status_code = 200
        resp_200_fallback.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "Ответ от резервной модели 2.5 flash"}]}}]
        }

        # 2 попытки на основной (503, 503) -> 1 попытка на резервной (200)
        mock_client = AsyncMock()
        mock_client.post.side_effect = [resp_503, resp_503, resp_200_fallback]

        with patch("app.services.llm_communicator.gemini_http_client.get_client", new_callable=AsyncMock) as mock_get_client, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_get_client.return_value = mock_client

            result = await comm.generate_reply(
                system_prompt="Тест",
                messages=[{"role": "user", "content": "Привет"}],
                model_name="gemini-3.1-flash-lite",
                gemini_cache_name="cachedContents/old_cache"
            )

            self.assertEqual(result.text, "Ответ от резервной модели 2.5 flash")
            self.assertFalse(result.is_handover_requested)
            self.assertEqual(mock_client.post.call_count, 3)

            # Проверяем, что 3-й вызов шел к резервной модели gemini-2.5-flash и без кэша
            third_call = mock_client.post.call_args_list[2]
            url = third_call.args[0] if third_call.args else third_call.kwargs.get("url")
            body = third_call.kwargs.get("json", {})

            self.assertIn("gemini-2.5-flash", url)
            self.assertNotIn("cachedContent", body)
            self.assertIn("systemInstruction", body)

    async def test_communicator_all_models_fail_triggers_handover(self):
        """Если и основная, и резервная модели падают, запрашивается перевод на оператора"""
        comm = LLMCommunicator(api_key="test_key")

        resp_503 = MagicMock()
        resp_503.status_code = 503
        resp_503.text = '{"error": {"code": 503, "message": "High demand"}}'

        mock_client = AsyncMock()
        # 2 попытки на primary + 2 попытки на fallback = 4 раза 503
        mock_client.post.side_effect = [resp_503, resp_503, resp_503, resp_503]

        with patch("app.services.llm_communicator.gemini_http_client.get_client", new_callable=AsyncMock) as mock_get_client, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_get_client.return_value = mock_client

            result = await comm.generate_reply(
                system_prompt="Тест",
                messages=[{"role": "user", "content": "Привет"}],
                model_name="gemini-3.1-flash-lite"
            )

            self.assertTrue(result.is_handover_requested)
            self.assertIn("задержка связи", result.text)
            self.assertIsNotNone(result.error)

    async def test_extractor_retry_and_fallback(self):
        """Проверка повтора и переключения на резервную модель в LLMExtractor"""
        extractor = LLMExtractor(api_key="test_key")

        fm = FieldMapping(
            amo_field_id=777,
            field_name="Город",
            field_type="text",
            is_enabled=True,
            overwrite_if_filled=True
        )

        resp_503 = MagicMock()
        resp_503.status_code = 503
        resp_503.text = '{"error": 503}'

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": '{"field_777": "Ташкент"}'}]}}]
        }

        mock_client = AsyncMock()
        # 2 попытки 503 на primary, затем 200 на fallback
        mock_client.post.side_effect = [resp_503, resp_503, resp_200]

        with patch("app.services.llm_extractor.gemini_http_client.get_client", new_callable=AsyncMock) as mock_get_client, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_get_client.return_value = mock_client

            res = await extractor.extract_lead_fields(
                field_mappings=[fm],
                messages=[{"role": "user", "content": "Я из Ташкента"}],
                model_name="gemini-3.1-flash-lite"
            )

            self.assertEqual(res.field_name_values, {"Город": "Ташкент"})
            self.assertIsNone(res.error)
            self.assertEqual(mock_client.post.call_count, 3)

    async def test_custom_fallback_models_used(self):
        """Проверка, что используется именно пользовательская запасная модель, указанная в настройках"""
        comm = LLMCommunicator(api_key="test_key")
        extractor = LLMExtractor(api_key="test_key")

        resp_503 = MagicMock()
        resp_503.status_code = 503
        resp_503.text = '{"error": 503}'

        resp_200_comm = MagicMock()
        resp_200_comm.status_code = 200
        resp_200_comm.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "Ответ от кастомной запасной модели"}]}}]
        }

        mock_client = AsyncMock()
        mock_client.post.side_effect = [resp_503, resp_503, resp_200_comm]

        with patch("app.services.llm_communicator.gemini_http_client.get_client", new_callable=AsyncMock) as mock_get_client, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_get_client.return_value = mock_client

            custom_fallback = "gemini-3.5-flash"
            result = await comm.generate_reply(
                system_prompt="Тест",
                messages=[{"role": "user", "content": "Привет"}],
                model_name="gemini-3.1-flash-lite",
                fallback_model=custom_fallback
            )

            self.assertEqual(result.text, "Ответ от кастомной запасной модели")
            third_call = mock_client.post.call_args_list[2]
            url = third_call.args[0] if third_call.args else third_call.kwargs.get("url")
            self.assertIn(custom_fallback, url)

        # Аналогично проверяем экстрактор с кастомной запасной моделью
        resp_200_ext = MagicMock()
        resp_200_ext.status_code = 200
        resp_200_ext.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": '{"field_10": "Кастом"}'}]}}]
        }
        mock_client.post.side_effect = [resp_503, resp_503, resp_200_ext]

        fm = FieldMapping(
            amo_field_id=10,
            field_name="КастомПоле",
            field_type="text",
            is_enabled=True,
            overwrite_if_filled=True
        )

        with patch("app.services.llm_extractor.gemini_http_client.get_client", new_callable=AsyncMock) as mock_get_client, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_get_client.return_value = mock_client

            res = await extractor.extract_lead_fields(
                field_mappings=[fm],
                messages=[{"role": "user", "content": "Тест"}],
                model_name="gemini-3.1-flash-lite",
                fallback_model="gemini-3.5-flash"
            )

            self.assertEqual(res.field_name_values, {"КастомПоле": "Кастом"})
            third_call = mock_client.post.call_args_list[2]
            url = third_call.args[0] if third_call.args else third_call.kwargs.get("url")
            self.assertIn("gemini-3.5-flash", url)

