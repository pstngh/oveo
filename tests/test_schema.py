from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oveo.db import Database
from oveo.models import Generation, Message, Thread, UsageEvent, WorkItem
from tests.conftest import add_user


async def test_sqlite_pragmas_are_enabled(database: Database) -> None:
    async with database.engine.connect() as connection:
        assert await connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert await connection.scalar(text("PRAGMA busy_timeout")) == 5000
        assert await connection.scalar(text("PRAGMA journal_mode")) == "wal"


async def test_one_active_generation_per_thread_and_request_idempotency(
    db: AsyncSession,
) -> None:
    user = await add_user(db, "charles", role="owner")
    thread = Thread(owner_id=user.id, mode="translate", voice_key=None)
    db.add(thread)
    await db.flush()
    thread_id = thread.id
    user_id = user.id
    db.add(
        Generation(
            thread_id=thread_id,
            requester_id=user_id,
            client_request_id="request-1",
            purpose="chat",
            status="running",
        )
    )
    await db.commit()

    db.add(
        Generation(
            thread_id=thread_id,
            requester_id=user_id,
            client_request_id="request-2",
            purpose="chat",
            status="queued",
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()

    existing = await db.scalar(
        select(Generation).where(Generation.client_request_id == "request-1")
    )
    assert existing is not None
    existing.status = "completed"
    await db.commit()
    db.add(
        Generation(
            thread_id=thread_id,
            requester_id=user_id,
            client_request_id="request-2",
            purpose="chat",
            status="queued",
        )
    )
    await db.commit()

    db.add(
        Generation(
            thread_id=thread_id,
            requester_id=user_id,
            client_request_id="request-2",
            purpose="chat",
            status="failed",
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_only_one_active_work_item_per_thread(db: AsyncSession) -> None:
    user = await add_user(db, "yousra")
    thread = Thread(owner_id=user.id, mode="translate", voice_key=None)
    db.add(thread)
    await db.flush()
    db.add(WorkItem(thread_id=thread.id, kind="translation", active=True))
    await db.commit()
    db.add(WorkItem(thread_id=thread.id, kind="translation", active=True))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_thread_delete_cascades_content_but_preserves_usage(db: AsyncSession) -> None:
    user = await add_user(db, "charles", role="owner")
    thread = Thread(owner_id=user.id, mode="translate", voice_key=None)
    db.add(thread)
    await db.flush()
    message = Message(
        thread_id=thread.id,
        ordinal=1,
        role="user",
        actor_user_id=user.id,
        content=[{"type": "conversation", "text": "synthetic source"}],
    )
    db.add(message)
    await db.flush()
    generation = Generation(
        thread_id=thread.id,
        requester_id=user.id,
        source_message_id=message.id,
        client_request_id="request-delete",
        purpose="chat",
        status="completed",
    )
    db.add(generation)
    await db.flush()
    usage = UsageEvent(
        thread_id=thread.id,
        generation_id=generation.id,
        requester_id=user.id,
        provider="openrouter",
        provider_request_id="provider-1",
        dedupe_key="provider-1:charge",
        event_type="charge",
        purpose="chat",
        amount_microusd=1234,
    )
    db.add(usage)
    await db.commit()

    await db.execute(delete(Thread).where(Thread.id == thread.id))
    await db.commit()
    assert await db.scalar(select(func.count()).select_from(Message)) == 0
    assert await db.scalar(select(func.count()).select_from(Generation)) == 0
    retained = await db.scalar(select(UsageEvent))
    assert retained is not None
    assert retained.amount_microusd == 1234


def test_initial_alembic_migration_and_append_only_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "migrated.sqlite3"
    monkeypatch.setenv("OVEO_DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    config = Config("alembic.ini")
    command.upgrade(config, "head")

    connection = sqlite3.connect(database_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "users",
            "sessions",
            "threads",
            "messages",
            "attachments",
            "generations",
            "work_items",
            "work_versions",
            "usage_events",
        } <= tables
        connection.execute(
            "INSERT INTO usage_events "
            "(id, provider, dedupe_key, event_type, purpose, amount_microusd, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("event", "openrouter", "dedupe", "charge", "chat", 1, "2026-09-16"),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE usage_events SET amount_microusd = 2 WHERE id = 'event'")
    finally:
        connection.close()
