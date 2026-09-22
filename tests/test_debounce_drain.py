import unittest
from unittest.mock import AsyncMock, patch
from app.services.debounce_service import DebounceService
from app.services.delivery_service import DeliveryService


class TestDebounceBufferDrain(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.debounce = DebounceService()

    @patch("redis.asyncio.from_url")
    async def test_has_buffered_messages(self, mock_from_url):
        mock_redis = AsyncMock()
        mock_from_url.return_value = mock_redis
        self.debounce.redis = mock_redis

        mock_redis.llen.return_value = 3
        has_msgs = await self.debounce.has_buffered_messages("acc1", "lead1")
        self.assertTrue(has_msgs)
        mock_redis.llen.assert_called_with("debounce_msgs:acc1:lead1")

        mock_redis.llen.return_value = 0
        has_no_msgs = await self.debounce.has_buffered_messages("acc1", "lead1")
        self.assertFalse(has_no_msgs)

    @patch("app.services.delivery_service.debounce_service.acquire_lead_lock")
    @patch("app.services.delivery_service.debounce_service.schedule_debounce")
    async def test_lock_busy_reschedules(self, mock_schedule, mock_acquire):
        mock_acquire.return_value = False
        delivery = DeliveryService()

        await delivery.process_lead_after_debounce("acc1", "123")

        mock_schedule.assert_called_once()
        args, kwargs = mock_schedule.call_args
        self.assertEqual(kwargs.get("delay"), 1.0)
        self.assertEqual(kwargs.get("account_id"), "acc1")
        self.assertEqual(kwargs.get("lead_id"), "123")

    @patch("app.services.delivery_service.debounce_service.acquire_lead_lock")
    @patch("app.services.delivery_service.debounce_service.release_lead_lock")
    @patch("app.services.delivery_service.debounce_service.has_buffered_messages")
    @patch("app.services.delivery_service.debounce_service.pop_buffered_messages")
    @patch("app.services.delivery_service.debounce_service.schedule_debounce")
    async def test_finally_drains_buffered_messages(
        self, mock_schedule, mock_pop, mock_has_buffered, mock_release, mock_acquire
    ):
        test_uuid = "14a93685-a715-4291-aa77-aa98e14d5a77"
        mock_acquire.return_value = True
        mock_pop.return_value = {"texts": [], "attachments": []}
        # Имитируем, что пока шла обработка, прилетели новые сообщения
        mock_has_buffered.return_value = True

        delivery = DeliveryService()
        await delivery.process_lead_after_debounce(test_uuid, "123")

        mock_release.assert_called_once_with(test_uuid, "123")
        mock_has_buffered.assert_called_once_with(test_uuid, "123")
        mock_schedule.assert_called_once()
        _, kwargs = mock_schedule.call_args
        self.assertEqual(kwargs.get("delay"), 0.5)


if __name__ == "__main__":
    unittest.main()
