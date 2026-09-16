from __future__ import annotations

import asyncio
import logging
import re
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
    GenerationError,
    GenerationManager,
    ProviderCompletion,
    ProviderError,
    ProviderRequest,
    _apply_base_replacements,
    _snapshot_messages,
)
from oveo.models import Base, Generation, Message, Thread, UsageEvent, User, WorkItem, WorkVersion
from oveo.protocol import (
    AppendState,
    EstablishState,
    FullState,
    OutputReplacement,
    ProtocolError,
    ReplaceState,
    SourceOutputReplacement,
)

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


class DelayedCancellationProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cleanup_done = asyncio.Event()

    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del request, emit, cancel_event
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.03)
            self.cleanup_done.set()
            raise


class UnexpectedFailureProvider:
    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del request, emit, cancel_event
        raise RuntimeError("private prompt marker")


class MetadataProvider:
    def __init__(self, *, delay: float = 0) -> None:
        self.calls: list[str] = []
        self.delay = delay

    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del request, emit, cancel_event
        raise AssertionError("generation is not used by this test")

    async def reconcile_cost(self, provider_generation_id: str) -> int:
        self.calls.append(provider_generation_id)
        if self.delay:
            await asyncio.sleep(self.delay)
        return 1


class FailThenSucceedProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del request, cancel_event
        self.calls += 1
        if self.calls == 1:
            error = ProviderError("provider_network")
            # A provider adapter adding legacy retry state must not revive manager retries.
            error.__dict__["transient"] = True
            raise error
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
    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del cancel_event
        self.requests.append(request)
        if request.purpose == "summary":
            await emit(  # type: ignore[operator]
                b'{"version":1,"summary":"Earlier decisions.","unresolved":["Tone"]}'
            )
            return ProviderCompletion(cost_microusd=11)
        await emit(_SUCCESS)  # type: ignore[operator]
        return ProviderCompletion(cost_microusd=22)


class HandoffCompactingProvider:
    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del cancel_event
        self.requests.append(request)
        if ":prompt_handoff_compaction:" in request.generation_id:
            await emit(  # type: ignore[operator]
                b'{"v":1,"event":"response_start"}\n'
                b'{"v":1,"event":"block_start","id":"b1","type":"deliverable"}\n'
                b'{"v":1,"event":"block_delta","id":"b1","text":"Extracted user instruction."}\n'
                b'{"v":1,"event":"block_end","id":"b1"}\n'
                b'{"v":1,"event":"state","operation":"none"}\n'
                b'{"v":1,"event":"response_end"}\n'
            )
            return ProviderCompletion(cost_microusd=1)
        await emit(_SUCCESS)  # type: ignore[operator]
        return ProviderCompletion(cost_microusd=2)


class HandoffCaptureProvider:
    def __init__(self) -> None:
        self.request: ProviderRequest | None = None

    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del cancel_event
        self.request = request
        await emit(_SUCCESS)  # type: ignore[operator]
        return ProviderCompletion(cost_microusd=0)


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


async def test_thread_cancellation_awaits_task_cleanup_before_returning(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    provider = DelayedCancellationProvider()
    manager = GenerationManager(database, settings, provider)
    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="cancel-thread",
        text="Synthetic source.",
        attachment=None,
        owner_id=user.id,
        mode="translate",
    )
    await provider.started.wait()

    await manager.cancel_thread(submitted.thread_id)

    assert provider.cleanup_done.is_set()
    stopped = await manager.get_snapshot(submitted.generation_id)
    assert stopped is not None and stopped["status"] == "stopped"
    assert submitted.generation_id not in manager._tasks
    assert submitted.generation_id not in manager._conditions
    await manager.shutdown()


async def test_retry_reuses_user_message_without_duplication(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    provider = FailThenSucceedProvider()
    manager = GenerationManager(
        database,
        settings.model_copy(update={"provider_retry_attempts": 3}),
        provider,
    )
    original = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="original",
        text="Synthetic source.",
        attachment=None,
        owner_id=user.id,
        mode="translate",
    )
    await _wait_status(manager, original.generation_id, {"failed"})
    assert provider.calls == 1
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


async def test_unexpected_generation_failure_logs_only_safe_diagnostics(
    manager_database: tuple[Database, Settings, User], caplog: pytest.LogCaptureFixture
) -> None:
    database, settings, user = manager_database
    manager = GenerationManager(database, settings, UnexpectedFailureProvider())
    caplog.set_level(logging.ERROR, logger="oveo.background")
    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="unexpected",
        text="Synthetic source.",
        attachment=None,
        owner_id=user.id,
        mode="translate",
    )

    await _wait_status(manager, submitted.generation_id, {"failed"})

    diagnostics = "\n".join(record.getMessage() for record in caplog.records)
    assert "area=generation" in diagnostics
    assert "exception_class=RuntimeError" in diagnostics
    assert "locations=" in diagnostics
    assert re.search(r"error_id=[0-9a-f]{32}", diagnostics)
    assert "private prompt marker" not in diagnostics
    await manager.shutdown()


