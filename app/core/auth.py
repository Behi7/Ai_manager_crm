import secrets
import logging
from fastapi import Request, HTTPException, status
from app.core.config import settings

logger = logging.getLogger("Auth")


async def verify_admin_key(request: Request) -> bool:
    """
    Проверка ключа администратора для защиты API и админ-панели (защита от IDOR и публичного доступа).
    Поддерживает:
    - Заголовок 'X-Admin-Key'
    - Заголовок 'Authorization: Bearer <key>'
    - Query-параметр '?api_key=<key>'
    - Cookie 'admin_key'
    """
    configured_key = settings.ADMIN_API_KEY.strip() if settings.ADMIN_API_KEY else ""
    if not configured_key:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="ADMIN_API_KEY is not configured")

    # 1. Заголовок X-Admin-Key
    provided_key = request.headers.get("x-admin-key") or request.headers.get("X-Admin-Key")

    # 2. Bearer токен в Authorization
    if not provided_key:
        auth_header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
        if auth_header.lower().startswith("bearer "):
            provided_key = auth_header[7:].strip()

    # 3. Query-параметр api_key
    if not provided_key:
        provided_key = request.query_params.get("api_key")

    # 4. Cookie admin_key
    if not provided_key:
        provided_key = request.cookies.get("admin_key")

    if not provided_key or not secrets.compare_digest(provided_key.strip(), configured_key):
        client_ip = request.client.host if request.client else "unknown"
        logger.warning(
            f"Несанкционированная попытка доступа к {request.method} {request.url.path} с IP {client_ip}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Неверный или отсутствующий ключ администратора (Invalid or missing admin API key)"
        )

    return True
