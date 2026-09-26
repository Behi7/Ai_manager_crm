import enum
import uuid
from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy import (
    String, Text, Boolean, BigInteger, LargeBinary,
    Numeric, Integer, DateTime, ForeignKey, Enum as SQLEnum, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB
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
    leads: Mapped[List["Lead"]] = relationship("Lead", back_populates="account", cascade="all, delete-orphan", lazy="noload")


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
    stages_json: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True, default=list)
    enabled_stage_ids: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True, default=None)
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
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="lead")  # 'lead' или 'contact'
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_hint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    overwrite_if_filled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    account: Mapped["Account"] = relationship("Account", back_populates="field_mappings")

    __table_args__ = (
        UniqueConstraint("account_id", "entity_type", "amo_field_id", name="uq_field_account_entity_amo_id"),
    )


DEFAULT_COMMENT_PROMPT = (
    "Ты — вежливый и дружелюбный ИИ-менеджер. Твоя задача — отвечать на комментарии клиентов под постами и Reels в соцсетях.\n\n"
    "ПРАВИЛА ОТВЕТА:\n"
    "1. Отвечай строго на том языке, на котором написал клиент (узбекский или русский).\n"
    "2. Если клиент прислал '+', '++', огонёк '🔥', смайлик или вопрос о цене/наличии:\n"
    "   - На узбекском: «Assalomu alaykum! Qiziqishingiz uchun rahmat. Narxlar va batafsil ma'lumotni Direct-ga yubordik 👉 {direct_link} (yoki shaxsiy xabarlaringizni tekshiring 📩)»\n"
    "   - На русском: «Здравствуйте! Спасибо за интерес! Отправили подробности и цены вам в Direct 👉 {direct_link} (или проверьте личные сообщения 📩)»\n"
    "3. Если клиент задал конкретный вопрос по товару или услуге — ответь на вопрос кратко (1-2 предложения) по базе знаний и обязательно предложи продолжить в Direct: {direct_link}.\n"
    "4. Твой ответ публичный, поэтому держи его кратким, дружелюбным и без длинных списков вопросов."
)

DEFAULT_EXTRACTOR_SYSTEM_PROMPT = (
    "Ты — аналитик CRM. Твоя задача — внимательно изучить диалог между Клиентом и Менеджером, "
    "а также прикрепленные медиафайлы (голосовые сообщения, кругляшки, фото чеков/товаров) "
    "и извлечь ТОЧНЫЕ факты о клиенте и его запросе для сохранения в CRM.\n\n"
    "ПРАВИЛА:\n"
    "1. Извлекай только ту информацию, о которой клиент явно сообщил сам или подтвердил слова менеджера.\n"
    "2. ВНИМАНИЕ: Если у поля уже указано 'Текущее значение в CRM' (не ПУСТО), и клиент в ПОСЛЕДНЕМ сообщении явно НЕ исправлял и НЕ менял это значение на другое — ОБЯЗАТЕЛЬНО верни null (или не включай это поле в результат)! Категорически запрещено повторно извлекать или перефразировать старые ответы из истории диалога, которые уже записаны в CRM.\n"
    "3. ПРАВИЛО ДЛЯ ИМЕНИ КОНТАКТА (field_contact_sys_name): Если клиент назвал своё имя в диалоге — верни это имя. Если в диалоге ещё не называл, посмотри на ник мессенджера: если там написано настоящее человеческое имя (например Salohiddin, Алишер, Hojiakbar, Дильшод) — верни это имя! Если же в нике написано название компании/отдела (Texnik Bo'lim, Marketing Markazi, магазин) или случайный набор символов (йцуке123, user777, смайлики) — НЕ используй ник и верни null!\n"
    "4. Если поле не упоминалось или нет уверенности — НЕ добавляй его в результат или укажи null.\n"
    "5. Не придумывай и не домысливай факты.\n"
    "6. Верни JSON-объект, где ключи — это строго идентификаторы полей (например field_lead_12345), а значения — только НОВЫЕ или ИЗМЕНЕННЫЕ данные."
)

DEFAULT_COMMUNICATOR_QUALIFICATION_PROMPT = (
    "СТРОГИЕ ПРАВИЛА КВАЛИФИКАЦИИ И ОБЩЕНИЯ (ФАЗА СБОРА ДАННЫХ):\n"
    "1. ⚠️ ВНИМАНИЕ — КВАЛИФИКАЦИЯ ЕЩЁ НЕ ЗАВЕРШЕНА! Пока список целевых данных выше не пуст, КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО говорить клиенту, что «все данные записаны», «специалисты скоро свяжутся» или спрашивать «есть ли у вас ещё вопросы?» (это условие из системного промпта действует ТОЛЬКО когда список целевых данных пуст!).\n"
    "2. ПРАВИЛО ОДНОГО ВОПРОСА: Задавай максимум ОДИН ненавязчивый вопрос в сообщении! (по одному из недостающих параметров из списка выше). Категорически запрещено присылать списки вопросов, анкеты или опросники.\n"
    "3. СНАЧАЛА ПОЛЬЗА/ОТВЕТ, ЗАТЕМ ВОПРОС: Всегда сначала дай полноценный, дружелюбный ответ на вопрос или реплику клиента, и только затем органично задай уместный вопрос по одному недостающему параметру из списка выше.\n"
    "4. НЕ ПЕРЕСПРАШИВАЙ: Если клиент уже сообщил информацию или она указана в уже известных данных, не спрашивай повторно — спрашивай только недостающие целевые параметры из списка выше.\n"
    "5. ЗАВЕРШЕНИЕ КВАЛИФИКАЦИИ: Когда все ключевые квалификационные параметры сделки уже выяснены (список целевых данных пуст) — больше НЕ задавай квалификационных вопросов по анкете. Поблагодари клиента, сообщи что все данные записаны и специалисты свяжутся с ним в скором времени, и спроси, остались ли у него вопросы."
)

