from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from oveo.admin import clear_terminal_snapshots, main, schema_revisions
from oveo.config import Settings
from oveo.db import Database
from oveo.models import Generation, Message, Thread, User

ROOT = Path(__file__).parents[1]
SNAPSHOT = {"schema_version": 1, "provider_messages": [{"role": "user", "content": "x" * 500}]}


def test_schema_revisions_lists_the_shipped_migrations_head_first() -> None:
    revisions = schema_revisions(ROOT / "alembic.ini")
    # Migration files are named <revision>_<slug>.py with revisions like 20260917_0005.
    shipped = sorted(path.name[:13] for path in (ROOT / "migrations" / "versions").glob("*.py"))
    assert revisions[0] == shipped[-1]
    assert sorted(revisions) == shipped


def test_schema_revisions_command_needs_no_settings_or_database(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A deployment runs this inside the candidate image with no runtime configuration.
    monkeypatch.setenv("OVEO_ENVIRONMENT", "production")
    monkeypatch.chdir(tmp_path)
    assert main(["schema-revisions", "--config", str(ROOT / "alembic.ini")]) == 0
    lines = capsys.readouterr().out.split()
    assert lines == schema_revisions(ROOT / "alembic.ini")
    assert list(tmp_path.iterdir()) == []


async def test_clear_terminal_snapshots_is_a_dry_run_unless_applied(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, _settings, user = manager_database
    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode="translate")
        db.add(thread)
        await db.flush()
        message = Message(
            thread_id=thread.id,
            ordinal=1,
            role="user",
            actor_user_id=user.id,
            content=[{"type": "conversation", "text": "Kept message."}],
        )
        db.add(message)
        await db.flush()
        rows = {
            status: Generation(
                thread_id=thread.id if status == "running" else None,
                requester_id=user.id,
                source_message_id=message.id,
                client_request_id=f"request-{status}",
                purpose="chat",
                status=status,
                request_snapshot=SNAPSHOT,
                error_code="provider_timeout" if status == "failed" else None,
                provider_generation_id=f"gen-{status}",
            )
            for status in ("completed", "failed", "stopped", "running")
        }
        already_clear = Generation(
            requester_id=user.id,
            client_request_id="request-cleared",
            purpose="chat",
            status="completed",
            request_snapshot={},
        )
        db.add_all([*rows.values(), already_clear])
        await db.commit()
        ids = {status: generation.id for status, generation in rows.items()}

    count, size = await clear_terminal_snapshots(database, apply=False)
    assert count == 3 and size > 3 * 500
    async with database.sessions() as db:
        kept = await db.scalars(select(Generation.request_snapshot))
        assert sum(1 for snapshot in kept if snapshot) == 4

    cleared, _ = await clear_terminal_snapshots(database, apply=True)
    assert cleared == 3
    async with database.sessions() as db:
        for status, generation_id in ids.items():
            generation = await db.get(Generation, generation_id)
            assert generation is not None
            # The running generation still needs its context; diagnostics stay.
            assert generation.request_snapshot == (SNAPSHOT if status == "running" else {})
            assert generation.provider_generation_id == f"gen-{status}"
        assert (await db.scalar(select(Message.content))) == [
            {"type": "conversation", "text": "Kept message."}
        ]
    assert await clear_terminal_snapshots(database, apply=False) == (0, 0)
