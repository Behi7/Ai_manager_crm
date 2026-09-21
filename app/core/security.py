from cryptography.fernet import Fernet
from app.core.config import settings

_cipher_suite = Fernet(settings.SECRET_KEY.encode() if isinstance(settings.SECRET_KEY, str) else settings.SECRET_KEY)


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
    return _cipher_suite.decrypt(encrypted_token).decode("utf-8")

