import unittest
from unittest.mock import patch, MagicMock, AsyncMock
from app.services.llm_communicator import LLMCommunicator


class TestCommunicatorFields(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.communicator = LLMCommunicator(api_key="test_api_key")

    @patch("httpx.AsyncClient.post")
    async def test_target_fields_in_prompt(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{"text": "Здорово! А в каком городе вы находитесь?"}]
                }
            }]
        }
        mock_post.return_value = mock_response

        target_fields = [
            {"name": "Город", "hint": "В каком городе живет клиент"},
            {"name": "Бюджет", "hint": "Планируемый бюджет на рекламу"}
        ]
        known_fields = {
            "Имя": "Алишер"
        }

        resp = await self.communicator.generate_reply(
            system_prompt="Ты менеджер автосалона.",
            messages=[{"role": "user", "content": "Привет! Хочу купить машину."}],
            target_fields=target_fields,
            known_fields=known_fields
        )

        self.assertEqual(resp.text, "Здорово! А в каком городе вы находитесь?")
        self.assertTrue(mock_post.called)
        call_json = mock_post.call_args[1]["json"]
        system_instruction_text = call_json["systemInstruction"]["parts"][0]["text"]

        # Проверяем, что в системную инструкцию попали известные поля
        self.assertIn("УЖЕ ИЗВЕСТНЫЕ ДАННЫЕ О КЛИЕНТЕ И СДЕЛКЕ:", system_instruction_text)
        self.assertIn("- Имя: Алишер", system_instruction_text)

        # Проверяем, что в системную инструкцию попали целевые поля с подсказками
        self.assertIn("ЦЕЛЕВЫЕ ДАННЫЕ, КОТОРЫЕ НУЖНО ВЫЯСНИТЬ У КЛИЕНТА (КВАЛИФИКАЦИЯ):", system_instruction_text)
        self.assertIn("- Город: В каком городе живет клиент", system_instruction_text)
        self.assertIn("- Бюджет: Планируемый бюджет на рекламу", system_instruction_text)

        # Проверяем правило одного вопроса и этику диалога
        self.assertIn("ПРАВИЛО ОДНОГО ВОПРОСА: Задавай максимум ОДИН ненавязчивый вопрос в сообщении!", system_instruction_text)
        self.assertIn("СНАЧАЛА ПОЛЬЗА/ОТВЕТ, ЗАТЕМ ВОПРОС", system_instruction_text)

    @patch("httpx.AsyncClient.post")
    async def test_all_fields_known(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{"text": "Отлично, оформляем доставку!"}]
                }
            }]
        }
        mock_post.return_value = mock_response

        known_fields = {
            "Город": "Ташкент",
            "Бюджет": "1000$"
        }

        resp = await self.communicator.generate_reply(
            system_prompt="Ты менеджер.",
            messages=[{"role": "user", "content": "Да, все верно"}],
            target_fields=[],
            known_fields=known_fields
        )

        self.assertEqual(resp.text, "Отлично, оформляем доставку!")
        call_json = mock_post.call_args[1]["json"]
        system_instruction_text = call_json["systemInstruction"]["parts"][0]["text"]

        self.assertIn("Все ключевые квалификационные параметры сделки уже выяснены.", system_instruction_text)
        self.assertNotIn("ЦЕЛЕВЫЕ ДАННЫЕ, КОТОРЫЕ НУЖНО ВЫЯСНИТЬ", system_instruction_text)


if __name__ == "__main__":
    unittest.main()
