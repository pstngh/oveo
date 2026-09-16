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


def test_initial_schema_accepts_only_current_modes_and_work_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "current-schema.sqlite3"
    command.upgrade(_config(database_path, monkeypatch), "head")

    connection = sqlite3.connect(database_path)
    try:
        now = "2026-09-16T12:00:00+00:00"
        connection.execute(
            "INSERT INTO users "
            "(id, username, display_name, role, password_hash, credential_version, "
            "failed_login_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("user-1", "charles", "Charles", "owner", "synthetic", 1, 0, now),
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
