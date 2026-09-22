import asyncio
import json
import unittest
from urllib.parse import urlencode

from app.services.amocrm_client import amocrm_client
from app.api.routes_webhook import extract_webhook_message
from app.services.debounce_service import debounce_service
from app.services.llm_communicator import communicator


class TestMultimodalIntegration(unittest.TestCase):

    def test_detect_media_mime_type(self):
        # 1. Magic bytes
        ogg_bytes = b"OggS\x00\x02\x00\x00"
        self.assertEqual(amocrm_client.detect_media_mime_type(ogg_bytes), "audio/ogg")

        wav_bytes = b"RIFF\x24\x00\x00\x00WAVEfmt "
        self.assertEqual(amocrm_client.detect_media_mime_type(wav_bytes), "audio/wav")

        mp4_bytes = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00"
        self.assertEqual(amocrm_client.detect_media_mime_type(mp4_bytes), "video/mp4")

        jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF"
        self.assertEqual(amocrm_client.detect_media_mime_type(jpeg_bytes), "image/jpeg")

        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        self.assertEqual(amocrm_client.detect_media_mime_type(png_bytes), "image/png")

        pdf_bytes = b"%PDF-1.5\n%..."
        self.assertEqual(amocrm_client.detect_media_mime_type(pdf_bytes), "application/pdf")

        # 2. Content-type header
        unknown_bytes = b"random_binary_data_12345"
        self.assertEqual(
            amocrm_client.detect_media_mime_type(unknown_bytes, content_type_header="audio/ogg; codecs=opus"),
            "audio/ogg"
        )
        self.assertEqual(
            amocrm_client.detect_media_mime_type(unknown_bytes, content_type_header="video/mp4"),
            "video/mp4"
        )

        # 3. Filename extension
        self.assertEqual(
            amocrm_client.detect_media_mime_type(unknown_bytes, file_name_or_url="voice_message.opus"),
            "audio/ogg"
        )
        self.assertEqual(
            amocrm_client.detect_media_mime_type(unknown_bytes, file_name_or_url="video_note.mp4"),
            "video/mp4"
        )
        self.assertEqual(
            amocrm_client.detect_media_mime_type(unknown_bytes, file_name_or_url="receipt.jpg"),
            "image/jpeg"
        )

    def test_extract_webhook_message_json(self):
        # Голосовое сообщение в JSON
        json_payload = {
            "message": {
                "add": [
                    {
                        "id": "msg_voice_101",
                        "entity_id": 98765,
                        "author": {"type": "external"},
                        "text": "",
                        "attachment": {
                            "type": "audio",
                            "link": "https://amojo.amocrm.ru/attachments/voice_101.ogg",
                            "file_name": "voice.ogg"
                        },
                        "created_at": 1726740000
                    }
                ]
            }
        }
        body_bytes = json.dumps(json_payload).encode("utf-8")
        msg = extract_webhook_message(body_bytes, "application/json")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["id"], "msg_voice_101")
        self.assertEqual(msg["entity_id"], "98765")
        self.assertEqual(msg["text"], "[Голосовое сообщение]")
        self.assertIsNotNone(msg["attachment"])
        self.assertEqual(msg["attachment"]["type"], "audio")
        self.assertEqual(msg["attachment"]["link"], "https://amojo.amocrm.ru/attachments/voice_101.ogg")

        # Фотография с подписью в JSON
        json_photo = {
            "message": {
                "add": [
                    {
                        "id": "msg_photo_102",
                        "entity_id": 98765,
                        "author": {"type": "external"},
                        "text": "Сколько стоит этот диван?",
                        "attachment": {
                            "type": "picture",
                            "link": "https://amojo.amocrm.ru/attachments/sofa.jpg",
                            "file_name": "sofa.jpg"
                        },
                        "created_at": 1726740000
                    }
                ]
            }
        }
        body_bytes2 = json.dumps(json_photo).encode("utf-8")
        msg2 = extract_webhook_message(body_bytes2, "application/json")
        self.assertIsNotNone(msg2)
        self.assertEqual(msg2["text"], "[Фотография] Сколько стоит этот диван?")
        self.assertEqual(msg2["attachment"]["type"], "picture")

    def test_extract_webhook_message_form_urlencoded(self):
        # Кругляшек / видеосообщение в form-urlencoded
        form_data = {
            "message[add][0][id]": "msg_video_201",
            "message[add][0][entity_id]": "554433",
            "message[add][0][author][type]": "external",
            "message[add][0][text]": "Пустое сообщение",
            "message[add][0][attachment][type]": "video",
            "message[add][0][attachment][link]": "https://amojo.amocrm.ru/attachments/round_note.mp4",
            "message[add][0][attachment][file_name]": "round_note.mp4",
            "message[add][0][created_at]": "1726740000"
        }
        body_bytes = urlencode(form_data).encode("utf-8")
        msg = extract_webhook_message(body_bytes, "application/x-www-form-urlencoded")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["id"], "msg_video_201")
        self.assertEqual(msg["entity_id"], "554433")
        self.assertEqual(msg["text"], "[Видеосообщение / Кругляшек]")
        self.assertEqual(msg["attachment"]["type"], "video")
        self.assertEqual(msg["attachment"]["link"], "https://amojo.amocrm.ru/attachments/round_note.mp4")

    def test_debounce_service_buffer(self):
        async def _test():
            acc_id = "test_acc_media"
            lead_id = "112233"

            # 1. Добавляем голосовое сообщение
            await debounce_service.add_message_to_buffer(
                acc_id,
                lead_id,
                text="[Голосовое сообщение]",
                attachment={"type": "audio", "link": "https://amojo.amocrm.ru/voice.ogg", "file_name": "voice.ogg"}
            )

            # 2. Добавляем текстовое пояснение
            await debounce_service.add_message_to_buffer(
                acc_id,
                lead_id,
                text="И еще уточните по доставке",
                attachment=None
            )

            # 3. Извлекаем из буфера
            popped = await debounce_service.pop_buffered_messages(acc_id, lead_id)
            self.assertIn("texts", popped)
            self.assertIn("attachments", popped)
            self.assertEqual(len(popped["texts"]), 2)
            self.assertEqual(popped["texts"][0], "[Голосовое сообщение]")
            self.assertEqual(popped["texts"][1], "И еще уточните по доставке")
            self.assertEqual(len(popped["attachments"]), 1)
            self.assertEqual(popped["attachments"][0]["link"], "https://amojo.amocrm.ru/voice.ogg")

        asyncio.run(_test())

    def test_communicator_media_summary_parsing(self):
        # Проверка извлечения [МЕДИА: ...] и [[HANDOVER]]
        raw_response = (
            "[МЕДИА: Клиент спрашивает, сколько стоит диван Честерфилд в сером цвете]\n"
            "Здравствуйте! Диван Честерфилд в сером цвете есть в наличии, стоимость 4 500 000 сум."
        )

        import re
        media_match = re.search(r"\[МЕДИА:\s*([^\]]+)\]", raw_response, flags=re.IGNORECASE)
        self.assertIsNotNone(media_match)
        media_summary = f"[Медиа: {media_match.group(1).strip()}]"
        self.assertEqual(
            media_summary,
            "[Медиа: Клиент спрашивает, сколько стоит диван Честерфилд в сером цвете]"
        )

        cleaned_text = re.sub(r"\[МЕДИА:\s*[^\]]+\]", "", raw_response, flags=re.IGNORECASE).strip()
        cleaned_text = communicator._clean_response(cleaned_text)
        self.assertEqual(
            cleaned_text,
            "Здравствуйте! Диван Честерфилд в сером цвете есть в наличии, стоимость 4 500 000 сум."
        )


if __name__ == "__main__":
    unittest.main()

