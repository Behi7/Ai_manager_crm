from cryptography.fernet import Fernet, InvalidToken
from app.core.config import settings
from app.services.amocrm_client import AmoCRMAuthOrBillingError

import base64
import hashlib

def _build_cipher_suite() -> Fernet:
    raw_key = settings.SECRET_KEY.encode("utf-8") if isinstance(settings.SECRET_KEY, str) else settings.SECRET_KEY
    try:
        return Fernet(raw_key)
    except Exception:
        # Если в .env задана произвольная строка вместо 32-байтного url-safe base64 ключа,
        # детерминированно приводим её к валидному 32-байтному ключу Fernet через SHA-256
        derived_key = base64.urlsafe_b64encode(hashlib.sha256(raw_key).digest())
        return Fernet(derived_key)

_cipher_suite = _build_cipher_suite()


def encrypt_token(token: str) -> bytes:
    """Шифрует токен amoCRM для безопасного хранения в БД"""
    if not token:
        return b""
    return _cipher_suite.encrypt(token.encode("utf-8"))


def decrypt_token(encrypted_token: bytes) -> str:
    """Расшифровывает токен amoCRM"""
    if not encrypted_token:
        return ""
    if isinstance(encrypted_token, memoryview):
        encrypted_token = encrypted_token.tobytes()
    try:
        return _cipher_suite.decrypt(encrypted_token).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise AmoCRMAuthOrBillingError(
            401,
            "Не удалось расшифровать токен amoCRM (ключ шифрования изменен или токен поврежден). Переподключите аккаунт."
        ) from exc
