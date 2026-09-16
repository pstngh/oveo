"""Create the initial Oveo schema.

Revision ID: 20260916_0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260916_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[object]]:
    return [sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("failed_login_count", sa.Integer(), nullable=False),
        sa.Column("login_window_started_at", sa.DateTime(timezone=True)),
        sa.Column("login_locked_until", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint("role IN ('owner', 'user')", name="ck_users_role"),
        sa.CheckConstraint("credential_version >= 1", name="ck_users_credential_version"),
        sa.CheckConstraint("failed_login_count >= 0", name="ck_users_failed_login_count"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token_hash", sa.LargeBinary(32), nullable=False, unique=True),
        sa.Column("csrf_token_hash", sa.LargeBinary(32), nullable=False),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("credential_version >= 1", name="ck_sessions_credential_version"),
    )
    op.create_index("ix_sessions_user_expires", "sessions", ["user_id", "expires_at"])
    op.create_table(
        "threads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("title", sa.String(160)),
        sa.Column("context_summary", sa.Text()),
        sa.Column("summary_through_ordinal", sa.Integer()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "mode IN ('translate', 'revision', 'internal_comms')",
            name="ck_threads_mode",
        ),
    )
    op.create_index("ix_threads_owner_updated", "threads", ["owner_id", "updated_at"])
    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "thread_id",
            sa.String(36),
            sa.ForeignKey("threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("actor_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT")),
        sa.Column("content_schema_version", sa.Integer(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("thread_id", "ordinal", name="uq_messages_thread_ordinal"),
        sa.CheckConstraint("ordinal >= 1", name="ck_messages_ordinal"),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
        sa.CheckConstraint("content_schema_version >= 1", name="ck_messages_schema_version"),
        sa.CheckConstraint(
            "(role = 'user' AND actor_user_id IS NOT NULL) OR "
            "(role = 'assistant' AND actor_user_id IS NULL)",
            name="ck_messages_actor",
        ),
    )
    op.create_index("ix_messages_thread_created", "messages", ["thread_id", "created_at"])
    op.create_table(
        "attachments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "message_id",
            sa.String(36),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("storage_name", sa.String(80), nullable=False, unique=True),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("byte_count >= 0", name="ck_attachments_byte_count"),
        sa.CheckConstraint("word_count >= 0", name="ck_attachments_word_count"),
    )
    op.create_table(
        "generations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.id", ondelete="CASCADE")),
        sa.Column(
            "requester_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_message_id",
            sa.String(36),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "result_message_id",
            sa.String(36),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            unique=True,
        ),
        sa.Column(
            "retry_of_generation_id",
            sa.String(36),
            sa.ForeignKey("generations.id", ondelete="SET NULL"),
        ),
        sa.Column("client_request_id", sa.String(100), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("request_snapshot", sa.JSON(), nullable=False),
        sa.Column("partial_blocks", sa.JSON(), nullable=False),
        sa.Column("stream_revision", sa.Integer(), nullable=False),
        sa.Column("provider_request_id", sa.String(255)),
        sa.Column("provider_generation_id", sa.String(255)),
        sa.Column("error_code", sa.String(80)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.UniqueConstraint(
            "requester_id",
            "client_request_id",
            name="uq_generations_requester_client_request",
        ),
        sa.CheckConstraint(
            "purpose IN ('chat', 'prompt_handoff', 'title', 'summary', 'smoke_test')",
            name="ck_generations_purpose",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'stopping', 'completed', 'failed', 'stopped')",
            name="ck_generations_status",
        ),
        sa.CheckConstraint("stream_revision >= 0", name="ck_generations_stream_revision"),
    )
    op.create_index("ix_generations_thread_created", "generations", ["thread_id", "created_at"])
    op.create_index(
        "uq_generations_one_active_per_thread",
        "generations",
        ["thread_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running', 'stopping')"),
    )
    op.create_table(
        "work_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "thread_id",
            sa.String(36),
            sa.ForeignKey("threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('translation', 'revision', 'draft')",
            name="ck_work_items_kind",
        ),
    )
    op.create_index(
        "uq_work_items_one_active_per_thread",
        "work_items",
        ["thread_id"],
        unique=True,
        sqlite_where=sa.text("active = 1"),
    )
    op.create_table(
        "work_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "work_item_id",
            sa.String(36),
            sa.ForeignKey("work_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column(
            "parent_version_id",
            sa.String(36),
            sa.ForeignKey("work_versions.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "cause_message_id",
            sa.String(36),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
        ),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("output_text", sa.Text(), nullable=False),
        sa.Column("source_word_count", sa.Integer(), nullable=False),
        sa.Column("brief", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("work_item_id", "version_no", name="uq_work_versions_item_version"),
        sa.CheckConstraint("version_no >= 1", name="ck_work_versions_version_no"),
        sa.CheckConstraint("source_word_count >= 0", name="ck_work_versions_source_word_count"),
        sa.CheckConstraint(
            "operation IN ('establish', 'append', 'replace', 'full')",
            name="ck_work_versions_operation",
        ),
    )
    op.create_table(
        "usage_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("thread_id", sa.String(36)),
        sa.Column("generation_id", sa.String(36)),
        sa.Column("requester_id", sa.String(36)),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("provider_request_id", sa.String(255)),
        sa.Column("provider_generation_id", sa.String(255)),
        sa.Column("dedupe_key", sa.String(255), nullable=False, unique=True),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("amount_microusd", sa.Integer()),
        *_timestamps(),
        sa.CheckConstraint(
            "event_type IN ('pending', 'charge', 'adjustment')",
            name="ck_usage_events_event_type",
        ),
        sa.CheckConstraint(
            "(event_type = 'pending' AND amount_microusd IS NULL) OR "
            "(event_type IN ('charge', 'adjustment') AND amount_microusd IS NOT NULL)",
            name="ck_usage_events_amount",
        ),
    )
    op.create_index(
        "ix_usage_events_provider_request",
        "usage_events",
        ["provider", "provider_request_id"],
    )
    op.create_index(
        "ix_usage_events_provider_generation",
        "usage_events",
        ["provider", "provider_generation_id"],
    )
    op.execute(
        "CREATE TRIGGER usage_events_no_update BEFORE UPDATE ON usage_events "
        "BEGIN SELECT RAISE(ABORT, 'usage_events are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER usage_events_no_delete BEFORE DELETE ON usage_events "
        "BEGIN SELECT RAISE(ABORT, 'usage_events are append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS usage_events_no_delete")
    op.execute("DROP TRIGGER IF EXISTS usage_events_no_update")
    op.drop_index("ix_usage_events_provider_generation", table_name="usage_events")
    op.drop_index("ix_usage_events_provider_request", table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_table("work_versions")
    op.drop_index("uq_work_items_one_active_per_thread", table_name="work_items")
    op.drop_table("work_items")
    op.drop_index("uq_generations_one_active_per_thread", table_name="generations")
    op.drop_index("ix_generations_thread_created", table_name="generations")
    op.drop_table("generations")
    op.drop_table("attachments")
    op.drop_index("ix_messages_thread_created", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_threads_owner_updated", table_name="threads")
    op.drop_table("threads")
    op.drop_index("ix_sessions_user_expires", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("users")
