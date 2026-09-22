import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from app.models.lead import Lead
from sqlalchemy import (
    String, Text, Boolean, BigInteger, LargeBinary,
    Numeric, Integer, DateTime, ForeignKey, Enum as SQLEnum, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class AccountStatus(str, enum.Enum):
    """Статусы жизненного цикла интеграции аккаунта amoCRM"""
    PENDING_VALIDATION = "pending_validation"
    FIELD_CREATED = "field_created"
    AWAITING_MANUAL_BOT = "awaiting_manual_bot"
    BOT_LINKED = "bot_linked"
    AWAITING_PIPELINE_SETUP = "awaiting_pipeline_setup"  # Ожидание настройки и включения воронок продаж
    CONFIGURED = "configured"
    VERIFIED = "verified"
    ERROR = "error"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subdomain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    amo_account_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    encrypted_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encrypted_refresh_token: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    token_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[AccountStatus] = mapped_column(
        SQLEnum(AccountStatus, name="account_status", native_enum=True),
        default=AccountStatus.PENDING_VALIDATION,
        nullable=False
    )

    ai_reply_field_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    bot_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)

    webhook_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    webhook_auto_registered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    webhook_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    # Связи
    pipelines: Mapped[List["Pipeline"]] = relationship("Pipeline", back_populates="account", cascade="all, delete-orphan", lazy="selectin")
    field_mappings: Mapped[List["FieldMapping"]] = relationship("FieldMapping", back_populates="account", cascade="all, delete-orphan", lazy="selectin")
    ai_config: Mapped[Optional["AIConfig"]] = relationship("AIConfig", back_populates="account", uselist=False, cascade="all, delete-orphan", lazy="selectin")
    leads: Mapped[List["Lead"]] = relationship("Lead", back_populates="account", cascade="all, delete-orphan", lazy="selectin")


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amo_pipeline_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    account: Mapped["Account"] = relationship("Account", back_populates="pipelines")

    __table_args__ = (
        UniqueConstraint("account_id", "amo_pipeline_id", name="uq_pipeline_account_amo_id"),
    )


class FieldMapping(Base):
    __tablename__ = "field_mappings"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amo_field_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    field_name: Mapped[str] = mapped_column(Text, nullable=False)
    field_type: Mapped[str] = mapped_column(String(50), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_hint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    overwrite_if_filled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    account: Mapped["Account"] = relationship("Account", back_populates="field_mappings")

    __table_args__ = (
        UniqueConstraint("account_id", "amo_field_id", name="uq_field_account_amo_id"),
    )


class AIConfig(Base):
    __tablename__ = "ai_configs"

    account_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    communicator_prompt: Mapped[str] = mapped_column(
        Text,
        default=(
            "Ты — профессиональный, живой и вежливый ИИ-менеджер по работе с клиентами. "
            "Отвечай кратко, по делу, на том языке, на котором пишет клиент (русский или узбекский). "
            "СТРОГО ЗАПРЕЩЕНО: "
            "1. Использовать плейсхолдеры в квадратных скобках вида [цена], [имя], [ссылка]. "
            "2. Здороваться повторно, если диалог уже идёт. "
            "3. Игнорировать то, что клиент уже сообщил ранее в этом диалоге. "
            "Если клиент торопит («алло», «вы тут?») — подтверди присутствие и продолжи диалог по теме."
        ),
        nullable=False
    )
    communicator_model: Mapped[str] = mapped_column(String(100), default="gemini-3.1-flash-lite", nullable=False)
    extractor_model: Mapped[str] = mapped_column(String(100), default="gemini-3.1-flash-lite", nullable=False)
    temperature: Mapped[float] = mapped_column(Numeric(3, 2), default=0.4, nullable=False)
    handover_after_stuck: Mapped[int] = mapped_column(Integer, default=4, nullable=False)

    account: Mapped["Account"] = relationship("Account", back_populates="ai_config")

