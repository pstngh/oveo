from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import AsyncGenerator
from typing import cast

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from oveo.attachments import ValidatedAttachment
from oveo.config import Settings
from oveo.db import Database
from oveo.docx import DocxBlock, docx_uncompressed_limit, extract_docx
from oveo.generation import (
    ActiveGenerationError,
    GenerationManager,
    OpenRouterProvider,
    ProviderCompletion,
    ProviderError,
    ProviderRequest,
    _apply_base_replacements,
    _snapshot_messages,
)
from oveo.models import Generation, Message, Thread, UsageEvent, User, WorkItem, WorkVersion
from oveo.protocol import (
    AppendState,
    ContentBlock,
    EstablishState,
    FullState,
    OutputReplacement,
    ProtocolDocument,
    ProtocolError,
    ReplaceState,
    SourceOutputReplacement,
    StateOperation,
)
from oveo.provider import OpenRouterClient
from tests.docx_fixtures import W, make_docx, textbox_document


def _document(state: StateOperation, visible: str) -> ProtocolDocument:
    return ProtocolDocument(
        version=1,
        blocks=(ContentBlock(type="deliverable", text=visible),),
        state=state,
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
    b'"brief":{"direction":"en-US-fr-CA"}}\n'
    b'{"v":1,"event":"response_end"}\n'
)


