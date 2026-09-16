from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import func, select

from oveo.auth import hash_password
from oveo.config import Settings
from oveo.db import Database
from oveo.generation import (
    ActiveGenerationError,
    GenerationManager,
    ProviderCompletion,
    ProviderError,
    ProviderRequest,
    _apply_base_replacements,
)
from oveo.models import Base, Generation, Message, Thread, UsageEvent, User, WorkItem, WorkVersion
from oveo.protocol import ProtocolError

_SUCCESS = (
    b'{"v":1,"event":"response_start"}\n'
    b'{"v":1,"event":"block_start","id":"b1","type":"deliverable"}\n'
    b'{"v":1,"event":"block_delta","id":"b1","text":"Translated text"}\n'
    b'{"v":1,"event":"block_end","id":"b1"}\n'
    b'{"v":1,"event":"state","operation":"none"}\n'
    b'{"v":1,"event":"response_end"}\n'
)

_ESTABLISH = (
    b'{"v":1,"event":"response_start"}\n'
    b'{"v":1,"event":"block_start","id":"b1","type":"deliverable"}\n'
    b'{"v":1,"event":"block_delta","id":"b1","text":"Bonjour"}\n'
    b'{"v":1,"event":"block_end","id":"b1"}\n'
    b'{"v":1,"event":"state","operation":"establish","source":"Hello",'
    b'"output":"Bonjour","brief":{"direction":"en-US-fr-CA"}}\n'
    b'{"v":1,"event":"response_end"}\n'
)


class BlockingProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del request, emit
        self.started.set()
        await cancel_event.wait()
        raise asyncio.CancelledError


class FailThenSucceedProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del request, cancel_event
        self.calls += 1
        if self.calls == 1:
            raise ProviderError("provider_network")
        await emit(_SUCCESS)  # type: ignore[operator]
        return ProviderCompletion(provider_request_id="provider-retry", cost_microusd=7)


class CanonicalReconcileProvider:
    def __init__(self) -> None:
        self.title_done = asyncio.Event()

    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del cancel_event
        if request.purpose == "title":
            await emit(b"English French Translation")  # type: ignore[operator]
            self.title_done.set()
            return ProviderCompletion(cost_microusd=0)
        await emit(_ESTABLISH)  # type: ignore[operator]
        return ProviderCompletion(
            provider_request_id="request-canonical",
            provider_generation_id="generation-canonical",
        )

    async def reconcile_cost(self, provider_generation_id: str) -> int | None:
        assert provider_generation_id == "generation-canonical"
        return 55


class CompactingProvider:
    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del cancel_event
        if request.purpose == "summary":
            await emit(  # type: ignore[operator]
                b'{"version":1,"summary":"Earlier decisions.","unresolved":["Tone"]}'
            )
            return ProviderCompletion(cost_microusd=11)
        await emit(_SUCCESS)  # type: ignore[operator]
        return ProviderCompletion(cost_microusd=22)


@pytest.fixture
async def manager_database(tmp_path: Path) -> AsyncIterator[tuple[Database, Settings, User]]:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'manager.sqlite3'}",
        data_dir=tmp_path,
        attachments_dir=tmp_path / "attachments",
        frontend_dir=tmp_path / "frontend",
        secure_cookies=False,
        provider_retry_attempts=1,
    )
    database = Database(settings.database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with database.sessions() as db:
        user = User(
            username="charles",
            display_name="Charles",
            role="owner",
            password_hash=hash_password("test password"),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    try:
        yield database, settings, user
    finally:
        await database.dispose()


async def _wait_status(
    manager: GenerationManager, generation_id: str, expected: set[str]
) -> dict[str, object]:
    for _ in range(100):
        snapshot = await manager.get_snapshot(generation_id)
        assert snapshot is not None
        if snapshot["status"] in expected:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError(f"generation did not reach {expected}")


async def test_same_thread_guard_stop_and_snapshot_reconnect(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    provider = BlockingProvider()
    manager = GenerationManager(database, settings, provider)
    first = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="first",
        text="Translate this synthetic sentence.",
        attachment=None,
        owner_id=user.id,
        mode="translate",
    )
    await provider.started.wait()

    with pytest.raises(ActiveGenerationError):
        await manager.submit_turn(
            requester_id=user.id,
            client_request_id="second",
            text="A second turn.",
            attachment=None,
            thread_id=first.thread_id,
        )

    event_stream = manager.events(first.generation_id)
    initial = await anext(event_stream)
    assert '"status":"running"' in initial
    await cast(AsyncGenerator[str, None], event_stream).aclose()

    await manager.stop(first.generation_id)
    stopped = await _wait_status(manager, first.generation_id, {"stopped"})
    assert stopped["blocks"] == []
    async with database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Message)) == 1
    await manager.shutdown()