async def test_legacy_mode_alias_and_voice_are_compatibility_only(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    provider = BlockingProvider()
    manager = GenerationManager(database, settings, provider)
    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="legacy-mode",
        text="Draft an internal note from these facts.",
        attachment=None,
        owner_id=user.id,
        mode="alithyagpt",
        voice_key="comm_internes",
    )
    await provider.started.wait()

    async with database.sessions() as db:
        thread = await db.get(Thread, submitted.thread_id)
        generation = await db.get(Generation, submitted.generation_id)
        assert thread is not None and generation is not None
        assert (thread.mode, thread.voice_key) == ("internal_comms", "comm_internes")
        assert generation.request_snapshot["mode"] == "internal_comms"
        assert "voice_key" not in generation.request_snapshot
        system = generation.request_snapshot["provider_messages"][0]["content"]
        assert "# Oveo Internal communications mode" in system
        assert "voice_key=" not in system

    with pytest.raises(GenerationError, match="does not use a writing voice"):
        await manager.submit_turn(
            requester_id=user.id,
            client_request_id="revision-with-voice",
            text="Revise this.",
            attachment=None,
            owner_id=user.id,
            mode="revision",
            voice_key="comm_internes",
        )

    await manager.stop(submitted.generation_id)
    await _wait_status(manager, submitted.generation_id, {"stopped"})
    await manager.shutdown()


@pytest.mark.parametrize(
    ("mode", "expected_kind"),
    (("translate", "translation"), ("revision", "revision"), ("internal_comms", "draft")),
)
async def test_canonical_work_kind_follows_mode(
    manager_database: tuple[Database, Settings, User],
    mode: str,
    expected_kind: str,
) -> None:
    database, settings, user = manager_database
    manager = GenerationManager(database, settings, BlockingProvider())
    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode=mode, voice_key=None)
        db.add(thread)
        await db.flush()
        generation = Generation(
            thread_id=thread.id,
            requester_id=user.id,
            client_request_id=f"kind-{mode}",
            purpose="chat",
            status="running",
        )
        await manager._apply_state_operation(
            db,
            generation=generation,
            thread=thread,
            state=EstablishState(
                source="Source or brief",
                output="Completed output",
                brief={"mode": mode},
            ),
        )
        await db.commit()
        item = await db.scalar(select(WorkItem).where(WorkItem.thread_id == thread.id))
        assert item is not None
        assert item.kind == expected_kind
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
    await manager.reconcile_pending_costs()
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