def _response_stream(
    visible: str | list[str],
    state: dict[str, object],
) -> bytes:
    deliverables = [visible] if isinstance(visible, str) else visible
    lines: list[dict[str, object]] = [{"v": 1, "event": "response_start"}]
    for index, text in enumerate(deliverables, start=1):
        block_id = f"b{index}"
        lines.extend(
            (
                {"v": 1, "event": "block_start", "id": block_id, "type": "deliverable"},
                {"v": 1, "event": "block_delta", "id": block_id, "text": text},
                {"v": 1, "event": "block_end", "id": block_id},
            )
        )
    lines.extend(
        (
            {"v": 1, "event": "state", **state},
            {"v": 1, "event": "response_end"},
        )
    )
    return b"".join(
        (json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        for line in lines
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
        self.requests: list[ProviderRequest] = []

    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del cancel_event
        self.requests.append(request)
        self.calls += 1
        if self.calls == 1:
            error = ProviderError("provider_network")
            # Provider-specific retry state must not revive manager-level retries.
            error.__dict__["transient"] = True
            raise error
        await emit(_SUCCESS)  # type: ignore[operator]
        return ProviderCompletion(provider_request_id="provider-retry", cost_microusd=7)


class ScriptedProvider:
    def __init__(self, responses: list[bytes | ProviderError]) -> None:
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del cancel_event
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("scripted provider exhausted")
        response = self.responses.pop(0)
        if isinstance(response, ProviderError):
            raise response
        for start in range(0, len(response), 17):
            await emit(response[start : start + 17])  # type: ignore[operator]
        return ProviderCompletion(
            provider_request_id=f"request-{len(self.requests)}",
            cost_microusd=len(self.requests),
        )


class MalformedSummaryThenSuccessProvider:
    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []
        self.summary_calls = 0

    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del cancel_event
        self.requests.append(request)
        if request.purpose == "summary":
            self.summary_calls += 1
            payload = (
                b'{"wrong":"shape"}'
                if self.summary_calls == 1
                else b'{"version":1,"summary":"Recovered summary.","unresolved":[]}'
            )
            await emit(payload)  # type: ignore[operator]
            return ProviderCompletion(cost_microusd=self.summary_calls)
        await emit(_SUCCESS)  # type: ignore[operator]
        return ProviderCompletion(cost_microusd=3)


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

    async def always_valid() -> bool:
        return True

    event_stream = manager.open_event_stream(
        first.generation_id, user_id=user.id, session_id="session", session_valid=always_valid
    )
    initial = await anext(event_stream)
    assert initial.startswith("event: snapshot\n")
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
    assert submitted.generation_id not in manager._live
    assert submitted.generation_id not in manager._subscribers
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
    async with database.sessions() as db:
        item = WorkItem(thread_id=original.thread_id, kind="translation", active=True)
        db.add(item)
        await db.flush()
        db.add(
            WorkVersion(
                work_item_id=item.id,
                version_no=1,
                parent_version_id=None,
                operation="establish",
                source_text="Current source.",
                output_text="Current output from after the failed snapshot.",
                source_word_count=2,
                brief={"direction": "en-US-fr-CA"},
            )
        )
        await db.commit()
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
    chat_requests = [request for request in provider.requests if request.purpose == "chat"]
    assert len(chat_requests) == 2
    assert "Current output from after the failed snapshot." not in str(chat_requests[0].snapshot)
    assert "Current output from after the failed snapshot." in str(chat_requests[1].snapshot)
    assert str(chat_requests[1].snapshot).count("Synthetic source.") == 1
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
        thread = Thread(owner_id=user.id, mode=mode)
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
            document=_document(
                EstablishState(source="Source or brief", brief={"mode": mode}),
                "Completed output",
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
        thread = Thread(owner_id=user.id, mode="translate")
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


async def test_provider_stream_decoder_and_commit_cover_all_canonical_operations(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    streams = [
        _response_stream(
            "Bonjour",
            {
                "operation": "establish",
                "source": "Hello",
                "brief": {"direction": "en-US-fr-CA", "tone": "plain"},
            },
        ),
        # The visible deliverable is the output addition; the state does not repeat it.
        _response_stream(
            "Liste:\n- un",
            {
                "operation": "append",
                "base_version": 1,
                "source_addition": "List:\n- one",
                "source_separator": "line",
                "output_separator": "line",
                "brief": {"direction": "en-US-fr-CA", "tone": "concise"},
            },
        ),
        _response_stream(
            "Bonjour\nListe:\n- deux",
            {
                "operation": "replace",
                "base_version": 2,
                "replacements": [
                    {
                        "source_anchor": "one",
                        "source_replacement": "two",
                        "output_anchor": "un",
                        "output_replacement": "deux",
                    }
                ],
                "brief": {"direction": "en-US-fr-CA", "tone": "formal"},
            },
        ),
        _response_stream(
            "Version complète.",
            {
                "operation": "full",
                "base_version": 3,
                "source": "Complete version.",
                "brief": {"direction": "en-US-fr-CA", "tone": "formal"},
            },
        ),
    ]
    provider = ScriptedProvider(streams)
    manager = GenerationManager(database, settings, provider)
    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode="translate", title="Existing conversation")
        db.add(thread)
        await db.commit()
        thread_id = thread.id

    completed_blocks: list[list[dict[str, str]]] = []
    for index in range(4):
        submitted = await manager.submit_turn(
            requester_id=user.id,
            client_request_id=f"operation-{index}",
            text=f"Operation request {index}",
            attachment=None,
            thread_id=thread_id,
        )
        snapshot = await _wait_status(manager, submitted.generation_id, {"completed"})
        completed_blocks.append(cast(list[dict[str, str]], snapshot["blocks"]))

    assert completed_blocks[1] == [{"type": "deliverable", "text": "Liste:\n- un"}]
    async with database.sessions() as db:
        versions = list(
            (await db.execute(select(WorkVersion).order_by(WorkVersion.version_no))).scalars()
        )
        assistant_messages = list(
            (
                await db.execute(
                    select(Message)
                    .where(Message.thread_id == thread_id, Message.role == "assistant")
                    .order_by(Message.ordinal)
                )
            ).scalars()
        )

    assert [version.operation for version in versions] == [
        "establish",
        "append",
        "replace",
        "full",
    ]
    assert versions[1].source_text == "Hello\nList:\n- one"
    assert versions[1].output_text == "Bonjour\nListe:\n- un"
    assert versions[1].brief["tone"] == "concise"
    assert versions[2].source_text == "Hello\nList:\n- two"
    assert versions[2].output_text == "Bonjour\nListe:\n- deux"
    assert versions[2].brief["tone"] == "formal"
    assert versions[3].source_text == "Complete version."
    assert versions[3].output_text == "Version complète."
    assert len(assistant_messages) == 4
    assert len(provider.requests) == 4
    await manager.shutdown()


@pytest.mark.parametrize(
    ("visible", "state"),
    [
        (
            ["Option une", "Option deux"],
            {"operation": "establish", "source": "Source", "brief": {"direction": "en-US-fr-CA"}},
        ),
        # The previous grammar repeated the deliverable as `output`; it is now rejected.
        (
            "Option une",
            {
                "operation": "establish",
                "source": "Source",
                "output": "Option une",
                "brief": {"direction": "en-US-fr-CA"},
            },
        ),
    ],
)
async def test_mutation_rejects_multiple_deliverables_and_repeated_output(
    manager_database: tuple[Database, Settings, User],
    visible: str | list[str],
    state: dict[str, object],
) -> None:
    database, settings, user = manager_database
    invalid_response = _response_stream(visible, state)
    provider = ScriptedProvider([invalid_response] * 3)
    manager = GenerationManager(database, settings, provider)
    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode="translate", title="Existing conversation")
        db.add(thread)
        await db.commit()
        thread_id = thread.id

    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="ambiguous-visible-state",
        text="Give me alternatives.",
        attachment=None,
        thread_id=thread_id,
    )
    failed = await _wait_status(manager, submitted.generation_id, {"failed"})
    assert failed["error_code"] == "protocol_error"
    assert failed["blocks"] == []
    async with database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(WorkVersion)) == 0
        assert await db.scalar(select(func.count()).select_from(Message)) == 1
    assert len(provider.requests) == 3
    await manager.shutdown()