DEFAULT_COMMUNICATOR_RULES_PROMPT = (
    "ПРАВИЛА РАБОТЫ С КАТАЛОГОМ И СТРОЖАЙШИЕ ПРАВИЛА ВЫВОДА:\n"
    "1. СТРОГИЙ ЗАПРЕТ НА СПАМ КАТАЛОГОМ И ПОВЕДЕНИЕ ПРОДАЖНИКА НА ПРИВЕТСТВИЕ: Если клиент просто поздоровался ('Привет', 'Salom', 'Assalomu alaykum') — НЕ вываливай сразу весь прайс-лист, но и КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО отвечать шаблонной фразой справочной службы «Чем я могу вам помочь?» / «Sizga qanday yordam bera olaman?». Ты — активный менеджер по продажам! Поздоровайся (по имени, если оно известно), в 1 короткой фразе обозначь направление компании (согласно системному промпту) и сразу перехвати инициативу: задай первый живой вопрос по квалификации клиента из списка целевых данных.\n"
    "2. ТОЧЕЧНАЯ РЕКОМЕНДАЦИЯ И ВЫГОДА: Рекомендуй конкретный продукт (1, максимум 2 подходящих варианта) только тогда, когда клиент сам спросил о товарах/ценах или когда стали понятны его потребности. Называй цены и характеристики строго из базы знаний.\n"
    "3. ДАННЫЕ КЛИЕНТА ≠ КОНТАКТЫ КОМПАНИИ: Данные из блока «УЖЕ ИЗВЕСТНЫЕ ДАННЫЕ О КЛИЕНТЕ И СДЕЛКЕ» — это анкетные данные САМОГО КЛИЕНТА. Никогда не выдавай телефон клиента за номер нашей компании.\n"
    "4. СТРОЖАЙШИЕ ПРАВИЛА ВЫВОДА:\n"
    "   - Запрещено использовать плейсхолдеры в квадратных скобках вида [цена], [имя], [товар]. Если точная информация неизвестна, ответь как живой менеджер: скажи, что уточняешь детали у коллег, либо задай уточняющий вопрос.\n"
    "   - Не здоровайся повторно, если в диалоге уже есть приветствие.\n"
    "   - Будь лаконичным (1-3 живых, емких предложения), дружелюбным и естественным.\n"
    "   - Отвечай строго на том языке, на котором пишет или говорит клиент (русский, узбекский или английский)."
)


class AIConfig(Base):
    __tablename__ = "ai_configs"

    account_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    communicator_prompt: Mapped[str] = mapped_column(
        Text,
        default=(
            "Ты — профессиональный, живой и дружелюбный менеджер отдела продаж нашей компании. "
            "Твоя цель — помочь клиенту выбрать подходящее решение, ответить на вопросы по нашим продуктам "
            "и бережно провести его по этапам сделки."
        ),
        nullable=False
    )
    communicator_model: Mapped[str] = mapped_column(String(100), default="gemini-3.1-flash-lite", nullable=False)
    fallback_communicator_model: Mapped[str] = mapped_column(String(100), default="gemini-3.5-flash", nullable=False)
    extractor_model: Mapped[str] = mapped_column(String(100), default="gemini-3.1-flash-lite", nullable=False)
    fallback_extractor_model: Mapped[str] = mapped_column(String(100), default="gemini-3.5-flash", nullable=False)
    temperature: Mapped[float] = mapped_column(Numeric(3, 2), default=0.4, nullable=False)
    handover_after_stuck: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    debounce_delay_seconds: Mapped[float] = mapped_column(Numeric(4, 1), default=2.5, server_default="2.5", nullable=False)
    gemini_api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    knowledge_base: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    knowledge_mode: Mapped[str] = mapped_column(String(50), default="plain_text", nullable=False)
    gemini_cache_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    gemini_cache_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    comment_prompt: Mapped[Optional[str]] = mapped_column(Text, default=DEFAULT_COMMENT_PROMPT, nullable=True)
    direct_link: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    extractor_system_prompt: Mapped[Optional[str]] = mapped_column(Text, default=DEFAULT_EXTRACTOR_SYSTEM_PROMPT, nullable=True)
    communicator_qualification_prompt: Mapped[Optional[str]] = mapped_column(Text, default=DEFAULT_COMMUNICATOR_QUALIFICATION_PROMPT, nullable=True)
    communicator_rules_prompt: Mapped[Optional[str]] = mapped_column(Text, default=DEFAULT_COMMUNICATOR_RULES_PROMPT, nullable=True)

    account: Mapped["Account"] = relationship("Account", back_populates="ai_config")

