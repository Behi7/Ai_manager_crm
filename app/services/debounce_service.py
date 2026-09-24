import asyncio
import json
import logging
import uuid
from typing import Any, Callable, Coroutine, Dict, List, Optional
import redis.asyncio as aioredis
from app.core.config import settings

logger = logging.getLogger("DebounceService")


class DebounceService:
    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self._timers: Dict[str, asyncio.TimerHandle] = {}
        self._active_lock_tokens: Dict[str, str] = {}
        self._background_tasks: set[asyncio.Task] = set()

    async def get_redis(self) -> aioredis.Redis:
        if self.redis is None:
            self.redis = aioredis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                encoding="utf-8",
                max_connections=settings.REDIS_MAX_CONNECTIONS,
                health_check_interval=settings.REDIS_HEALTH_CHECK_INTERVAL,
                retry_on_timeout=settings.REDIS_RETRY_ON_TIMEOUT
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
        attachment: Optional[Dict[str, Any]] = None,
        is_comment: bool = False
    ):
        """Добавление сообщения (текст, медиавложение, флаг комментария) в буфер лида"""
        r = await self.get_redis()
        key = f"debounce_msgs:{account_id}:{lead_id}"
        item = {
            "text": text,
            "attachment": attachment,
            "is_comment": is_comment
        }
        await r.rpush(key, json.dumps(item, ensure_ascii=False))
        await r.expire(key, 60)

    async def pop_buffered_messages(self, account_id: str, lead_id: str) -> Dict[str, Any]:
        """
        Извлечение всех накопленных сообщений лида из буфера.
        Возвращает: {"texts": List[str], "attachments": List[Dict[str, Any]], "is_comment": bool}
        """
        r = await self.get_redis()
        key = f"debounce_msgs:{account_id}:{lead_id}"
        # Атомарный pop: читаем всё и удаляем за одну неделимую Lua-операцию (устранение TOCTOU race condition)
        lua_pop_all = """
        local items = redis.call("LRANGE", KEYS[1], 0, -1)
        if #items > 0 then
            redis.call("DEL", KEYS[1])
        end
        return items
        """
        try:
            raw_items = await r.eval(lua_pop_all, 1, key)
            if not isinstance(raw_items, list):
                raw_items = await r.lrange(key, 0, -1)
                await r.delete(key)
        except Exception:
            # Безопасный fallback для сред тестирования / моков
            raw_items = await r.lrange(key, 0, -1)
            await r.delete(key)

        texts: List[str] = []
        attachments: List[Dict[str, Any]] = []
        is_comment = False

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
                    if parsed.get("is_comment"):
                        is_comment = True
                else:
                    if item_str.strip():
                        texts.append(item_str.strip())
            except Exception:
                # Обратная совместимость: если в очереди остался обычный текст
                if item_str.strip():
                    texts.append(item_str.strip())

        return {
            "texts": texts,
            "attachments": attachments,
            "is_comment": is_comment,
            "raw_items": list(raw_items) if raw_items else []
        }

    async def restore_buffered_messages(
        self,
        account_id: str,
        lead_id: str,
        buffered_data: Dict[str, Any]
    ):
        """
        Восстановление сообщений в начало буфера Redis (LPUSH) при сбое в пайплайне обработки (CRITICAL-01).
        """
        if not buffered_data:
            return
        texts = buffered_data.get("texts", []) if isinstance(buffered_data, dict) else []
        attachments = buffered_data.get("attachments", []) if isinstance(buffered_data, dict) else []
        is_comment = buffered_data.get("is_comment", False) if isinstance(buffered_data, dict) else False
        if not texts and not attachments:
            return

        try:
            r = await self.get_redis()
            key = f"debounce_msgs:{account_id}:{lead_id}"

            raw_items = buffered_data.get("raw_items") if isinstance(buffered_data, dict) else None
            if raw_items:
                await r.lpush(key, *reversed(raw_items))
                await r.expire(key, 300)
                logger.info(f"🔄 Восстановлено {len(raw_items)} исходных сообщений в буфер {account_id}:{lead_id} после сбоя.")
                return

            items_to_push = []
            for t in texts:
                items_to_push.append(json.dumps({"text": t, "attachment": None, "is_comment": is_comment}, ensure_ascii=False))
            for att in attachments:
                items_to_push.append(json.dumps({"text": "", "attachment": att, "is_comment": is_comment}, ensure_ascii=False))

            if items_to_push:
                # Вставляем в обратном порядке через lpush, чтобы восстановить исходный порядок сообщений
                await r.lpush(key, *reversed(items_to_push))
                await r.expire(key, 300)
                logger.info(f"🔄 Восстановлено {len(items_to_push)} сообщений в буфер {account_id}:{lead_id} после сбоя.")
        except Exception as e:
            logger.error(f"Не удалось восстановить буфер сообщений для {account_id}:{lead_id}: {e}")

    async def has_buffered_messages(self, account_id: str, lead_id: str) -> bool:
        """Проверка наличия ожидающих сообщений в буфере лида"""
        try:
            r = await self.get_redis()
            key = f"debounce_msgs:{account_id}:{lead_id}"
            count = await r.llen(key)
            return count > 0
        except Exception as e:
            logger.warning(f"Ошибка проверки буфера сообщений {account_id}:{lead_id}: {e}")
            return False

    async def acquire_lead_lock(self, account_id: str, lead_id: str, ttl: int = 90, token: Optional[str] = None) -> bool:
        """
        Распределенный захват мьютекса LEAD_BUSY в Redis на время выполнения генерации и записи.
        """
        lock_id = f"{account_id}:{lead_id}"
        tok = token or str(uuid.uuid4())
        r = await self.get_redis()
        key = f"lock:lead:{lock_id}"
        acquired = await r.set(key, tok, nx=True, ex=ttl)
        if acquired:
            self._active_lock_tokens[lock_id] = tok
            return True
        return False

    async def release_lead_lock(self, account_id: str, lead_id: str, token: Optional[str] = None):
        """Безопасное освобождение Redis-лока по токену через Lua-скрипт (не сбивает чужой продлённый лок)"""
        lock_id = f"{account_id}:{lead_id}"
        tok = token or self._active_lock_tokens.pop(lock_id, None)
        try:
            r = await self.get_redis()
            key = f"lock:lead:{lock_id}"
            if tok:
                lua_script = """
                if redis.call("get", KEYS[1]) == ARGV[1] then
                    return redis.call("del", KEYS[1])
                else
                    return 0
                end
                """
                await r.eval(lua_script, 1, key, tok)
            else:
                await r.delete(key)
        except Exception as e:
            logger.warning(f"Ошибка снятия Redis-лока {lock_id}: {e}")

    def schedule_debounce(
        self,
        account_id: str,
        lead_id: str,
        callback: Callable[[str, str], Coroutine],
        delay: float = 2.5
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
            task = asyncio.create_task(callback(account_id, lead_id))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        timer = loop.call_later(delay, _fire)
        self._timers[key] = timer


debounce_service = DebounceService()