@pytest.mark.parametrize(
    ("failed_state", "error_code"),
    [
        (
            {
                "operation": "append",
                "base_version": 99,
                "source_addition": "More",
                "output_addition": "Suite",
                "source_separator": "paragraph",
                "output_separator": "paragraph",
            },
            "stale_state",
        ),
        (
            {
                "operation": "replace",
                "base_version": 1,
                "replacements": [{"output_anchor": "missing", "output_replacement": "replacement"}],
            },
            "state_persistence_failed",
        ),
    ],
)
async def test_state_application_failures_preserve_valid_visible_blocks(
    manager_database: tuple[Database, Settings, User],
    failed_state: dict[str, object],
    error_code: str,
) -> None:
    database, settings, user = manager_database
    provider = ScriptedProvider(
        [
            _response_stream(
                "Bonjour",
                {
                    "operation": "establish",
                    "source": "Hello",
                    "brief": {"direction": "en-US-fr-CA"},
                },
            ),
            _response_stream("Suite", failed_state),
        ]
    )
    manager = GenerationManager(database, settings, provider)
    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode="translate", title="Existing conversation")
        db.add(thread)
        await db.commit()
        thread_id = thread.id

    first = await manager.submit_turn(
        requester_id=user.id,
        client_request_id=f"{error_code}-base",
        text="Translate Hello.",
        attachment=None,
        thread_id=thread_id,
    )
    await _wait_status(manager, first.generation_id, {"completed"})
    second = await manager.submit_turn(
        requester_id=user.id,
        client_request_id=f"{error_code}-failure",
        text="Apply the update.",
        attachment=None,
        thread_id=thread_id,
    )
    failed = await _wait_status(manager, second.generation_id, {"failed"})
    assert failed["error_code"] == error_code
    assert failed["blocks"] == [{"type": "deliverable", "text": "Suite"}]
    assert "private" not in str(failed["error_message"]).lower()
    async with database.sessions() as db:
        versions = list((await db.execute(select(WorkVersion))).scalars())
        assistants = list(
            (
                await db.execute(
                    select(Message).where(
                        Message.thread_id == thread_id,
                        Message.role == "assistant",
                    )
                )
            ).scalars()
        )
    assert len(versions) == 1
    assert len(assistants) == 1
    await manager.shutdown()


