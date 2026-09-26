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


def encrypt_secret_str(raw: str | None) -> str | None:
    """Шифрует строковый секрет (например, gemini_api_key) в строку с префиксом enc:v1:"""
    if not raw or not isinstance(raw, str):
        return None
    val = raw.strip()
    if not val:
        return None
    if val.startswith("enc:v1:"):
        return val
    token = _cipher_suite.encrypt(val.encode("utf-8")).decode("ascii")
    return f"enc:v1:{token}"


def decrypt_secret_str(stored: str | None) -> str | None:
    """Расшифровывает строковый секрет (с обратной совместимостью для открытых строк)"""
    if not stored or not isinstance(stored, str):
        return None
    val = stored.strip()
    if not val:
        return None
    if val.startswith("enc:v1:"):
        try:
            return _cipher_suite.decrypt(val[7:].encode("ascii")).decode("utf-8")
        except Exception:
            return None
    return val


def mask_secret_str(stored: str | None) -> str:
    """Возвращает маскированное представление ключа ('***abcd') для безопасного отображения в API"""
    plain = decrypt_secret_str(stored)
    if not plain:
        return ""
    if len(plain) > 4:
        return f"***{plain[-4:]}"
    return "***"