async def test_retry_reuses_user_message_without_duplication(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    provider = FailThenSucceedProvider()
    manager = GenerationManager(database, settings, provider)
    original = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="original",
        text="Synthetic source.",
        attachment=None,
        owner_id=user.id,
        mode="translate",
    )
    await _wait_status(manager, original.generation_id, {"failed"})
    retry_id = await manager.retry(
        generation_id=original.generation_id,
        requester_id=user.id,
        client_request_id="retry",
    )
    completed = await _wait_status(manager, retry_id, {"completed"})
    assert completed["blocks"] == [{"type": "deliverable", "text": "Translated text"}]
    async with database.sessions() as db:
        messages = list(
            (
                await db.execute(select(Message).where(Message.thread_id == original.thread_id))
            ).scalars()
        )
        assert [message.role for message in messages] == ["user", "assistant"]
    await manager.shutdown()


async def test_startup_marks_orphaned_generation_failed(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode="translate", voice_key=None)
        db.add(thread)
        await db.flush()
        orphan = Generation(
            thread_id=thread.id,
            requester_id=user.id,
            client_request_id="orphan",
            purpose="chat",
            status="running",
        )
        db.add(orphan)
        await db.commit()
        orphan_id = orphan.id
    manager = GenerationManager(database, settings, BlockingProvider())
    assert await manager.reconcile_orphans() == 1
    snapshot = await manager.get_snapshot(orphan_id)
    assert snapshot is not None
    assert snapshot["status"] == "failed"
    assert snapshot["error_code"] == "restart_interrupted"


async def test_canonical_state_and_pending_cost_reconcile_atomically(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    provider = CanonicalReconcileProvider()
    manager = GenerationManager(database, settings, provider)
    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="canonical",
        text="Hello",
        attachment=None,
        owner_id=user.id,
        mode="translate",
    )
    assert (await _wait_status(manager, submitted.generation_id, {"completed"}))["status"] == (
        "completed"
    )
    await provider.title_done.wait()
    async with database.sessions() as db:
        version = await db.scalar(select(WorkVersion))
        assert version is not None
        assert (version.version_no, version.source_text, version.output_text) == (
            1,
            "Hello",
            "Bonjour",
        )
        assert await db.scalar(select(func.count()).select_from(WorkItem)) == 1
        charges = list(
            (
                await db.execute(
                    select(UsageEvent.amount_microusd).where(UsageEvent.event_type == "charge")
                )
            ).scalars()
        )
        assert sum(value or 0 for value in charges) == 55
    assert await manager.reconcile_pending_costs() == 0
    await manager.shutdown()


async def test_automatic_compaction_preserves_recent_transcript_and_accounts_cost(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, base_settings, user = manager_database
    settings = base_settings.model_copy(
        update={"context_compaction_chars": 1_000, "context_recent_messages": 4}
    )
    async with database.sessions() as db:
        thread = Thread(
            owner_id=user.id,
            mode="translate",
            voice_key=None,
            title="Existing thread",
        )
        db.add(thread)
        await db.flush()
        for ordinal in range(1, 6):
            role = "user" if ordinal % 2 else "assistant"
            db.add(
                Message(
                    thread_id=thread.id,
                    ordinal=ordinal,
                    role=role,
                    actor_user_id=user.id if role == "user" else None,
                    content=[{"type": "conversation", "text": f"Prior turn {ordinal}"}],
                )
            )
        await db.commit()
        thread_id = thread.id

    manager = GenerationManager(database, settings, CompactingProvider())
    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="compact",
        text="A sufficiently long new source request.",
        attachment=None,
        thread_id=thread_id,
    )
    await _wait_status(manager, submitted.generation_id, {"completed"})
    async with database.sessions() as db:
        thread = await db.get(Thread, thread_id)
        assert thread is not None
        assert thread.summary_through_ordinal == 2
        assert thread.context_summary == "Earlier decisions.\n\nUnresolved:\n- Tone"
        total = await db.scalar(
            select(func.sum(UsageEvent.amount_microusd)).where(UsageEvent.event_type == "charge")
        )
        assert total == 33
    await manager.shutdown()


def test_exact_replacements_use_one_immutable_base_and_reject_overlap() -> None:
    assert (
        _apply_base_replacements("abc def", [("abc", "def"), ("def", "X")], label="output")
        == "def X"
    )
    with pytest.raises(ProtocolError, match="overlapping_output"):
        _apply_base_replacements("abcdef", [("abc", "X"), ("bc", "Y")], label="output")