@pytest.mark.parametrize(
    "invalid_response",
    [
        b"not-json\n",
        # Text work cannot establish without its complete source.
        _response_stream(
            "Visible draft that must be discarded",
            {"operation": "establish", "brief": {"direction": "en-US-fr-CA"}},
        ),
    ],
)
async def test_protocol_failure_retries_once_and_discards_any_visible_draft(
    manager_database: tuple[Database, Settings, User],
    invalid_response: bytes,
) -> None:
    database, settings, user = manager_database
    provider = ScriptedProvider([invalid_response, _SUCCESS])
    manager = GenerationManager(database, settings, provider)
    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode="translate", title="Existing conversation")
        db.add(thread)
        await db.commit()
        thread_id = thread.id

    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="protocol-repair",
        text="Translate this.",
        attachment=None,
        thread_id=thread_id,
    )
    completed = await _wait_status(manager, submitted.generation_id, {"completed"})
    assert completed["blocks"] == [{"type": "deliverable", "text": "Translated text"}]
    assert len(provider.requests) == 2
    assert "PROTOCOL RETRY" not in str(provider.requests[0].snapshot)
    assert "PROTOCOL RETRY" in str(provider.requests[1].snapshot)
    async with database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(WorkVersion)) == 0
        assistant_messages = int(
            await db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.thread_id == thread_id, Message.role == "assistant")
            )
            or 0
        )
        assert assistant_messages == 1
    await manager.shutdown()


async def test_internal_comms_recovers_from_mismatch_then_invalid_event_fields(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    missing_source = _response_stream(
        "Visible draft",
        {"operation": "establish", "brief": {"audience": "staff"}},
    )
    invalid_fields = _ESTABLISH.replace(
        b'{"v":1,"event":"response_end"}',
        b'{"v":1,"event":"response_end","extra":true}',
    )
    valid = _response_stream(
        "Final draft",
        {
            "operation": "establish",
            "source": "Synthetic brief",
            "brief": {"audience": "staff"},
        },
    )
    provider = ScriptedProvider([missing_source, invalid_fields, valid])
    manager = GenerationManager(database, settings, provider)
    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode="internal_comms", title="Existing conversation")
        db.add(thread)
        await db.commit()
        thread_id = thread.id

    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="internal-comms-protocol-repair",
        text="Draft a short staff notice from this synthetic brief.",
        attachment=None,
        thread_id=thread_id,
    )
    completed = await _wait_status(manager, submitted.generation_id, {"completed"})
    assert completed["blocks"] == [{"type": "deliverable", "text": "Final draft"}]
    assert len(provider.requests) == 3
    assert len({request.generation_id for request in provider.requests}) == 3
    assert "must include the complete source" in str(provider.requests[1].snapshot)
    assert "missing or extra keys" in str(provider.requests[2].snapshot)
    async with database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(WorkVersion)) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.thread_id == thread_id, Message.role == "assistant")
            )
            == 1
        )
    await manager.shutdown()


