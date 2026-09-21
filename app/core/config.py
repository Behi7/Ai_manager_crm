from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://ai_user:ai_secure_password_123@127.0.0.1:5433/ai_manager"
    )
    REDIS_URL: str = Field(default="redis://127.0.0.1:6380/0")
    SECRET_KEY: str = Field(default="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    GEMINI_API_KEY: str = Field(default="")
    HOST: str = Field(default="0.0.0.0")
    PORT: int = Field(default=8080)
    BASE_URL: str = Field(default="http://localhost:8080")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()

