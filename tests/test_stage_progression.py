import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.delivery_service import DeliveryService
from app.services.llm_communicator import CommunicatorResponse
from app.services.llm_extractor import ExtractionResult


def _make_stages():
    return [
        {"id": 10, "name": "Этап 0: Старт", "sort": 10},
        {"id": 20, "name": "Этап 1: Контакты", "sort": 20},
        {"id": 30, "name": "Этап 2: Сделка", "sort": 30},
        {"id": 40, "name": "Этап 3: Готов", "sort": 40},
        {"id": 50, "name": "Этап 4: Handover", "sort": 50},
    ]


class TestStageProgression(unittest.IsolatedAsyncioTestCase):
    async def _run_pipeline_with_state(
        self,
        initial_status_id: int,
        enabled_stage_ids: list,
        contact_filled: bool,
        lead_filled: bool,
        handover_requested: bool = False,
    ):
        svc = DeliveryService()
        acc_id = uuid.uuid4()

        pipe = MagicMock()
        pipe.amo_pipeline_id = 777
        pipe.name = "Основная воронка"
        pipe.is_enabled = True
        pipe.stages_json = _make_stages()
        pipe.enabled_stage_ids = enabled_stage_ids

        fm_contact = MagicMock()
        fm_contact.amo_field_id = 101
        fm_contact.field_name = "Телефон"
        fm_contact.field_type = "multitext"
        fm_contact.entity_type = "contact"
        fm_contact.is_enabled = True
        fm_contact.ai_hint = "Телефон"
        fm_contact.overwrite_if_filled = False

        fm_lead = MagicMock()
        fm_lead.amo_field_id = 202
        fm_lead.field_name = "Услуга"
        fm_lead.field_type = "text"
        fm_lead.entity_type = "lead"
        fm_lead.is_enabled = True
        fm_lead.ai_hint = "Услуга"
        fm_lead.overwrite_if_filled = False

        account = MagicMock()
        account.id = acc_id
        account.subdomain = "testsub"
        account.bot_id = 999
        account.ai_reply_field_id = 555
        account.pipelines = [pipe]
        account.field_mappings = [fm_contact, fm_lead]
        account.ai_config = None

        lead_obj = MagicMock()
        lead_obj.id = uuid.uuid4()
        lead_obj.stuck_count = 0
        lead_obj.handover_required = False

        mock_session = AsyncMock()
        mock_exec_res = MagicMock()
        mock_exec_res.scalar_one_or_none.return_value = lead_obj
        mock_exec_res.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_exec_res)

        extracted_fields = []
        if contact_filled:
            extracted_fields.append({"field_id": 101, "entity_type": "contact", "values": [{"value": "+998901234567"}]})
        if lead_filled:
            extracted_fields.append({"field_id": 202, "entity_type": "lead", "values": [{"value": "Кухня на заказ"}]})

        with patch("app.services.delivery_service.amocrm_client") as mock_amo,              patch("app.services.delivery_service.communicator") as mock_comm,              patch("app.services.delivery_service.extractor") as mock_ext:
            mock_amo.get_lead = AsyncMock(return_value={
                "id": 12345,
                "pipeline_id": 777,
                "status_id": initial_status_id,
                "contact_id": 888,
                "custom_fields_values": []
            })
            mock_amo.get_contact = AsyncMock(return_value={"id": 888, "custom_fields_values": []})
            mock_amo.patch_lead_custom_fields = AsyncMock(return_value=True)
            mock_amo.patch_contact_custom_fields = AsyncMock(return_value=True)
            mock_amo.run_salesbot = AsyncMock(return_value=True)
            mock_amo.create_operator_task = AsyncMock(return_value=True)
            mock_amo.patch_lead_status = AsyncMock(return_value=True)

            mock_comm.generate_reply = AsyncMock(return_value=CommunicatorResponse(
                text="Ответ ИИ",
                is_handover_requested=handover_requested
            ))
            mock_ext.extract_lead_fields = AsyncMock(return_value=ExtractionResult(
                raw_response={},
                fields_to_update=extracted_fields,
                field_name_values={"test": "val"} if extracted_fields else {}
            ))

            await svc._execute_lead_pipeline(
                session=mock_session,
                account=account,
                account_uuid=acc_id,
                amo_lead_id=12345,
                account_id_str=str(acc_id),
                lead_id_str="12345",
                access_token="tok",
                subdomain="testsub",
                buffered_texts=["Привет"],
                buffered_attachments=[],
            )
            return mock_amo, mock_comm, lead_obj

    async def test_stage_1_when_only_contact_filled(self):
        mock_amo, _, lead_obj = await self._run_pipeline_with_state(
            initial_status_id=10,
            enabled_stage_ids=[10, 20, 30, 40],
            contact_filled=True,
            lead_filled=False,
        )
        mock_amo.patch_lead_status.assert_awaited_once_with(
            subdomain="testsub", access_token="tok", lead_id=12345, status_id=20
        )
        self.assertFalse(lead_obj.handover_required)

    async def test_stage_2_when_only_lead_filled(self):
        mock_amo, _, lead_obj = await self._run_pipeline_with_state(
            initial_status_id=10,
            enabled_stage_ids=[10, 20, 30, 40],
            contact_filled=False,
            lead_filled=True,
        )
        mock_amo.patch_lead_status.assert_awaited_once_with(
            subdomain="testsub", access_token="tok", lead_id=12345, status_id=30
        )
        self.assertFalse(lead_obj.handover_required)

    async def test_stage_3_when_both_filled_and_ai_stays_enabled(self):
        mock_amo, _, lead_obj = await self._run_pipeline_with_state(
            initial_status_id=20,
            enabled_stage_ids=[10, 20, 30, 40],
            contact_filled=True,
            lead_filled=True,
        )
        mock_amo.patch_lead_status.assert_awaited_once_with(
            subdomain="testsub", access_token="tok", lead_id=12345, status_id=40
        )
        # ИИ НЕ отключается при переходе на 3-й этап!
        self.assertFalse(lead_obj.handover_required)

    async def test_stage_4_on_handover_and_ai_disabled(self):
        mock_amo, _, lead_obj = await self._run_pipeline_with_state(
            initial_status_id=10,
            enabled_stage_ids=[10, 20, 30, 40],
            contact_filled=False,
            lead_filled=False,
            handover_requested=True,
        )
        mock_amo.patch_lead_status.assert_awaited_once_with(
            subdomain="testsub", access_token="tok", lead_id=12345, status_id=50
        )
        mock_amo.create_operator_task.assert_not_called()
        self.assertTrue(lead_obj.handover_required)
        # Проверяем, что клиенту отправляется ответ Общителя (на языке клиента), а НЕ захардкоженная русская строка
        mock_amo.patch_lead_custom_fields.assert_awaited_with(
            subdomain="testsub",
            access_token="tok",
            lead_id=12345,
            fields=[{"field_id": 555, "values": [{"value": "Ответ ИИ"}]}],
        )

    async def test_skips_reply_when_stage_not_enabled(self):
        mock_amo, mock_comm, _ = await self._run_pipeline_with_state(
            initial_status_id=50,  # На этапе 50 (Handover) галочка снята
            enabled_stage_ids=[10, 20, 30, 40],
            contact_filled=True,
            lead_filled=True,
        )
        mock_comm.generate_reply.assert_not_called()

    async def test_replies_when_stage_4_checkbox_is_enabled(self):
        mock_amo, mock_comm, _ = await self._run_pipeline_with_state(
            initial_status_id=50,  # На этапе 50 (Handover) галочка ВКЛЮЧЕНА
            enabled_stage_ids=[10, 20, 30, 40, 50],
            contact_filled=True,
            lead_filled=True,
            handover_requested=False,
        )
        mock_comm.generate_reply.assert_awaited_once()
        mock_amo.run_salesbot.assert_awaited_once()