async def test_provider_stream_failure_has_specific_safe_classification(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    manager = GenerationManager(
        database,
        settings,
        ScriptedProvider([ProviderError("provider_stream_error")]),
    )
    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="provider-stream-failure",
        text="Translate this.",
        attachment=None,
        owner_id=user.id,
        mode="translate",
    )
    failed = await _wait_status(manager, submitted.generation_id, {"failed"})
    assert failed["error_code"] == "provider_stream_error"
    assert failed["error_message"] == (
        "The model provider returned an incomplete or invalid response stream."
    )
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
        thread = Thread(owner_id=user.id, mode="translate")
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
            document=_document(
                EstablishState(source="Hello world.", brief={"direction": "en-US-fr-CA"}),
                "Bonjour le monde.",
            ),
        )
        await manager._apply_state_operation(
            db,
            generation=generation,
            thread=thread,
            document=_document(
                AppendState(
                    base_version=1,
                    source_addition="A new paragraph.",
                    output_addition="Un nouveau paragraphe.",
                    source_separator="paragraph",
                    output_separator="paragraph",
                    brief={"direction": "en-US-fr-CA", "tone": "plain"},
                ),
                "Un nouveau paragraphe.",
            ),
        )
        await manager._apply_state_operation(
            db,
            generation=generation,
            thread=thread,
            document=_document(
                ReplaceState(
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
                    brief={"direction": "en-US-fr-CA", "tone": "formal"},
                ),
                "Salut le monde.\n\nUn paragraphe révisé.",
            ),
        )
        await manager._apply_state_operation(
            db,
            generation=generation,
            thread=thread,
            document=_document(
                FullState(base_version=3, brief={"direction": "en-US-fr-CA", "tone": "formal"}),
                "Version complète révisée.",
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
        thread = Thread(owner_id=user.id, mode="translate")
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
            document=_document(
                EstablishState(source="one two", brief={"direction": "en-US-fr-CA"}),
                "un deux",
            ),
        )
        with pytest.raises(ProtocolError, match="state_base_mismatch"):
            await manager._apply_state_operation(
                db,
                generation=generation,
                thread=thread,
                document=_document(
                    AppendState(
                        base_version=2,
                        source_addition="three",
                        output_addition="trois",
                        source_separator="space",
                        output_separator="space",
                    ),
                    "trois",
                ),
            )
        with pytest.raises(ProtocolError, match="state_source_word_limit"):
            await manager._apply_state_operation(
                db,
                generation=generation,
                thread=thread,
                document=_document(
                    AppendState(
                        base_version=1,
                        source_addition="three four",
                        output_addition="trois quatre",
                        source_separator="space",
                        output_separator="space",
                    ),
                    "trois quatre",
                ),
            )
        assert await db.scalar(select(func.count()).select_from(WorkVersion)) == 1

    await manager.shutdown()


async def test_automatic_compaction_preserves_recent_transcript_and_accounts_cost(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, base_settings, user = manager_database
    settings = base_settings.model_copy(
        update={"context_compaction_tokens": 10_500, "context_recent_messages": 4}
    )
    async with database.sessions() as db:
        thread = Thread(
            owner_id=user.id,
            mode="translate",
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


async def test_malformed_summary_is_retried_once_without_killing_user_turn(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, base_settings, user = manager_database
    settings = base_settings.model_copy(
        update={"context_compaction_tokens": 10_500, "context_recent_messages": 4}
    )
    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode="translate", title="Existing thread")
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
                    content=[{"type": "conversation", "text": f"Prior turn {ordinal} " * 400}],
                )
            )
        await db.commit()
        thread_id = thread.id

    provider = MalformedSummaryThenSuccessProvider()
    manager = GenerationManager(database, settings, provider)
    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="malformed-summary-retry",
        text="Continue safely.",
        attachment=None,
        thread_id=thread_id,
    )
    completed = await _wait_status(manager, submitted.generation_id, {"completed"})
    assert completed["status"] == "completed"
    assert [request.purpose for request in provider.requests] == ["summary", "summary", "chat"]
    async with database.sessions() as db:
        thread = await db.get(Thread, thread_id)
        assert thread is not None
        assert thread.context_summary == "Recovered summary."
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


async def test_single_oversized_handoff_turn_has_actionable_failure(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, base_settings, user = manager_database
    settings = base_settings.model_copy(update={"context_compaction_tokens": 500})
    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode="translate", title="Existing thread")
        db.add(thread)
        await db.flush()
        db.add(
            Message(
                thread_id=thread.id,
                ordinal=1,
                role="user",
                actor_user_id=user.id,
                content=[{"type": "conversation", "text": "instruction " * 5_000}],
            )
        )
        await db.commit()
        thread_id = thread.id

    provider = HandoffCaptureProvider()
    manager = GenerationManager(database, settings, provider)
    generation_id = await manager.submit_handoff(
        thread_id=thread_id,
        requester_id=user.id,
        client_request_id="oversized-single-handoff-turn",
    )
    failed = await _wait_status(manager, generation_id, {"failed"})
    assert failed["error_code"] == "handoff_turn_too_large"
    assert "Split that turn" in str(failed["error_message"])
    assert provider.request is None
    await manager.shutdown()