async def test_pending_cost_reconciliation_uses_bounded_batches(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    async with database.sessions() as db:
        db.add_all(
            [
                UsageEvent(
                    provider="openrouter",
                    dedupe_key=f"pending-{index}",
                    event_type="pending",
                    purpose="chat",
                    amount_microusd=None,
                    requester_id=user.id,
                    provider_generation_id=f"provider-generation-{index}",
                )
                for index in range(10)
            ]
        )
        await db.commit()
    provider = MetadataProvider()
    manager = GenerationManager(database, settings, provider)

    assert await manager.reconcile_pending_costs() == 8
    assert len(provider.calls) == 8
    assert await manager.reconcile_pending_costs() == 2
    assert len(provider.calls) == 10
    assert await manager.reconcile_pending_costs() == 0
    await manager.shutdown()


async def test_pending_cost_metadata_call_has_a_short_timeout(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, base_settings, user = manager_database
    async with database.sessions() as db:
        db.add(
            UsageEvent(
                provider="openrouter",
                dedupe_key="slow-pending",
                event_type="pending",
                purpose="chat",
                amount_microusd=None,
                requester_id=user.id,
                provider_generation_id="slow-generation",
            )
        )
        await db.commit()
    provider = MetadataProvider(delay=60)
    settings = base_settings.model_copy(update={"provider_metadata_timeout_seconds": 0.1})
    manager = GenerationManager(database, settings, provider)
    started = asyncio.get_running_loop().time()

    assert await manager.reconcile_pending_costs() == 0
    assert asyncio.get_running_loop().time() - started < 0.5
    assert provider.calls == ["slow-generation"]
    await manager.shutdown()


async def test_persisted_canonical_state_supports_every_mutation(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    manager = GenerationManager(database, settings, BlockingProvider())
    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode="translate", voice_key=None)
        db.add(thread)
        await db.flush()
        generation = Generation(
            thread_id=thread.id,
            requester_id=user.id,
            client_request_id="canonical-mutations",
            purpose="chat",
            status="running",
        )

        await manager._apply_state_operation(
            db,
            generation=generation,
            thread=thread,
            state=EstablishState(
                source="Hello world.",
                output="Bonjour le monde.",
                brief={"direction": "en-US-fr-CA"},
            ),
        )
        await manager._apply_state_operation(
            db,
            generation=generation,
            thread=thread,
            state=AppendState(
                base_version=1,
                source_addition="A new paragraph.",
                output_addition="Un nouveau paragraphe.",
            ),
        )
        await manager._apply_state_operation(
            db,
            generation=generation,
            thread=thread,
            state=ReplaceState(
                base_version=2,
                replacements=(
                    SourceOutputReplacement(
                        source_anchor="new paragraph",
                        source_replacement="revised paragraph",
                        output_anchor="nouveau paragraphe",
                        output_replacement="paragraphe révisé",
                    ),
                    OutputReplacement(
                        output_anchor="Bonjour",
                        output_replacement="Salut",
                    ),
                ),
            ),
        )
        await manager._apply_state_operation(
            db,
            generation=generation,
            thread=thread,
            state=FullState(
                base_version=3,
                output="Version complète révisée.",
                source=None,
                brief={"direction": "en-US-fr-CA", "tone": "formal"},
            ),
        )
        await db.commit()
        versions = list(
            (await db.execute(select(WorkVersion).order_by(WorkVersion.version_no))).scalars()
        )

    assert [version.operation for version in versions] == [
        "establish",
        "append",
        "replace",
        "full",
    ]
    assert [version.parent_version_id for version in versions] == [
        None,
        versions[0].id,
        versions[1].id,
        versions[2].id,
    ]
    assert versions[2].source_text == "Hello world.\n\nA revised paragraph."
    assert versions[2].output_text == "Salut le monde.\n\nUn paragraphe révisé."
    assert versions[3].source_text == versions[2].source_text
    assert versions[3].output_text == "Version complète révisée."
    assert versions[3].source_word_count == 5
    assert versions[3].brief == {"direction": "en-US-fr-CA", "tone": "formal"}
    await manager.shutdown()


async def test_persisted_canonical_state_rejects_stale_and_oversized_mutations(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, base_settings, user = manager_database
    settings = base_settings.model_copy(update={"max_source_words": 3})
    manager = GenerationManager(database, settings, BlockingProvider())
    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode="translate", voice_key=None)
        db.add(thread)
        await db.flush()
        generation = Generation(
            thread_id=thread.id,
            requester_id=user.id,
            client_request_id="canonical-invalid-mutations",
            purpose="chat",
            status="running",
        )
        await manager._apply_state_operation(
            db,
            generation=generation,
            thread=thread,
            state=EstablishState(
                source="one two",
                output="un deux",
                brief={"direction": "en-US-fr-CA"},
            ),
        )
        with pytest.raises(ProtocolError, match="state_base_mismatch"):
            await manager._apply_state_operation(
                db,
                generation=generation,
                thread=thread,
                state=AppendState(
                    base_version=2,
                    source_addition="three",
                    output_addition="trois",
                ),
            )
        with pytest.raises(ProtocolError, match="state_source_word_limit"):
            await manager._apply_state_operation(
                db,
                generation=generation,
                thread=thread,
                state=AppendState(
                    base_version=1,
                    source_addition="three four",
                    output_addition="trois quatre",
                ),
            )
        assert await db.scalar(select(func.count()).select_from(WorkVersion)) == 1

    await manager.shutdown()


async def test_automatic_compaction_preserves_recent_transcript_and_accounts_cost(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, base_settings, user = manager_database
    settings = base_settings.model_copy(
        update={"context_compaction_tokens": 10_000, "context_recent_messages": 4}
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
                    content=[
                        {
                            "type": "conversation",
                            "text": f"Prior turn {ordinal} " * 400,
                        }
                    ],
                )
            )
        await db.commit()
        thread_id = thread.id

    provider = CompactingProvider()
    manager = GenerationManager(database, settings, provider)
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
    assert [request.purpose for request in provider.requests] == ["summary", "chat"]
    await manager.shutdown()


async def test_large_handoff_is_compacted_from_user_material_only(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, base_settings, user = manager_database
    settings = base_settings.model_copy(update={"context_compaction_tokens": 2_000})
    async with database.sessions() as db:
        thread = Thread(
            owner_id=user.id,
            mode="translate",
            voice_key=None,
            title="Existing thread",
            context_summary="Private internal summary.",
            summary_through_ordinal=2,
        )
        db.add(thread)
        await db.flush()
        db.add_all(
            [
                Message(
                    thread_id=thread.id,
                    ordinal=1,
                    role="user",
                    actor_user_id=user.id,
                    content=[
                        {
                            "type": "conversation",
                            "text": "First explicit instruction " + "word " * 1_000,
                        }
                    ],
                ),
                Message(
                    thread_id=thread.id,
                    ordinal=2,
                    role="assistant",
                    actor_user_id=None,
                    content=[{"type": "conversation", "text": "Private assistant reply."}],
                ),
                Message(
                    thread_id=thread.id,
                    ordinal=3,
                    role="user",
                    actor_user_id=user.id,
                    content=[
                        {
                            "type": "conversation",
                            "text": "Second explicit instruction " + "word " * 1_000,
                        }
                    ],
                ),
            ]
        )
        await db.commit()
        thread_id = thread.id

    provider = HandoffCompactingProvider()
    manager = GenerationManager(database, settings, provider)
    generation_id = await manager.submit_handoff(
        thread_id=thread_id,
        requester_id=user.id,
        client_request_id="large-handoff-user-only",
    )
    assert (await _wait_status(manager, generation_id, {"completed"}))["status"] == "completed"
    assert len(provider.requests) == 3
    serialized_requests = [
        str(request.snapshot["provider_messages"]) for request in provider.requests
    ]
    assert "First explicit instruction" in serialized_requests[0]
    assert "Second explicit instruction" in serialized_requests[1]
    assert "Extracted user instruction." in serialized_requests[2]
    assert all("Private assistant reply." not in item for item in serialized_requests)
    assert all("Private internal summary." not in item for item in serialized_requests)
    assert all("VERSION-CONTROLLED MODE PROMPT" not in item for item in serialized_requests)
    async with database.sessions() as db:
        total = await db.scalar(
            select(func.sum(UsageEvent.amount_microusd)).where(UsageEvent.event_type == "charge")
        )
        assert total == 4
    await manager.shutdown()


async def test_handoff_provider_request_contains_only_user_authored_material(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    async with database.sessions() as db:
        thread = Thread(
            owner_id=user.id,
            mode="translate",
            voice_key=None,
            title="Existing thread",
            context_summary="Private internal summary.",
            summary_through_ordinal=2,
        )
        db.add(thread)
        await db.flush()
        db.add_all(
            [
                Message(
                    thread_id=thread.id,
                    ordinal=1,
                    role="user",
                    actor_user_id=user.id,
                    content=[{"type": "conversation", "text": "Always keep the product name."}],
                ),
                Message(
                    thread_id=thread.id,
                    ordinal=2,
                    role="assistant",
                    actor_user_id=None,
                    content=[{"type": "conversation", "text": "Private assistant reply."}],
                ),
            ]
        )
        await db.commit()
        thread_id = thread.id

    provider = HandoffCaptureProvider()
    manager = GenerationManager(database, settings, provider)
    generation_id = await manager.submit_handoff(
        thread_id=thread_id,
        requester_id=user.id,
        client_request_id="handoff-user-only",
    )
    assert (await _wait_status(manager, generation_id, {"completed"}))["status"] == "completed"
    assert provider.request is not None
    provider_messages = provider.request.snapshot["provider_messages"]
    serialized = str(provider_messages)
    assert "Always keep the product name." in serialized
    assert "Private assistant reply." not in serialized
    assert "Private internal summary." not in serialized
    assert "# Oveo Translate" not in serialized
    assert "VERSION-CONTROLLED MODE PROMPT" not in serialized
    assert "VERSION-CONTROLLED RESPONSE PROTOCOL" not in serialized
    await manager.shutdown()


def test_exact_replacements_use_one_immutable_base_and_reject_overlap() -> None:
    assert (
        _apply_base_replacements("abc def", [("abc", "def"), ("def", "X")], label="output")
        == "def X"
    )
    with pytest.raises(ProtocolError, match="overlapping_output"):
        _apply_base_replacements("abcdef", [("abc", "X"), ("bc", "Y")], label="output")
    with pytest.raises(ProtocolError, match="missing_output"):
        _apply_base_replacements("abcdef", [("missing", "X")], label="output")
    with pytest.raises(ProtocolError, match="ambiguous_output"):
        _apply_base_replacements("same and same", [("same", "X")], label="output")


def test_snapshot_message_validation_preserves_call_site_error_codes() -> None:
    with pytest.raises(ProviderError) as compaction_error:
        _snapshot_messages({})
    assert compaction_error.value.code == "context_compaction_failed"

    with pytest.raises(ProviderError) as request_error:
        _snapshot_messages({}, error_code="invalid_request_snapshot")
    assert request_error.value.code == "invalid_request_snapshot"
