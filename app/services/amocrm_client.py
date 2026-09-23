import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Union, Tuple
from urllib.parse import urlparse
import httpx

logger = logging.getLogger("AmoCRMClient")


class AmoCRMAuthOrBillingError(Exception):
    """Исключение при протухании токена (401) или окончании подписки amoCRM (402/403)"""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"amoCRM error {status_code}: {detail}")


class AmoCRMRateLimiter:
    """Ограничитель частоты запросов к amoCRM (максимум ~7 запросов/сек на аккаунт)"""
    def __init__(self, min_interval: float = 0.15):
        self.min_interval = min_interval
        self._last_request_time: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def wait(self, subdomain: str):
        lock = self._locks.setdefault(subdomain, asyncio.Lock())
        async with lock:
            now = time.time()
            elapsed = now - self._last_request_time.get(subdomain, 0.0)
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self._last_request_time[subdomain] = time.time()


class AmoCRMClient:
    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self.rate_limiter = AmoCRMRateLimiter(min_interval=0.15)
        self._client: Optional[httpx.AsyncClient] = None
        self._client_lock = asyncio.Lock()

    ALLOWED_DOMAINS = (".amocrm.ru", ".amocrm.com", ".kommo.com")

    def _base_url(self, subdomain: str) -> str:
        sub = subdomain.strip().lower()
        if "." in sub:
            for domain in self.ALLOWED_DOMAINS:
                if sub.endswith(domain):
                    return f"https://{sub}"
            clean_sub = sub.split(".")[0]
            logger.warning(
                f"AmoCRMClient: Неизвестный или неподдерживаемый домен '{sub}'. "
                f"Разрешены только {self.ALLOWED_DOMAINS}. Используется безопасный fallback: https://{clean_sub}.amocrm.ru"
            )
            return f"https://{clean_sub}.amocrm.ru"
        return f"https://{sub}.amocrm.ru"

    async def get_client(self) -> httpx.AsyncClient:
        """Получение или создание долгоживущего AsyncClient с пулом соединений (потокобезопасно)"""
        if self._client is None or self._client.is_closed:
            async with self._client_lock:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(
                        timeout=self.timeout,
                        follow_redirects=True,
                        limits=httpx.Limits(max_keepalive_connections=20, max_connections=100)
                    )
        return self._client

    async def close(self):
        """Закрытие пула соединений при остановке приложения"""
        async with self._client_lock:
            if self._client and not self._client.is_closed:
                await self._client.aclose()
                self._client = None

    def _headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "AIManager-SaaS/1.0"
        }

    def _check_http_auth_or_billing(self, resp: httpx.Response, subdomain: str):
        """Проверка на ошибки авторизации (401) или блокировки подписки (402, 403)"""
        if resp.status_code == 401:
            err = "Токен amoCRM истёк или отозван (HTTP 401)"
            logger.error(f"🚨 {err} ({subdomain})")
            raise AmoCRMAuthOrBillingError(401, err)
        elif resp.status_code in (402, 403):
            err = f"Подписка amoCRM закончилась или доступ заблокирован (HTTP {resp.status_code})"
            logger.error(f"🚨 {err} ({subdomain})")
            raise AmoCRMAuthOrBillingError(resp.status_code, err)

    async def validate_token(self, subdomain: str, token: str) -> Dict[str, Any]:
        """
        Проверка токена и получение информации об аккаунте.
        GET /api/v4/account
        """
        await self.rate_limiter.wait(subdomain)
        url = f"{self._base_url(subdomain)}/api/v4/account"
        try:
            client = await self.get_client()
            resp = await client.get(url, headers=self._headers(token))
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "is_valid": True,
                    "account_id": data.get("id"),
                    "name": data.get("name"),
                    "account_name": data.get("name"),
                    "subdomain": data.get("subdomain")
                }
            elif resp.status_code == 401:
                return {"is_valid": False, "error": "Неверный поддомен или токен истёк (HTTP 401)"}
            elif resp.status_code in (402, 403):
                return {"is_valid": False, "error": f"Подписка amoCRM закончилась или доступ заблокирован (HTTP {resp.status_code})"}
            else:
                return {"is_valid": False, "error": f"Ошибка amoCRM: HTTP {resp.status_code}"}
        except (httpx.RequestError, httpx.TimeoutException) as net_err:
            logger.error(f"Сетевая ошибка при проверке токена amoCRM ({subdomain}): {net_err}")
            return {"is_valid": False, "error": f"Сетевая ошибка при связи с amoCRM: {net_err}"}

    async def ensure_reply_field(self, subdomain: str, token: str, field_name: str = "Ответ ИИ") -> Optional[int]:
        """
        Идемпотентный поиск или создание поля сделки «Ответ ИИ».
        """
        await self.rate_limiter.wait(subdomain)
        base = self._base_url(subdomain)
        try:
            client = await self.get_client()
            # 1. Поиск существующего поля
            get_url = f"{base}/api/v4/leads/custom_fields?limit=250"
            resp = await client.get(get_url, headers=self._headers(token))
            self._check_http_auth_or_billing(resp, subdomain)
            if resp.status_code == 200:
                fields_data = resp.json()
                for item in fields_data.get("_embedded", {}).get("custom_fields", []):
                    if item.get("name") in [field_name, "Ответ ИИ", "AI: Ответ ассистента"]:
                        logger.info(f"✅ Найдено существующее поле '{item.get('name')}' ID: {item.get('id')} ({subdomain})")
                        return item.get("id")

            # 2. Создание поля, если не найдено
            await self.rate_limiter.wait(subdomain)
            post_url = f"{base}/api/v4/leads/custom_fields"
            payload = [
                {
                    "name": field_name,
                    "type": "textarea"
                }
            ]
            resp_post = await client.post(post_url, json=payload, headers=self._headers(token))
            self._check_http_auth_or_billing(resp_post, subdomain)
            if resp_post.status_code in [200, 201]:
                created_data = resp_post.json()
                field_id = created_data.get("_embedded", {}).get("custom_fields", [])[0].get("id")
                logger.info(f"✨ Успешно создано поле '{field_name}' ID: {field_id} ({subdomain})")
                return field_id
            else:
                logger.error(f"Ошибка создания поля в amoCRM ({subdomain}): HTTP {resp_post.status_code} {resp_post.text}")
                return None
        except AmoCRMAuthOrBillingError:
            raise
        except (httpx.RequestError, httpx.TimeoutException) as net_err:
            logger.error(f"Сетевая ошибка в ensure_reply_field ({subdomain}): {net_err}")
            return None

    async def try_register_webhook(self, subdomain: str, token: str, destination_url: str) -> Optional[int]:
        """
        Попытка автоматической регистрации вебхука add_message.
        POST /api/v4/webhooks
        """
        await self.rate_limiter.wait(subdomain)
        url = f"{self._base_url(subdomain)}/api/v4/webhooks"
        payload = {
            "destination": destination_url,
            "settings": ["add_message"]
        }
        try:
            client = await self.get_client()
            resp = await client.post(url, json=payload, headers=self._headers(token))
            self._check_http_auth_or_billing(resp, subdomain)
            if resp.status_code in [200, 201]:
                data = resp.json()
                webhook_id = data.get("id")
                logger.info(f"✅ Авто-регистрация вебхука успешна (ID {webhook_id}) для {subdomain}")
                return webhook_id
            else:
                logger.warning(f"Авто-регистрация вебхука вернула HTTP {resp.status_code} ({subdomain})")
                return None
        except AmoCRMAuthOrBillingError:
            raise
        except (httpx.RequestError, httpx.TimeoutException) as net_err:
            logger.warning(f"Сетевая ошибка авто-регистрации вебхука ({subdomain}): {net_err}")
            return None
        except Exception as e:
            logger.warning(f"Ошибка авто-регистрации вебхука ({subdomain}): {e}")
            return None

    async def list_bots(self, subdomain: str, token: str) -> List[Dict[str, Any]]:
        """
        Получение списка Salesbot для выпадающего списка в UI.
        GET /api/v4/bots
        """
        await self.rate_limiter.wait(subdomain)
        url = f"{self._base_url(subdomain)}/api/v4/bots?limit=250"
        try:
            client = await self.get_client()
            resp = await client.get(url, headers=self._headers(token))
            self._check_http_auth_or_billing(resp, subdomain)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("_embedded", {}).get("items", [])
                return [
                    {
                        "id": b.get("id"),
                        "name": b.get("name"),
                        "is_active": b.get("settings", {}).get("active", True)
                    }
                    for b in items
                ]
            return []
        except AmoCRMAuthOrBillingError:
            raise
        except (httpx.RequestError, httpx.TimeoutException) as net_err:
            logger.error(f"Сетевая ошибка в list_bots ({subdomain}): {net_err}")
            return []

    async def list_pipelines(self, subdomain: str, token: str) -> List[Dict[str, Any]]:
        """
        Получение списка воронок со статусами.
        GET /api/v4/leads/pipelines
        """
        await self.rate_limiter.wait(subdomain)
        url = f"{self._base_url(subdomain)}/api/v4/leads/pipelines"
        try:
            client = await self.get_client()
            resp = await client.get(url, headers=self._headers(token))
            self._check_http_auth_or_billing(resp, subdomain)
            if resp.status_code == 200:
                data = resp.json()
                pipelines = data.get("_embedded", {}).get("pipelines", [])
                result = []
                for p in pipelines:
                    statuses = p.get("_embedded", {}).get("statuses", [])
                    result.append({
                        "id": p.get("id"),
                        "name": p.get("name"),
                        "statuses": [{"id": s.get("id"), "name": s.get("name")} for s in statuses]
                    })
                return result
            return []
        except AmoCRMAuthOrBillingError:
            raise
        except (httpx.RequestError, httpx.TimeoutException) as net_err:
            logger.error(f"Сетевая ошибка в list_pipelines ({subdomain}): {net_err}")
            return []

    async def list_custom_fields(self, subdomain: str, token: str) -> List[Dict[str, Any]]:
        """
        Получение списка кастомных полей сделок для маппинга Экстрактора.
        GET /api/v4/leads/custom_fields
        """
        await self.rate_limiter.wait(subdomain)
        url = f"{self._base_url(subdomain)}/api/v4/leads/custom_fields?limit=250"
        try:
            client = await self.get_client()
            resp = await client.get(url, headers=self._headers(token))
            self._check_http_auth_or_billing(resp, subdomain)
            if resp.status_code == 200:
                data = resp.json()
                fields = data.get("_embedded", {}).get("custom_fields", [])
                return [
                    {
                        "id": f.get("id"),
                        "name": f.get("name"),
                        "type": f.get("type"),
                        "entity_type": "lead"
                    }
                    for f in fields
                ]
            return []
        except AmoCRMAuthOrBillingError:
            raise
        except (httpx.RequestError, httpx.TimeoutException) as net_err:
            logger.error(f"Сетевая ошибка в list_custom_fields ({subdomain}): {net_err}")
            return []

    async def list_contact_custom_fields(self, subdomain: str, token: str) -> List[Dict[str, Any]]:
        """
        Получение списка кастомных полей контактов.
        GET /api/v4/contacts/custom_fields
        """
        await self.rate_limiter.wait(subdomain)
        url = f"{self._base_url(subdomain)}/api/v4/contacts/custom_fields?limit=250"
        try:
            client = await self.get_client()
            resp = await client.get(url, headers=self._headers(token))
            self._check_http_auth_or_billing(resp, subdomain)
            if resp.status_code == 200:
                data = resp.json()
                fields = data.get("_embedded", {}).get("custom_fields", [])
                return [
                    {
                        "id": f.get("id"),
                        "name": f.get("name"),
                        "type": f.get("type"),
                        "entity_type": "contact"
                    }
                    for f in fields
                ]
            return []
        except AmoCRMAuthOrBillingError:
            raise
        except (httpx.RequestError, httpx.TimeoutException) as net_err:
            logger.error(f"Сетевая ошибка в list_contact_custom_fields ({subdomain}): {net_err}")
            return []

    async def patch_lead_field(
        self,
        subdomain: str,
        token: str = "",
        lead_id: int = 0,
        field_id: int = 0,
        text_value: str = "",
        value: Optional[str] = None,
        access_token: Optional[str] = None
    ) -> bool:
        """
        Запись текста ответа ИИ в кастомное поле сделки (обёртка над patch_lead_custom_fields).
        PATCH /api/v4/leads/{lead_id}
        """
        val = value if value is not None else text_value
        return await self.patch_lead_custom_fields(
            subdomain=subdomain,
            token=token,
            access_token=access_token,
            lead_id=lead_id,
            fields=[{"field_id": int(field_id), "values": [{"value": val}]}]
        )

    async def patch_lead_custom_fields(
        self,
        subdomain: str,
        token: str = "",
        lead_id: int = 0,
        fields_dict: Optional[Dict[int, Any]] = None,
        fields: Optional[List[Dict[str, Any]]] = None,
        access_token: Optional[str] = None
    ) -> bool:
        """
        Запись извлеченных полей сделки от Экстрактора.
        Принимает как словарь {field_id: val}, так и готовый список [{"field_id": ..., "values": [...]}]
        """
        t = token or access_token or ""
        custom_fields_values = []
        if fields is not None:
            custom_fields_values = fields
        elif fields_dict:
            for field_id, val in fields_dict.items():
                if val is not None and str(val).strip():
                    custom_fields_values.append({
                        "field_id": int(field_id),
                        "values": [{"value": str(val)}]
                    })

        if not custom_fields_values:
            return True

        await self.rate_limiter.wait(subdomain)
        url = f"{self._base_url(subdomain)}/api/v4/leads/{lead_id}"
        payload = {"custom_fields_values": custom_fields_values}
        try:
            client = await self.get_client()
            resp = await client.patch(url, json=payload, headers=self._headers(t))
            self._check_http_auth_or_billing(resp, subdomain)
            if resp.status_code in [200, 201]:
                logger.info(f"📊 Экстрактор обновил {len(custom_fields_values)} полей в сделке #{lead_id} ({subdomain})")
                return True
            else:
                logger.error(f"Ошибка обновления полей Экстрактором #{lead_id}: HTTP {resp.status_code} {resp.text}")
                return False
        except AmoCRMAuthOrBillingError:
            raise
        except (httpx.RequestError, httpx.TimeoutException) as net_err:
            logger.error(f"Сетевая ошибка в patch_lead_custom_fields (#{lead_id}, {subdomain}): {net_err}")
            return False

    async def patch_contact_custom_fields(
        self,
        subdomain: str,
        contact_id: int,
        fields: List[Dict[str, Any]],
        token: str = "",
        access_token: Optional[str] = None
    ) -> bool:
        """
        Запись полей контакта от Экстрактора.
        PATCH /api/v4/contacts/{contact_id}
        """
        t = token or access_token or ""
        if not fields:
            return True
        await self.rate_limiter.wait(subdomain)
        url = f"{self._base_url(subdomain)}/api/v4/contacts/{contact_id}"
        payload = {"custom_fields_values": fields}
        try:
            client = await self.get_client()
            resp = await client.patch(url, json=payload, headers=self._headers(t))
            self._check_http_auth_or_billing(resp, subdomain)
            if resp.status_code in [200, 201]:
                logger.info(f"📊 Экстрактор обновил {len(fields)} полей в контакте #{contact_id} ({subdomain})")
                return True
            else:
                logger.error(f"Ошибка обновления полей контакта #{contact_id}: HTTP {resp.status_code} {resp.text}")
                return False
        except AmoCRMAuthOrBillingError:
            raise
        except (httpx.RequestError, httpx.TimeoutException) as net_err:
            logger.error(f"Сетевая ошибка в patch_contact_custom_fields (#{contact_id}, {subdomain}): {net_err}")
            return False

    async def run_salesbot(
        self,
        subdomain: str,
        token: str = "",
        bot_id: int = 0,
        entity_id: int = 0,
        entity_type: str = "leads",
        access_token: Optional[str] = None
    ) -> bool:
        """
        Запуск Salesbot для отправки сообщения клиенту.
        POST /api/v4/bots/{bot_id}/run
        """
        t = token or access_token or ""
        await self.rate_limiter.wait(subdomain)
        url = f"{self._base_url(subdomain)}/api/v4/bots/{bot_id}/run"
        payload = {
            "entity_id": int(entity_id),
            "entity_type": entity_type
        }
        try:
            client = await self.get_client()
            resp = await client.post(url, json=payload, headers=self._headers(t))
            self._check_http_auth_or_billing(resp, subdomain)
            if resp.status_code == 202:
                logger.info(f"🤖 Запущен Salesbot #{bot_id} для {entity_type} #{entity_id} ({subdomain})")
                return True
            else:
                logger.error(f"Ошибка запуска Salesbot #{bot_id}: HTTP {resp.status_code} {resp.text}")
                return False
        except AmoCRMAuthOrBillingError:
            raise
        except (httpx.RequestError, httpx.TimeoutException) as net_err:
            logger.error(f"Сетевая ошибка в run_salesbot (bot={bot_id}, {subdomain}): {net_err}")
            return False

    async def create_operator_task(
        self,
        subdomain: str,
        token: str = "",
        entity_id: int = 0,
        text: str = "ИИ передал диалог человеку: требуется ответ оператора!",
        entity_type: str = "leads",
        responsible_user_id: Optional[int] = None,
        access_token: Optional[str] = None,
        element_id: Optional[int] = None
    ) -> bool:
        """
        Создание задачи оператору при Handover.
        POST /api/v4/tasks
        """
        t = token or access_token or ""
        eid = element_id if element_id is not None else entity_id
        await self.rate_limiter.wait(subdomain)
        url = f"{self._base_url(subdomain)}/api/v4/tasks"
        task_item: Dict[str, Any] = {
            "text": text,
            "complete_till": int(time.time() + 3600),  # Срок: +1 час
            "entity_id": int(eid),
            "entity_type": entity_type,
            "task_type_id": 1
        }
        if responsible_user_id:
            task_item["responsible_user_id"] = int(responsible_user_id)

        try:
            client = await self.get_client()
            resp = await client.post(url, json=[task_item], headers=self._headers(t))
            self._check_http_auth_or_billing(resp, subdomain)
            if resp.status_code in [200, 201]:
                logger.info(f"🚨 Создана задача оператору по {entity_type} #{eid} ({subdomain})")
                return True
            else:
                logger.error(f"Ошибка создания задачи оператору #{eid}: HTTP {resp.status_code} {resp.text}")
                return False
        except AmoCRMAuthOrBillingError:
            raise
        except (httpx.RequestError, httpx.TimeoutException) as net_err:
            logger.error(f"Сетевая ошибка в create_operator_task (#{eid}, {subdomain}): {net_err}")
            return False

    async def get_lead(self, subdomain: str, token: str = "", lead_id: int = 0, access_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Получение данных сделки (включая pipeline_id и прикрепленные контакты)"""
        t = token or access_token or ""
        await self.rate_limiter.wait(subdomain)
        url = f"{self._base_url(subdomain)}/api/v4/leads/{lead_id}?with=contacts"
        try:
            client = await self.get_client()
            resp = await client.get(url, headers=self._headers(t))
            self._check_http_auth_or_billing(resp, subdomain)
            if resp.status_code == 200:
                return resp.json()
            return None
        except AmoCRMAuthOrBillingError:
            raise
        except (httpx.RequestError, httpx.TimeoutException) as net_err:
            logger.error(f"Сетевая ошибка в get_lead (lead={lead_id}, {subdomain}): {net_err}")
            return None

    async def get_contact(self, subdomain: str, contact_id: int, token: str = "", access_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Получение данных контакта из amoCRM (включая custom_fields_values)"""
        t = token or access_token or ""
        await self.rate_limiter.wait(subdomain)
        url = f"{self._base_url(subdomain)}/api/v4/contacts/{contact_id}"
        try:
            client = await self.get_client()
            resp = await client.get(url, headers=self._headers(t))
            self._check_http_auth_or_billing(resp, subdomain)
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"get_contact: HTTP {resp.status_code} для contact_id={contact_id} ({subdomain})")
            return None
        except AmoCRMAuthOrBillingError:
            raise
        except (httpx.RequestError, httpx.TimeoutException) as net_err:
            logger.error(f"Сетевая ошибка в get_contact (contact_id={contact_id}, {subdomain}): {net_err}")
            return None

    async def get_latest_lead(self, subdomain: str, token: str = "", pipeline_id: Optional[int] = None, access_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Получение последней созданной сделки (с возможностью фильтра по воронке)"""
        t = token or access_token or ""
        await self.rate_limiter.wait(subdomain)
        url = f"{self._base_url(subdomain)}/api/v4/leads?limit=1&order[created_at]=desc"
        if pipeline_id:
            url += f"&filter[pipeline_id]={pipeline_id}"
        try:
            client = await self.get_client()
            resp = await client.get(url, headers=self._headers(t))
            self._check_http_auth_or_billing(resp, subdomain)
            if resp.status_code == 200:
                leads = resp.json().get("_embedded", {}).get("leads", [])
                if leads:
                    return leads[0]
            return None
        except AmoCRMAuthOrBillingError:
            raise
        except (httpx.RequestError, httpx.TimeoutException) as net_err:
            logger.error(f"Сетевая ошибка в get_latest_lead ({subdomain}): {net_err}")
            return None

    async def get_latest_lead_id(self, subdomain: str, token: str = "", pipeline_id: Optional[int] = None, access_token: Optional[str] = None) -> Optional[int]:
        """Получение ID последней созданной сделки в amoCRM"""
        lead = await self.get_latest_lead(subdomain, token, pipeline_id, access_token)
        return lead.get("id") if lead else None

    @staticmethod
    def detect_media_mime_type(
        content: bytes,
        content_type_header: str = "",
        file_name_or_url: str = "",
        hint_type: Optional[str] = None
    ) -> str:
        """
        Точное определение MIME-типа для передачи в Gemini API.
        Анализирует сигнатуры байтов (magic bytes), заголовок Content-Type и расширение.
        """
        # 1. Проверка сигнатур байтов (magic bytes)
        if len(content) >= 4:
            if content.startswith(b"OggS"):
                return "audio/ogg"
            if content.startswith(b"\xff\xd8\xff"):
                return "image/jpeg"
            if content.startswith(b"\x89PNG\r\n\x1a\n"):
                return "image/png"
            if content.startswith(b"RIFF") and len(content) >= 12:
                if content[8:12] == b"WAVE":
                    return "audio/wav"
                if content[8:12] == b"WEBP":
                    return "image/webp"
            if content.startswith(b"%PDF"):
                return "application/pdf"
            if content.startswith(b"ID3") or content.startswith(b"\xff\xfb") or content.startswith(b"\xff\xf3"):
                return "audio/mp3"
            # MP4 / MOV контейнеры (кругляшки и видео Telegram)
            if len(content) >= 8 and (content[4:8] == b"ftyp" or content[4:8] == b"moov"):
                return "video/mp4"

        # 2. Анализ заголовка Content-Type
        if content_type_header:
            ct = content_type_header.split(";")[0].strip().lower()
            if ct in ("audio/ogg", "audio/opus"):
                return "audio/ogg"
            if ct in ("audio/mp4", "audio/m4a", "audio/aac"):
                return "audio/m4a"
            if ct in ("audio/mpeg", "audio/mp3"):
                return "audio/mp3"
            if ct in ("audio/wav", "audio/x-wav"):
                return "audio/wav"
            if ct in ("video/mp4", "video/quicktime", "video/webm", "video/mpeg"):
                return "video/mp4" if ct != "video/quicktime" else "video/quicktime"
            if ct in ("image/jpeg", "image/jpg"):
                return "image/jpeg"
            if ct in ("image/png", "image/webp", "application/pdf"):
                return ct

        # 3. Анализ расширения файла или пути URL
        lower_name = file_name_or_url.lower().split("?")[0]
        if lower_name.endswith((".ogg", ".oga", ".opus")):
            return "audio/ogg"
        if lower_name.endswith(".mp3"):
            return "audio/mp3"
        if lower_name.endswith(".wav"):
            return "audio/wav"
        if lower_name.endswith((".m4a", ".aac")):
            return "audio/m4a"
        if lower_name.endswith((".mp4", ".m4v")):
            return "video/mp4"
        if lower_name.endswith(".mov"):
            return "video/quicktime"
        if lower_name.endswith((".jpg", ".jpeg")):
            return "image/jpeg"
        if lower_name.endswith(".png"):
            return "image/png"
        if lower_name.endswith(".webp"):
            return "image/webp"
        if lower_name.endswith(".pdf"):
            return "application/pdf"

        # 4. Fallback по хинту из вебхука amoCRM
        if hint_type == "audio":
            return "audio/ogg"
        if hint_type == "video":
            return "video/mp4"
        if hint_type == "picture":
            return "image/jpeg"

        return "application/octet-stream"

    async def download_attachment(
        self,
        url: str,
        token: Optional[str] = None,
        subdomain: Optional[str] = None,
        hint_type: Optional[str] = None,
        hint_filename: Optional[str] = None,
        max_size_bytes: int = 20 * 1024 * 1024  # 20 МБ
    ) -> Optional[Dict[str, Any]]:
        """
        Скачивание медиавложения (аудио, фото, видео/кругляшек) по ссылке из amoCRM.
        Возвращает dict: {"data_bytes": bytes, "mime_type": str, "file_name": str, "size": int}
        """
        if not url:
            return None

        # Нормализация относительных ссылок
        target_url = url
        if target_url.startswith("/") and subdomain:
            target_url = f"{self._base_url(subdomain)}{target_url}"

        parsed = urlparse(target_url)
        is_amocrm_domain = (
            parsed.netloc.endswith("amocrm.ru") or
            "amojo" in parsed.netloc or
            parsed.netloc.endswith("amocrm.com") or
            parsed.netloc.endswith("kommo.com")
        )

        headers = {}
        if token and is_amocrm_domain:
            headers["Authorization"] = f"Bearer {token}"

        try:
            client = await self.get_client()

            async def _stream_download(request_headers: dict) -> Tuple[Optional[bytes], Optional[str], int]:
                try:
                    async with client.stream("GET", target_url, headers=request_headers) as response:
                        if response.status_code != 200:
                            return None, None, response.status_code

                        content_length_str = response.headers.get("content-length")
                        if content_length_str:
                            try:
                                if int(content_length_str) > max_size_bytes:
                                    logger.warning(
                                        f"Размер вложения {target_url} ({int(content_length_str) / 1024 / 1024:.2f} МБ) превышает лимит {max_size_bytes / 1024 / 1024:.1f} МБ"
                                    )
                                    return None, None, 413
                            except ValueError:
                                pass

                        chunks = []
                        total_bytes = 0
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            total_bytes += len(chunk)
                            if total_bytes > max_size_bytes:
                                logger.warning(
                                    f"Размер потока вложения {target_url} превысил лимит {max_size_bytes / 1024 / 1024:.1f} МБ во время скачивания"
                                )
                                return None, None, 413
                            chunks.append(chunk)

                        return b"".join(chunks), response.headers.get("content-type", ""), 200
                except (httpx.RequestError, httpx.TimeoutException) as stream_err:
                    logger.warning(f"Сетевая ошибка стриминга вложения {target_url}: {stream_err}")
                    return None, None, 500

            content, content_type, status_code = await _stream_download(headers)
            # Если с заголовком авторизации на стороннем CDN/S3 получили 400/401/403, пробуем без него
            if status_code in (400, 401, 403) and headers:
                logger.debug(f"Повторная попытка загрузки {target_url} без заголовка Authorization...")
                content, content_type, status_code = await _stream_download({})

            if status_code != 200 or content is None:
                if status_code != 413:
                    logger.warning(f"Не удалось скачать вложение {target_url}: HTTP {status_code}")
                return None

            file_name = hint_filename or parsed.path.split("/")[-1] or "attachment"
            mime_type = self.detect_media_mime_type(
                content=content,
                content_type_header=content_type or "",
                file_name_or_url=file_name or target_url,
                hint_type=hint_type
            )

            logger.info(f"📥 Успешно скачано вложение: {file_name} ({mime_type}, {len(content) / 1024:.1f} КБ)")
            return {
                "data_bytes": content,
                "mime_type": mime_type,
                "file_name": file_name,
                "size": len(content)
            }
        except Exception as e:
            logger.error(f"Ошибка при скачивании вложения {target_url}: {e}")
            return None


amocrm_client = AmoCRMClient()
