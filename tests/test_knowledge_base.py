import unittest
from unittest.mock import AsyncMock, patch, MagicMock
import uuid

from app.models.account import AIConfig
from app.services.llm_communicator import LLMCommunicator, CommunicatorResponse
from app.api.routes_accounts import get_ai_config, update_ai_config, UpdateAIConfigRequest


class TestKnowledgeBase(unittest.IsolatedAsyncioTestCase):

    async def test_plain_text_knowledge_injected_into_prompt(self):
        """Проверка внедрения каталога продуктов в системный промпт в режиме plain_text"""
        comm = LLMCommunicator(api_key="fake_key")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "Добрый день! Чем могу помочь?"}]
                    }
                }
            ]
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp

        with patch("app.services.llm_communicator.gemini_http_client.get_client", new_callable=AsyncMock) as mock_get_client:
            mock_get_client.return_value = mock_client

            catalog_text = "### Пакет Старт\n- Цена: 45 000 руб.\n- Подходит: До 5 сотрудников"
            res = await comm.generate_reply(
                system_prompt="Ты умный менеджер.",
                messages=[{"role": "user", "content": "Привет"}],
                knowledge_base=catalog_text,
                knowledge_mode="plain_text"
            )

            self.assertEqual(res.text, "Добрый день! Чем могу помочь?")
            mock_client.post.assert_called_once()
            call_kwargs = mock_client.post.call_args.kwargs
            sent_body = call_kwargs.get("json", {})

            # Проверяем, что каталог попал в systemInstruction
            sys_inst = sent_body["systemInstruction"]["parts"][0]["text"]
            self.assertIn("БАЗА ЗНАНИЙ И КАТАЛОГ ПРОДУКТОВ КОМПАНИИ", sys_inst)
            self.assertIn("Пакет Старт", sys_inst)
            self.assertIn("45 000 руб.", sys_inst)
            self.assertIn("СТРОГИЙ ЗАПРЕТ НА СПАМ КАТАЛОГОМ", sys_inst)

    async def test_disabled_knowledge_mode_not_injected(self):
        """Проверка, что при knowledge_mode='disabled' каталог не попадает в промпт"""
        comm = LLMCommunicator(api_key="fake_key")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "Привет!"}]}}]
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp

        with patch("app.services.llm_communicator.gemini_http_client.get_client", new_callable=AsyncMock) as mock_get_client:
            mock_get_client.return_value = mock_client

            await comm.generate_reply(
                system_prompt="Ты менеджер.",
                messages=[{"role": "user", "content": "Здравствуйте"}],
                knowledge_base="Секретный товар",
                knowledge_mode="disabled"
            )

            call_kwargs = mock_client.post.call_args.kwargs
            sys_inst = call_kwargs.get("json", {})["systemInstruction"]["parts"][0]["text"]
            self.assertNotIn("БАЗА ЗНАНИЙ И КАТАЛОГ ПРОДУКТОВ", sys_inst)
            self.assertNotIn("Секретный товар", sys_inst)

    async def test_gemini_cache_mode_with_cached_content(self):
        """Проверка прикрепления cachedContent в режиме gemini_cache"""
        comm = LLMCommunicator(api_key="fake_key")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "Здравствуйте!"}]}}]
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp

        with patch("app.services.llm_communicator.gemini_http_client.get_client", new_callable=AsyncMock) as mock_get_client:
            mock_get_client.return_value = mock_client

            cache_id = "cachedContents/sample_cache_987"
            await comm.generate_reply(
                system_prompt="Ты менеджер.",
                messages=[{"role": "user", "content": "Здравствуйте"}],
                knowledge_base="Огромный каталог...",
                knowledge_mode="gemini_cache",
                gemini_cache_name=cache_id
            )

            call_kwargs = mock_client.post.call_args.kwargs
            sent_body = call_kwargs.get("json", {})
            self.assertEqual(sent_body.get("cachedContent"), cache_id)
            self.assertNotIn("systemInstruction", sent_body)

    async def test_gemini_cache_mode_fallback_without_cache_name(self):
        """Проверка fallback на прямой промпт, если gemini_cache выбран, но кэш ещё не создан"""
        comm = LLMCommunicator(api_key="fake_key")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "Добрый день!"}]}}]
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp

        with patch("app.services.llm_communicator.gemini_http_client.get_client", new_callable=AsyncMock) as mock_get_client:
            mock_get_client.return_value = mock_client

            await comm.generate_reply(
                system_prompt="Ты менеджер.",
                messages=[{"role": "user", "content": "Здравствуйте"}],
                knowledge_base="Каталог товаров на 10 позиций",
                knowledge_mode="gemini_cache",
                gemini_cache_name=None  # Кэша нет -> безопасный fallback на In-Context
            )

            call_kwargs = mock_client.post.call_args.kwargs
            sent_body = call_kwargs.get("json", {})
            self.assertNotIn("cachedContent", sent_body)
            self.assertIn("systemInstruction", sent_body)
            sys_inst = sent_body["systemInstruction"]["parts"][0]["text"]
            self.assertIn("Каталог товаров на 10 позиций", sys_inst)

    async def test_create_gemini_context_cache_handling(self):
        """Проверка create_gemini_context_cache при успехе и при недостатке токенов"""
        comm = LLMCommunicator(api_key="test_key")

        # 1. Успешное создание кэша
        mock_ok_resp = MagicMock()
        mock_ok_resp.status_code = 200
        mock_ok_resp.json.return_value = {"name": "cachedContents/new_cache_456"}

        # 2. Ошибка недостаточного объема токенов (< 32k)
        mock_err_resp = MagicMock()
        mock_err_resp.status_code = 400
        mock_err_resp.text = "Cached content token count must be greater than or equal to 32768"

        mock_client = AsyncMock()

        with patch("app.services.llm_communicator.gemini_http_client.get_client", new_callable=AsyncMock) as mock_get_client:
            mock_get_client.return_value = mock_client

            # Успех
            mock_client.post.return_value = mock_ok_resp
            cache_name = await comm.create_gemini_context_cache(
                model_name="gemini-1.5-flash",
                system_instruction="Ты менеджер",
                knowledge_content="Большой текст..."
            )
            self.assertEqual(cache_name, "cachedContents/new_cache_456")

            # Ошибка по лимиту токенов
            mock_client.post.return_value = mock_err_resp
            fallback_res = await comm.create_gemini_context_cache(
                model_name="gemini-1.5-flash",
                system_instruction="Ты менеджер",
                knowledge_content="Короткий текст"
            )
            self.assertIsNone(fallback_res)

    async def test_api_get_and_patch_knowledge_base(self):
        """Проверка получения и обновления knowledge_base и knowledge_mode через API"""
        acc_id = uuid.uuid4()
        cfg = AIConfig(
            account_id=acc_id,
            communicator_prompt="Prompt 1",
            communicator_model="gemini-3.1-flash-lite",
            extractor_model="gemini-3.1-flash-lite",
            temperature=0.4,
            handover_after_stuck=4,
            knowledge_base="Каталог услуг",
            knowledge_mode="plain_text",
            gemini_cache_name="cachedContents/old"
        )

        mock_session = AsyncMock()
        mock_res = MagicMock()
        mock_res.scalar_one_or_none.return_value = cfg
        mock_session.execute = AsyncMock(return_value=mock_res)

        with patch("app.api.routes_accounts.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_session

            # GET
            get_res = await get_ai_config(acc_id)
            self.assertEqual(get_res["knowledge_base"], "Каталог услуг")
            self.assertEqual(get_res["knowledge_mode"], "plain_text")

            # PATCH
            patch_req = UpdateAIConfigRequest(
                knowledge_base="Новый каталог услуг 2026",
                knowledge_mode="gemini_cache"
            )
            patch_res = await update_ai_config(acc_id, patch_req)
            self.assertTrue(patch_res["success"])
            self.assertEqual(cfg.knowledge_base, "Новый каталог услуг 2026")
            self.assertEqual(cfg.knowledge_mode, "gemini_cache")
            # Старый кэш должен инвалидироваться при изменении текста
            self.assertIsNone(cfg.gemini_cache_name)


if __name__ == "__main__":
    unittest.main()

