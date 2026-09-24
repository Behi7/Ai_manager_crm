from typing import List, Union
import logging
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator

logger = logging.getLogger("Config")

DEFAULT_INSECURE_SECRET = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


class Settings(BaseSettings):
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://ai_user:ai_secure_password_123@127.0.0.1:5433/ai_manager"
    )
    REDIS_URL: str = Field(default="redis://127.0.0.1:6380/0")
    REDIS_MAX_CONNECTIONS: int = Field(default=50)
    REDIS_HEALTH_CHECK_INTERVAL: int = Field(default=30)
    REDIS_RETRY_ON_TIMEOUT: bool = Field(default=True)

    SECRET_KEY: str = Field(default=DEFAULT_INSECURE_SECRET)
    ADMIN_API_KEY: str = Field(default="ai_admin_sec_994821a7c4")
    GEMINI_API_KEY: str = Field(default="")
    HOST: str = Field(default="0.0.0.0")
    PORT: int = Field(default=8080)
    BASE_URL: str = Field(default="http://localhost:8080")
    ENVIRONMENT: str = Field(default="development")

    ALLOWED_ORIGINS: Union[List[str], str] = Field(
        default=["http://localhost:3000", "http://localhost:8080", "http://127.0.0.1:8080", "http://localhost:5173"]
    )

    @field_validator("ALLOWED_ORIGINS", mode="before")
    @classmethod
    def parse_allowed_origins(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            if v.startswith("[") and v.endswith("]"):
                import json
                try:
                    return json.loads(v)
                except Exception:
                    pass
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @field_validator("SECRET_KEY")
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        if v == DEFAULT_INSECURE_SECRET:
            logger.warning(
                "ВНИМАНИЕ: Используется небезопасный дефолтный SECRET_KEY. "
                "Обязательно задайте уникальный SECRET_KEY в файле .env!"
            )
        return v

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()

