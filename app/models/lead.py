import uuid
from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy import (
    String, Text, Boolean, BigInteger, DateTime,
    ForeignKey, UniqueConstraint, Index
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amo_lead_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    amo_contact_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    is_busy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stuck_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    handover_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    account: Mapped["Account"] = relationship("Account", back_populates="leads")
    messages: Mapped[List["ConversationMessage"]] = relationship("ConversationMessage", back_populates="lead", cascade="all, delete-orphan")
    extraction_logs: Mapped[List["ExtractionLog"]] = relationship("ExtractionLog", back_populates="lead", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("account_id", "amo_lead_id", name="uq_lead_account_amo_id"),
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # 'user' | 'assistant'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    lead: Mapped["Lead"] = relationship("Lead", back_populates="messages")

    __table_args__ = (
        Index("idx_conv_lead_time", "lead_id", "created_at"),
    )


class ProcessedWebhookMessage(Base):
    __tablename__ = "processed_webhook_messages"

    message_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lead_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

