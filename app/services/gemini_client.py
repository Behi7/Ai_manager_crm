import asyncio
import logging
from typing import Optional
import httpx

logger = logging.getLogger("GeminiHTTPClient")


class GeminiHTTPClient:
    """
    Пул долгоживущих HTTP-соединений для обращений к Google Gemini API (IMPORTANT-13).
    Предотвращает повторное открытие TCP/TLS сокетов на каждом шаге генерации и экстракции.
    """
    def __init__(self, timeout: float = 25.0):
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            async with self._lock:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(
                        timeout=self.timeout,
                        limits=httpx.Limits(max_keepalive_connections=20, max_connections=50)
                    )
        return self._client

    async def close(self):
        async with self._lock:
            if self._client and not self._client.is_closed:
                await self._client.aclose()
                self._client = None
                logger.info("Пул соединений Gemini HTTP Client закрыт.")


gemini_http_client = GeminiHTTPClient()

