from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def _config(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv(
        "OVEO_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path}",
    )
    return Config("alembic.ini")


def test_alembic_head_matches_sqlalchemy_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path / "schema-drift.sqlite3", monkeypatch)
    command.upgrade(config, "head")

    command.check(config)


def test_current_schema_accepts_only_current_modes_and_work_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "current-schema.sqlite3"
    command.upgrade(_config(database_path, monkeypatch), "head")

    connection = sqlite3.connect(database_path)
    try:
        now = "2026-09-16T12:00:00+00:00"
        attachment_columns = {
            row[1]: row[3] for row in connection.execute("PRAGMA table_info(attachments)")
        }
        assert attachment_columns["document_blocks"] == 1
        connection.execute(
            "INSERT INTO users "
            "(id, username, display_name, password_hash, credential_version, "
            "failed_login_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("user-1", "charles", "Charles", "synthetic", 1, 0, now),
        )
        for mode in ("translate", "revision", "internal_comms"):
            thread_id = f"{mode}-thread"
            connection.execute(
                "INSERT INTO threads "
                "(id, owner_id, mode, title, updated_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (thread_id, "user-1", mode, mode, now, now),
            )
        for thread_mode, kind in (
            ("translate", "translation"),
            ("revision", "revision"),
            ("internal_comms", "draft"),
        ):
            connection.execute(
                "INSERT INTO work_items (id, thread_id, kind, active, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"{kind}-work", f"{thread_mode}-thread", kind, 1, now),
            )
        connection.execute(
            "INSERT INTO messages "
            "(id, thread_id, ordinal, role, actor_user_id, content_schema_version, "
            "content, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("message-1", "translate-thread", 1, "user", "user-1", 1, "[]", now),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO attachments "
                "(id, message_id, storage_name, original_name, media_type, document_blocks, "
                "byte_count, word_count, sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "attachment-1",
                    "message-1",
                    "00000000-0000-0000-0000-000000000000.docx",
                    "source.docx",
                    "text/plain",
                    "[]",
                    1,
                    1,
                    "0" * 64,
                    now,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO threads "
                "(id, owner_id, mode, title, updated_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("unsupported-thread", "user-1", "unsupported", "Unsupported", now, now),
            )
        connection.commit()
    finally:
        connection.close()


def test_docx_migration_clears_conversations_and_preserves_accounts_and_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "remove-role.sqlite3"
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "20260916_0001")

    connection = sqlite3.connect(database_path)
    try:
        now = "2026-09-16T12:00:00+00:00"
        connection.execute(
            "INSERT INTO users "
            "(id, username, display_name, role, password_hash, credential_version, "
            "failed_login_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("user-1", "charles", "Charles", "owner", "synthetic", 1, 0, now),
        )
        connection.execute(
            "INSERT INTO threads "
            "(id, owner_id, mode, title, updated_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("thread-1", "user-1", "translate", "Preserved", now, now),
        )
        connection.execute(
            "INSERT INTO messages "
            "(id, thread_id, ordinal, role, actor_user_id, content_schema_version, "
            "content, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("message-1", "thread-1", 1, "user", "user-1", 1, "[]", now),
        )
        connection.execute(
            "INSERT INTO attachments "
            "(id, message_id, storage_name, original_name, media_type, byte_count, "
            "word_count, sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "attachment-1",
                "message-1",
                "00000000-0000-0000-0000-000000000000.txt",
                "legacy.txt",
                "text/plain",
                6,
                1,
                "0" * 64,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO generations "
            "(id, thread_id, requester_id, source_message_id, client_request_id, "
            "purpose, status, request_snapshot, partial_blocks, stream_revision, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "generation-1",
                "thread-1",
                "user-1",
                "message-1",
                "request-1",
                "chat",
                "completed",
                "{}",
                "[]",
                0,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO work_items (id, thread_id, kind, active, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("work-1", "thread-1", "translation", 1, now),
        )
        connection.execute(
            "INSERT INTO work_versions "
            "(id, work_item_id, version_no, cause_message_id, operation, source_text, "
            "output_text, source_word_count, brief, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "version-1",
                "work-1",
                1,
                "message-1",
                "establish",
                "legacy",
                "ancien",
                1,
                "{}",
                now,
            ),
        )
        connection.execute(
            "INSERT INTO usage_events "
            "(id, thread_id, generation_id, requester_id, provider, dedupe_key, "
            "event_type, purpose, amount_microusd, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "usage-1",
                "thread-1",
                "generation-1",
                "user-1",
                "openrouter",
                "usage-1",
                "charge",
                "chat",
                123,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    command.upgrade(config, "head")

    connection = sqlite3.connect(database_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        assert "role" not in columns
        assert connection.execute("SELECT username, display_name FROM users").fetchall() == [
            ("charles", "Charles")
        ]
        for statement in (
            "SELECT count(*) FROM threads",
            "SELECT count(*) FROM messages",
            "SELECT count(*) FROM attachments",
            "SELECT count(*) FROM generations",
            "SELECT count(*) FROM work_items",
            "SELECT count(*) FROM work_versions",
        ):
            assert connection.execute(statement).fetchone() == (0,)
        assert connection.execute(
            "SELECT requester_id, amount_microusd FROM usage_events"
        ).fetchall() == [("user-1", 123)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