async def test_prompt_handoff_rejects_unreplaced_reserved_sentinel(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    sentinel_stream = _response_stream(
        "OVEO_HANDOFF_TEXT_MUST_BE_REPLACED_V1",
        {"operation": "none"},
    )
    provider = ScriptedProvider([sentinel_stream] * 3)
    manager = GenerationManager(database, settings, provider)
    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode="translate", title="Existing thread")
        db.add(thread)
        await db.flush()
        db.add(
            Message(
                thread_id=thread.id,
                ordinal=1,
                role="user",
                actor_user_id=user.id,
                content=[{"type": "conversation", "text": "Keep the term."}],
            )
        )
        await db.commit()
        thread_id = thread.id

    generation_id = await manager.submit_handoff(
        thread_id=thread_id,
        requester_id=user.id,
        client_request_id="handoff-sentinel",
    )
    failed = await _wait_status(manager, generation_id, {"failed"})
    assert failed["error_code"] == "protocol_error"
    assert len(provider.requests) == 3
    await manager.shutdown()


async def test_handoff_provider_request_contains_only_user_authored_material(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    async with database.sessions() as db:
        thread = Thread(
            owner_id=user.id,
            mode="translate",
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


async def test_truncated_response_is_reported_once_without_protocol_retries(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, base_settings, user = manager_database
    settings = base_settings.model_copy(
        update={"openrouter_api_key": SecretStr("sk-or-v1-" + "0" * 64)}
    )
    chat_calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        chat_calls.append(body)
        chunks = [
            {"id": "gen-cut", "choices": [{"delta": {"content": _ESTABLISH.decode()[:40]}}]},
            {
                "id": "gen-cut",
                "choices": [{"delta": {"content": ""}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 32000, "cost": "0.25"},
            },
        ]
        content = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        return httpx.Response(
            200, content=(content + "data: [DONE]\n\n").encode(), headers={"x-request-id": "r"}
        )

    provider = OpenRouterProvider(settings)
    await provider.aclose()
    provider._client = OpenRouterClient(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    manager = GenerationManager(database, settings, provider)
    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="truncated",
        text="Translate this synthetic document.",
        attachment=None,
        owner_id=user.id,
        mode="translate",
    )
    failed = await _wait_status(manager, submitted.generation_id, {"failed"})

    assert failed["error_code"] == "response_too_long"
    assert "output limit" in str(failed["error_message"])
    assert len(chat_calls) == 1  # no provider replay and no protocol retry
    assert chat_calls[0]["max_completion_tokens"] == settings.chat_max_completion_tokens
    assert chat_calls[0]["reasoning_effort"] == "high"
    async with database.sessions() as db:
        charges = list(
            (
                await db.execute(
                    select(UsageEvent.amount_microusd).where(UsageEvent.event_type == "charge")
                )
            ).scalars()
        )
    assert charges == [250_000]  # the synthetic reported cost is kept, not dropped
    await manager.shutdown()


async def test_append_derives_the_addition_and_refuses_ambiguous_whole_documents(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    manager = GenerationManager(database, settings, BlockingProvider())
    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode="translate")
        db.add(thread)
        await db.flush()
        generation = Generation(
            thread_id=thread.id,
            requester_id=user.id,
            client_request_id="append-derivation",
            purpose="chat",
            status="running",
        )
        await manager._apply_state_operation(
            db,
            generation=generation,
            thread=thread,
            document=_document(EstablishState(source="One.", brief={"d": "en-fr"}), "Un."),
        )
        # Whole-document display: the addition must be named explicitly.
        with pytest.raises(ProtocolError, match="state_output_addition_missing"):
            await manager._apply_state_operation(
                db,
                generation=generation,
                thread=thread,
                document=_document(
                    AppendState(
                        base_version=1,
                        source_addition="Two.",
                        source_separator="space",
                        output_separator="space",
                    ),
                    "Un. Deux.",
                ),
            )
        await manager._apply_state_operation(
            db,
            generation=generation,
            thread=thread,
            document=_document(
                AppendState(
                    base_version=1,
                    source_addition="Two.",
                    source_separator="space",
                    output_separator="space",
                    output_addition="Deux.",
                ),
                "Un. Deux.",
            ),
        )
        await db.commit()
        latest = await db.scalar(select(WorkVersion).where(WorkVersion.version_no == 2))
    assert latest is not None
    assert (latest.source_text, latest.output_text) == ("One. Two.", "Un. Deux.")
    await manager.shutdown()


async def test_outdated_stored_docx_map_fails_closed_and_keeps_the_response(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    package = make_docx(document_xml=textbox_document(anchor_first=True))
    # The block map the earlier extractor stored for this package (text-box text folded
    # into the anchoring paragraph and repeated as extra blocks).
    legacy_texts = [
        "Intro paragraph.",
        "Callout textCallout textQuarterly results improved.",
        "Callout text",
        "Callout text",
    ]
    attachment = ValidatedAttachment(
        original_name="legacy.docx",
        content=package,
        byte_count=len(package),
        word_count=8,
        sha256=hashlib.sha256(package).hexdigest(),
        document_blocks=tuple(
            DocxBlock(id=f"p{index:06d}", kind="paragraph", text=text)
            for index, text in enumerate(legacy_texts, start=1)
        ),
    )
    visible = "\n\n".join(text.upper() for text in legacy_texts)
    provider = ScriptedProvider(
        [
            _response_stream(
                visible,
                {
                    "operation": "establish",
                    "brief": {"direction": "en-fr"},
                    "docx_blocks": [
                        {"id": f"p{index:06d}", "text": text.upper()}
                        for index, text in enumerate(legacy_texts, start=1)
                    ],
                },
            )
        ]
    )
    manager = GenerationManager(database, settings, provider)
    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="legacy-map",
        text="Translate the attached document.",
        attachment=attachment,
        owner_id=user.id,
        mode="translate",
    )
    failed = await _wait_status(manager, submitted.generation_id, {"failed"})

    assert failed["error_code"] == "docx_template_outdated"
    assert "Upload the document again" in str(failed["error_message"])
    assert failed["blocks"] == [{"type": "deliverable", "text": visible}]
    assert len(provider.requests) == 1
    async with database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(WorkVersion)) == 0
    await manager.shutdown()


async def test_docx_template_is_parsed_before_the_commit_takes_the_write_lock(
    manager_database: tuple[Database, Settings, User],
) -> None:
    # L-6: parsing a template can take a while; other writers must not wait for it.
    database, settings, user = manager_database
    package = make_docx(
        document_xml=(
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document xmlns:w="{W}"><w:body>'
            "<w:p><w:r><w:t>First paragraph.</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Second paragraph.</w:t></w:r></w:p>"
            "<w:sectPr/></w:body></w:document>"
        )
    )
    blocks = extract_docx(
        package, max_uncompressed_bytes=docx_uncompressed_limit(settings.max_upload_bytes)
    ).blocks
    attachment = ValidatedAttachment(
        original_name="plain.docx",
        content=package,
        byte_count=len(package),
        word_count=4,
        sha256=hashlib.sha256(package).hexdigest(),
        document_blocks=blocks,
    )
    translated = [block.text.upper() for block in blocks]
    provider = ScriptedProvider(
        [
            _response_stream(
                "\n\n".join(translated),
                {
                    "operation": "establish",
                    "brief": {"direction": "en-fr"},
                    "docx_blocks": [
                        {"id": block.id, "text": text}
                        for block, text in zip(blocks, translated, strict=True)
                    ],
                },
            )
        ]
    )
    manager = GenerationManager(database, settings, provider)
    database_path = settings.database_url.split(":///", 1)[1]
    observed: list[str] = []
    original = manager._load_docx_template

    def load_while_checking_the_lock(template: object) -> object:
        other = sqlite3.connect(database_path, timeout=0.2)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
            observed.append("write lock free")
        except sqlite3.OperationalError:
            observed.append("write lock held")
        finally:
            other.close()
        return original(template)  # type: ignore[arg-type]

    manager._load_docx_template = load_while_checking_the_lock  # type: ignore[assignment,method-assign]
    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="plain-docx",
        text="Translate the attached document.",
        attachment=attachment,
        owner_id=user.id,
        mode="translate",
    )
    final = await _wait_status(manager, submitted.generation_id, {"completed", "failed"})

    assert final["status"] == "completed", final
    assert observed == ["write lock free"]
    async with database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(WorkVersion)) == 1
    await manager.shutdown()
