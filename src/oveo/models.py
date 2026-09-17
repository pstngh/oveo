from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("credential_version >= 1", name="ck_users_credential_version"),
        CheckConstraint("failed_login_count >= 0", name="ck_users_failed_login_count"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    login_window_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    login_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Session(Base, TimestampMixin):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("credential_version >= 1", name="ck_sessions_credential_version"),
        Index("ix_sessions_user_expires", "user_id", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, nullable=False)
    csrf_token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped[User] = relationship(lazy="joined")


class Thread(Base, TimestampMixin):
    __tablename__ = "threads"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('translate', 'revision', 'internal_comms')",
            name="ck_threads_mode",
        ),
        Index("ix_threads_owner_updated", "owner_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str | None] = mapped_column(String(160))
    context_summary: Mapped[str | None] = mapped_column(Text)
    summary_through_ordinal: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Message(Base, TimestampMixin):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("thread_id", "ordinal", name="uq_messages_thread_ordinal"),
        CheckConstraint("ordinal >= 1", name="ck_messages_ordinal"),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
        CheckConstraint("content_schema_version >= 1", name="ck_messages_schema_version"),
        CheckConstraint(
            "(role = 'user' AND actor_user_id IS NOT NULL) OR "
            "(role = 'assistant' AND actor_user_id IS NULL)",
            name="ck_messages_actor",
        ),
        Index("ix_messages_thread_created", "thread_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    content_schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    content: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)


class Attachment(Base, TimestampMixin):
    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint("byte_count >= 0", name="ck_attachments_byte_count"),
        CheckConstraint("word_count >= 0", name="ck_attachments_word_count"),
        CheckConstraint(
            "role IN ('source', 'reference')",
            name="ck_attachments_role",
        ),
        CheckConstraint(
            "media_type = "
            "'application/vnd.openxmlformats-officedocument.wordprocessingml.document'",
            name="ck_attachments_docx_media_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    storage_name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(16), default="source", server_default="source", nullable=False
    )
    media_type: Mapped[str] = mapped_column(
        String(100),
        default="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        nullable=False,
    )
    document_blocks: Mapped[list[dict[str, str]]] = mapped_column(JSON, nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class Generation(Base, TimestampMixin):
    __tablename__ = "generations"
    __table_args__ = (
        UniqueConstraint(
            "requester_id", "client_request_id", name="uq_generations_requester_client_request"
        ),
        CheckConstraint(
            "purpose IN ('chat', 'prompt_handoff', 'title', 'summary', 'smoke_test')",
            name="ck_generations_purpose",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'stopping', 'completed', 'failed', 'stopped')",
            name="ck_generations_status",
        ),
        CheckConstraint("stream_revision >= 0", name="ck_generations_stream_revision"),
        Index("ix_generations_thread_created", "thread_id", "created_at"),
        Index(
            "uq_generations_one_active_per_thread",
            "thread_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running', 'stopping')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"))
    requester_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    source_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE")
    )
    result_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), unique=True
    )
    retry_of_generation_id: Mapped[str | None] = mapped_column(
        ForeignKey("generations.id", ondelete="SET NULL")
    )
    client_request_id: Mapped[str] = mapped_column(String(100), nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    request_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    partial_blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    stream_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    provider_generation_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkItem(Base, TimestampMixin):
    __tablename__ = "work_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('translation', 'revision', 'draft')",
            name="ck_work_items_kind",
        ),
        Index(
            "uq_work_items_one_active_per_thread",
            "thread_id",
            unique=True,
            sqlite_where=text("active = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class WorkVersion(Base, TimestampMixin):
    __tablename__ = "work_versions"
    __table_args__ = (
        UniqueConstraint("work_item_id", "version_no", name="uq_work_versions_item_version"),
        CheckConstraint("version_no >= 1", name="ck_work_versions_version_no"),
        CheckConstraint("source_word_count >= 0", name="ck_work_versions_source_word_count"),
        CheckConstraint(
            "operation IN ('establish', 'append', 'replace', 'full')",
            name="ck_work_versions_operation",
        ),
        CheckConstraint(
            "(docx_template_attachment_id IS NULL AND docx_blocks IS NULL) OR "
            "(docx_template_attachment_id IS NOT NULL AND docx_blocks IS NOT NULL)",
            name="ck_work_versions_docx_pair",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("work_versions.id", ondelete="SET NULL")
    )
    cause_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    output_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    brief: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    docx_template_attachment_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "attachments.id",
            ondelete="CASCADE",
            name="fk_work_versions_docx_template_attachment_id_attachments",
        )
    )
    docx_blocks: Mapped[list[dict[str, str]] | None] = mapped_column(JSON(none_as_null=True))


class UsageEvent(Base, TimestampMixin):
    __tablename__ = "usage_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('pending', 'charge', 'adjustment')",
            name="ck_usage_events_event_type",
        ),
        CheckConstraint(
            "(event_type = 'pending' AND amount_microusd IS NULL) OR "
            "(event_type IN ('charge', 'adjustment') AND amount_microusd IS NOT NULL)",
            name="ck_usage_events_amount",
        ),
        Index("ix_usage_events_provider_request", "provider", "provider_request_id"),
        Index("ix_usage_events_provider_generation", "provider", "provider_generation_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # These are intentionally not foreign keys: ledger entries survive conversation deletion.
    thread_id: Mapped[str | None] = mapped_column(String(36))
    generation_id: Mapped[str | None] = mapped_column(String(36))
    requester_id: Mapped[str | None] = mapped_column(String(36))
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    provider_generation_id: Mapped[str | None] = mapped_column(String(255))
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    amount_microusd: Mapped[int | None] = mapped_column(Integer)


@event.listens_for(UsageEvent, "before_update")
@event.listens_for(UsageEvent, "before_delete")
def _usage_events_are_append_only(*_args: object, **_kwargs: object) -> None:
    raise ValueError("usage events are append-only")
