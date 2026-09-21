import asyncio
import json
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional
import redis.asyncio as aioredis
from app.core.config import settings

logger = logging.getLogger("DebounceService")


class DebounceService:
    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self._timers: Dict[str, asyncio.TimerHandle] = {}
        self._memory_busy: set = set()

    async def get_redis(self) -> aioredis.Redis:
        if self.redis is None:
            self.redis = aioredis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                encoding="utf-8"
            )
        return self.redis

    async def is_duplicate_message(self, account_id: str, message_id: str) -> bool:
        """
        Проверка и установка дедупликации через SETNX в Redis (TTL 10 минут).
        Возвращает True, если сообщение дубликат.
        """
        if not message_id:
            return False
        r = await self.get_redis()
        key = f"msg_dedup:{account_id}:{message_id}"
        is_new = await r.set(key, "1", nx=True, ex=600)
        return not is_new

    async def add_message_to_buffer(
        self,
        account_id: str,
        lead_id: str,
        text: str,
        attachment: Optional[Dict[str, Any]] = None
    ):
        """Добавление сообщения (текст и возможное медиавложение) в буфер лида"""
        r = await self.get_redis()
        key = f"debounce_msgs:{account_id}:{lead_id}"
        item = {
            "text": text,
            "attachment": attachment
        }
        await r.rpush(key, json.dumps(item, ensure_ascii=False))
        await r.expire(key, 60)

    async def pop_buffered_messages(self, account_id: str, lead_id: str) -> Dict[str, Any]:
        """
        Извлечение всех накопленных сообщений лида из буфера.
        Возвращает: {"texts": List[str], "attachments": List[Dict[str, Any]]}
        """
        r = await self.get_redis()
        key = f"debounce_msgs:{account_id}:{lead_id}"
        raw_items = await r.lrange(key, 0, -1)
        await r.delete(key)

        texts: List[str] = []
        attachments: List[Dict[str, Any]] = []

        for item_str in raw_items:
            try:
                parsed = json.loads(item_str)
                if isinstance(parsed, dict):
                    t = parsed.get("text", "").strip()
                    if t:
                        texts.append(t)
                    att = parsed.get("attachment")
                    if att and isinstance(att, dict) and att.get("link"):
                        attachments.append(att)
                else:
                    if item_str.strip():
                        texts.append(item_str.strip())
            except Exception:
                # Обратная совместимость: если в очереди остался обычный текст
                if item_str.strip():
                    texts.append(item_str.strip())

        return {
            "texts": texts,
            "attachments": attachments
        }

    async def acquire_lead_lock(self, account_id: str, lead_id: str, ttl: int = 90) -> bool:
        """
        Захват мьютекса LEAD_BUSY на время выполнения генерации и записи.
        """
        lock_id = f"{account_id}:{lead_id}"
        if lock_id in self._memory_busy:
            return False
        
        r = await self.get_redis()
        key = f"lock:lead:{lock_id}"
        acquired = await r.set(key, "1", nx=True, ex=ttl)
        if acquired:
            self._memory_busy.add(lock_id)
            return True
        return False

    async def release_lead_lock(self, account_id: str, lead_id: str):
        """Освобождение мьютекса LEAD_BUSY"""
        lock_id = f"{account_id}:{lead_id}"
        self._memory_busy.discard(lock_id)
        try:
            r = await self.get_redis()
            key = f"lock:lead:{lock_id}"
            await r.delete(key)
        except Exception as e:
            logger.warning(f"Ошибка снятия Redis-лока {lock_id}: {e}")

    def schedule_debounce(
        self,
        account_id: str,
        lead_id: str,
        callback: Callable[[str, str], Coroutine],
        delay: float = 1.8
    ):
        """
        Умный таймер дебаунса: если клиент присылает сообщение, таймер сбрасывается.
        Через delay секунд тишины вызывается callback.
        """
        key = f"{account_id}:{lead_id}"
        loop = asyncio.get_running_loop()

        # Если уже был запущен таймер для этой сделки — отменяем его
        if key in self._timers:
            self._timers[key].cancel()

        def _fire():
            self._timers.pop(key, None)
            asyncio.create_task(callback(account_id, lead_id))

        timer = loop.call_later(delay, _fire)
        self._timers[key] = timer


debounce_service = DebounceService()

