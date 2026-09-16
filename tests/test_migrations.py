from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def _config(database_path: Path, monkeypatch: object) -> Config:
    monkeypatch.setenv(  # type: ignore[attr-defined]
        "OVEO_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path}",
    )
    return Config("alembic.ini")


def test_three_section_upgrade_downgrade_preserves_rows_and_legacy_data(
    tmp_path: Path, monkeypatch: object
) -> None:
    database_path = tmp_path / "three-sections.sqlite3"
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "20260916_0001")

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        now = "2026-09-16T12:00:00+00:00"
        connection.execute(
            "INSERT INTO users "
            "(id, username, display_name, role, password_hash, credential_version, "
            "failed_login_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("user-1", "charles", "Charles", "owner", "synthetic", 1, 0, now),
        )
        connection.execute(
            "INSERT INTO threads "
            "(id, owner_id, mode, voice_key, title, context_summary, "
            "summary_through_ordinal, updated_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-thread",
                "user-1",
                "alithyagpt",
                "comm_internes",
                "Legacy internal note",
                "Keep this summary exactly.",
                1,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO messages "
            "(id, thread_id, ordinal, role, actor_user_id, content_schema_version, "
            "content, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "message-1",
                "legacy-thread",
                1,
                "user",
                "user-1",
                1,
                json.dumps([{"type": "conversation", "text": "Exact legacy brief."}]),
                now,
            ),
        )
        connection.execute(
            "INSERT INTO attachments "
            "(id, message_id, storage_name, original_name, media_type, byte_count, "
            "word_count, sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "attachment-1",
                "message-1",
                "attachment-1.txt",
                "brief.txt",
                "text/plain",
                5,
                1,
                "a" * 64,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO work_items (id, thread_id, kind, active, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("work-1", "legacy-thread", "draft", 1, now),
        )
        connection.execute(
            "INSERT INTO work_versions "
            "(id, work_item_id, version_no, operation, source_text, output_text, "
            "source_word_count, brief, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "version-1",
                "work-1",
                1,
                "establish",
                "Exact source notes.",
                "Exact saved draft.",
                3,
                json.dumps({"audience": "employees"}),
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    command.upgrade(config, "head")
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            "SELECT mode, voice_key, title, context_summary FROM threads WHERE id = 'legacy-thread'"
        ).fetchone() == (
            "internal_comms",
            "comm_internes",
            "Legacy internal note",
            "Keep this summary exactly.",
        )
        assert connection.execute(
            "SELECT content FROM messages WHERE id = 'message-1'"
        ).fetchone() == (json.dumps([{"type": "conversation", "text": "Exact legacy brief."}]),)
        assert connection.execute(
            "SELECT original_name, sha256 FROM attachments WHERE id = 'attachment-1'"
        ).fetchone() == ("brief.txt", "a" * 64)
        assert connection.execute(
            "SELECT source_text, output_text, brief FROM work_versions WHERE id = 'version-1'"
        ).fetchone() == (
            "Exact source notes.",
            "Exact saved draft.",
            json.dumps({"audience": "employees"}),
        )

        now = "2026-09-16T13:00:00+00:00"
        connection.execute(
            "INSERT INTO threads "
            "(id, owner_id, mode, voice_key, title, updated_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("revision-thread", "user-1", "revision", None, "Revision", now, now),
        )
        connection.execute(
            "INSERT INTO work_items (id, thread_id, kind, active, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("work-2", "revision-thread", "revision", 1, now),
        )
        connection.commit()
    finally:
        connection.close()

    command.downgrade(config, "20260916_0001")
    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute("SELECT id, mode, voice_key FROM threads ORDER BY id").fetchall()
        assert rows == [
            ("legacy-thread", "alithyagpt", "comm_internes"),
            ("revision-thread", "alithyagpt", "comm_internes"),
        ]
        assert connection.execute("SELECT id, kind FROM work_items ORDER BY id").fetchall() == [
            ("work-1", "draft"),
            ("work-2", "draft"),
        ]
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM attachments").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM work_versions").fetchone() == (1,)
    finally:
        connection.close()

    command.upgrade(config, "head")
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("SELECT id, mode FROM threads ORDER BY id").fetchall() == [
            ("legacy-thread", "internal_comms"),
            ("revision-thread", "internal_comms"),
        ]
        assert connection.execute(
            "SELECT source_text, output_text FROM work_versions WHERE id = 'version-1'"
        ).fetchone() == ("Exact source notes.", "Exact saved draft.")
    finally:
        connection.close()
