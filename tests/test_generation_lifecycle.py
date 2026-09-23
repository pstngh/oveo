"""Lifecycle races and recovery: Stop versus completion, orphans, retries, usage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.exc import OperationalError

import oveo.generation as generation_module
from oveo.attachments import ValidatedAttachment, persist_attachment
from oveo.config import Settings
from oveo.db import Database
from oveo.docx import DocxBlock
from oveo.generation import (
    ActiveGenerationError,
    GenerationError,
    GenerationManager,
    OpenRouterProvider,
    ProviderCompletion,
    ProviderError,
    ProviderRequest,
)
from oveo.main import create_app
from oveo.models import Attachment, Generation, Message, Thread, UsageEvent, User
from oveo.protocol import ContentBlock, NoState, ProtocolDocument
from oveo.provider import OpenRouterClient
from tests.test_generation_manager import _SUCCESS, _wait_status

Emit = Callable[[bytes], Awaitable[None]]
ChatHook = Callable[[ProviderRequest, Emit, asyncio.Event], Awaitable[None]]


class HookProvider:
    """Synthetic provider: titles answer at once, chat runs an optional hook."""

    def __init__(self, chat: ChatHook | None = None) -> None:
        self.requests: list[ProviderRequest] = []
        self.chat = chat
        self.in_flight = 0
        self.max_in_flight = 0

    async def generate(
        self, request: ProviderRequest, emit: Emit, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if request.purpose == "title":
                await emit(b"Synthetic Probe Title")
                return ProviderCompletion(provider_request_id=f"title-{len(self.requests)}")
            if self.chat is not None:
                await self.chat(request, emit, cancel_event)
            else:
                await emit(_SUCCESS)
            return ProviderCompletion(
                provider_request_id=f"chat-{len(self.requests)}", cost_microusd=10
            )
        finally:
            self.in_flight -= 1


async def _submit(
    manager: GenerationManager,
    user: User,
    key: str,
    *,
    thread_id: str | None = None,
    text: str = "Translate this synthetic sentence.",
) -> generation_module.Submission:
    if thread_id is None:
        return await manager.submit_turn(
            requester_id=user.id,
            client_request_id=key,
            text=text,
            attachment=None,
            owner_id=user.id,
            mode="translate",
        )
    return await manager.submit_turn(
        requester_id=user.id,
        client_request_id=key,
        text=text,
        attachment=None,
        thread_id=thread_id,
    )


async def _assistant_count(database: Database, thread_id: str) -> int:
    async with database.sessions() as db:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.thread_id == thread_id, Message.role == "assistant")
            )
            or 0
        )


async def _orphan(database: Database, user: User, status: str, **values: Any) -> tuple[str, str]:
    """A generation row left active with no task, as after a failed terminal write."""

    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode="translate", title="Existing")
        db.add(thread)
        await db.flush()
        message = Message(
            thread_id=thread.id,
            ordinal=1,
            role="user",
            actor_user_id=user.id,
            content=[{"type": "conversation", "text": "Earlier turn."}],
        )
        db.add(message)
        await db.flush()
        generation = Generation(
            thread_id=thread.id,
            requester_id=user.id,
            source_message_id=message.id,
            client_request_id=f"orphan-{status}",
            purpose="chat",
            status=status,
            request_snapshot={"schema_version": 1, "provider_messages": []},
            **values,
        )
        db.add(generation)
        await db.commit()
        return thread.id, generation.id


# --- H-1: Stop racing completion ------------------------------------------------


async def test_stop_during_the_final_commit_leaves_one_completed_answer(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    manager = GenerationManager(database, settings, HookProvider())
    first = await _submit(manager, user, "seed")
    await _wait_status(manager, first.generation_id, {"completed"})
    await asyncio.sleep(0.05)  # let the first-exchange title finish

    entered = asyncio.Event()
    release = asyncio.Event()
    original = manager._apply_state_operation

    async def gated(db: Any, **kwargs: Any) -> None:
        entered.set()
        await release.wait()
        await original(db, **kwargs)

    manager._apply_state_operation = gated  # type: ignore[method-assign]
    second = await _submit(manager, user, "raced", thread_id=first.thread_id)
    await entered.wait()
    # The completion already holds the write lock; this Stop waits and then finds the
    # generation completed (it previously overwrote `completed` with `stopping`).
    stop = asyncio.create_task(manager.stop(second.generation_id))
    await asyncio.sleep(0.2)
    release.set()
    await stop
    manager._apply_state_operation = original  # type: ignore[method-assign]

    final = await _wait_status(manager, second.generation_id, {"completed", "stopped", "failed"})
    assert final["status"] == "completed"
    assert await _assistant_count(database, first.thread_id) == 2
    await manager.stop(second.generation_id)  # a late Stop changes nothing
    snapshot = await manager.get_snapshot(second.generation_id)
    assert snapshot is not None and snapshot["status"] == "completed"
    third = await _submit(manager, user, "after", thread_id=first.thread_id)
    await _wait_status(manager, third.generation_id, {"completed"})
    await manager.shutdown()

    restarted = GenerationManager(database, settings, HookProvider())
    assert await restarted.reconcile_orphans() == 0
    with pytest.raises(GenerationError, match="cannot be retried"):
        await restarted.retry(
            generation_id=second.generation_id, requester_id=user.id, client_request_id="dup"
        )
    assert await _assistant_count(database, first.thread_id) == 3
    await restarted.shutdown()


async def test_a_stop_that_wins_first_means_the_answer_is_never_committed(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    thread_id, generation_id = await _orphan(database, user, "stopping")
    manager = GenerationManager(database, settings, HookProvider())
    document = ProtocolDocument(
        version=1, blocks=(ContentBlock(type="deliverable", text="Late answer"),), state=NoState()
    )
    await manager._commit_success(generation_id, document, ProviderCompletion())
    snapshot = await manager.get_snapshot(generation_id)
    assert snapshot is not None and snapshot["status"] == "stopped"
    assert await _assistant_count(database, thread_id) == 0
    await manager.shutdown()


async def test_stop_at_natural_completion_timing_never_wedges_or_duplicates(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    rng = random.Random(1)  # noqa: S311 - deterministic synthetic timing, not security
    holder: dict[str, Any] = {}

    async def chat(request: ProviderRequest, emit: Emit, cancel_event: asyncio.Event) -> None:
        del request, cancel_event
        await emit(_SUCCESS)
        if "id" not in holder:
            return
        generation_id = holder["id"]
        delay = holder["delay"]

        async def late_stop() -> None:
            await asyncio.sleep(delay)
            await holder["manager"].stop(generation_id)

        holder["stop"] = asyncio.create_task(late_stop())

    manager = GenerationManager(database, settings, HookProvider(chat))
    holder["manager"] = manager
    seed = await _submit(manager, user, "seed")
    await _wait_status(manager, seed.generation_id, {"completed"})
    await asyncio.sleep(0.05)
    outcomes: list[str] = []
    for trial in range(40):
        holder.pop("stop", None)
        holder["delay"] = rng.uniform(0.0, 0.012)
        holder["id"] = "pending"
        submitted = await _submit(manager, user, f"trial-{trial}", thread_id=seed.thread_id)
        holder["id"] = submitted.generation_id
        # The hook read the placeholder id, so re-arm with the real one for this trial.
        for _ in range(200):
            if "stop" in holder:
                break
            await asyncio.sleep(0.005)
        stop_task = holder.pop("stop", None)
        if stop_task is not None:
            await stop_task
        await manager.stop(submitted.generation_id)
        final = await _wait_status(manager, submitted.generation_id, {"completed", "stopped"})
        async with database.sessions() as db:
            row = await db.get(Generation, submitted.generation_id)
            assert row is not None
            assert row.status in {"completed", "stopped"}  # never left `stopping`
            assert (row.status == "completed") == (row.result_message_id is not None)
            assert row.request_snapshot == {}
        outcomes.append(str(final["status"]))
        answers = await _assistant_count(database, seed.thread_id)
        assert answers == 1 + outcomes.count("completed")
    await manager.shutdown()


# --- M-10: orphans and failing terminal writes ------------------------------------


async def test_stop_finishes_an_orphaned_generation_immediately(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    _, generation_id = await _orphan(database, user, "running")
    manager = GenerationManager(database, settings, HookProvider())
    await manager.stop(generation_id)
    snapshot = await manager.get_snapshot(generation_id)
    assert snapshot is not None and snapshot["status"] == "stopped"
    await manager.shutdown()


async def test_a_new_turn_finishes_an_orphan_instead_of_being_rejected_forever(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    thread_id, orphan_id = await _orphan(database, user, "running")
    manager = GenerationManager(database, settings, HookProvider())
    submitted = await _submit(manager, user, "next", thread_id=thread_id)
    await _wait_status(manager, submitted.generation_id, {"completed"})
    orphan = await manager.get_snapshot(orphan_id)
    assert orphan is not None
    assert (orphan["status"], orphan["error_code"]) == ("failed", "generation_interrupted")
    await manager.shutdown()


async def test_terminal_failure_write_is_retried_through_a_locked_database(
    manager_database: tuple[Database, Settings, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings, user = manager_database
    monkeypatch.setattr(generation_module, "_WRITE_RETRY_DELAYS", (0.01,) * 6)

    async def failing_chat(request: ProviderRequest, emit: Emit, cancel: asyncio.Event) -> None:
        del request, emit, cancel
        raise ProviderError("provider_network")

    manager = GenerationManager(database, settings, HookProvider(failing_chat))
    original = manager._transition
    blocked = {"remaining": 3}

    async def locked_then_free(db: Any, generation_id: str, **kwargs: Any) -> bool:
        if kwargs.get("status") == "failed" and blocked["remaining"]:
            blocked["remaining"] -= 1
            raise OperationalError("UPDATE generations", {}, Exception("database is locked"))
        return await original(db, generation_id, **kwargs)

    monkeypatch.setattr(manager, "_transition", locked_then_free)
    submitted = await _submit(manager, user, "locked")
    failed = await _wait_status(manager, submitted.generation_id, {"failed"})
    assert failed["error_code"] == "provider_network"
    assert blocked["remaining"] == 0
    await manager.shutdown()


async def test_exhausted_terminal_writes_leave_an_orphan_the_next_turn_repairs(
    manager_database: tuple[Database, Settings, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings, user = manager_database
    monkeypatch.setattr(generation_module, "_WRITE_RETRY_DELAYS", (0.001,) * 2)

    async def failing_chat(request: ProviderRequest, emit: Emit, cancel: asyncio.Event) -> None:
        del request, emit, cancel
        raise ProviderError("provider_network")

    manager = GenerationManager(database, settings, HookProvider(failing_chat))
    original = manager._transition

    async def always_locked(db: Any, generation_id: str, **kwargs: Any) -> bool:
        if kwargs.get("status") == "failed":
            raise OperationalError("UPDATE generations", {}, Exception("database is locked"))
        return await original(db, generation_id, **kwargs)

    monkeypatch.setattr(manager, "_transition", always_locked)
    first = await _submit(manager, user, "wedge")
    task = manager._tasks.get(first.generation_id)
    if task is not None:
        await asyncio.wait([task], timeout=5)
    stuck = await manager.get_snapshot(first.generation_id)
    assert stuck is not None and stuck["status"] == "running"

    monkeypatch.setattr(manager, "_transition", original)
    manager.provider = HookProvider()
    second = await _submit(manager, user, "repair", thread_id=first.thread_id)
    await _wait_status(manager, second.generation_id, {"completed"})
    repaired = await manager.get_snapshot(first.generation_id)
    assert repaired is not None and repaired["error_code"] == "generation_interrupted"
    await manager.shutdown()


# --- Conservative repair of rows written before the fix ----------------------------


async def test_startup_completes_rows_whose_answer_was_committed(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode="translate", title="Legacy")
        db.add(thread)
        await db.flush()
        rows: dict[str, str] = {}
        for index, status in enumerate(("stopping", "failed", "stopped")):
            question = Message(
                thread_id=thread.id,
                ordinal=2 * index + 1,
                role="user",
                actor_user_id=user.id,
                content=[{"type": "conversation", "text": f"Question {index}"}],
            )
            answer = Message(
                thread_id=thread.id,
                ordinal=2 * index + 2,
                role="assistant",
                actor_user_id=None,
                content=[{"type": "conversation", "text": f"Answer {index}"}],
            )
            db.add_all([question, answer])
            await db.flush()
            generation = Generation(
                thread_id=thread.id,
                requester_id=user.id,
                source_message_id=question.id,
                result_message_id=answer.id,
                client_request_id=f"legacy-{status}",
                purpose="chat",
                status=status,
                error_code="restart_interrupted" if status == "failed" else None,
                request_snapshot={"schema_version": 1, "provider_messages": []},
            )
            db.add(generation)
            await db.flush()
            rows[status] = generation.id
        await db.commit()
    _, open_stop = await _orphan(database, user, "stopping")
    _, open_run = await _orphan(database, user, "running")

    manager = GenerationManager(database, settings, HookProvider())
    assert await manager.reconcile_orphans() == 5
    for generation_id in rows.values():
        snapshot = await manager.get_snapshot(generation_id)
        assert snapshot is not None
        assert (snapshot["status"], snapshot["error_code"], snapshot["retryable"]) == (
            "completed",
            None,
            False,
        )
    stopped = await manager.get_snapshot(open_stop)
    interrupted = await manager.get_snapshot(open_run)
    assert stopped is not None and stopped["status"] == "stopped"
    assert interrupted is not None and interrupted["error_code"] == "restart_interrupted"
    assert await _assistant_count(database, thread.id) == 3  # history untouched
    async with database.sessions() as db:
        snapshots = list((await db.execute(select(Generation.request_snapshot))).scalars())
    assert all(value == {} for value in snapshots)
    await manager.shutdown()


# --- M-9: retry and handoff state ---------------------------------------------------


async def test_retry_is_refused_for_answered_or_superseded_turns(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    calls = {"chat": 0}

    async def fail_once(request: ProviderRequest, emit: Emit, cancel: asyncio.Event) -> None:
        del request, cancel
        calls["chat"] += 1
        if calls["chat"] == 1:
            raise ProviderError("provider_network")
        await emit(_SUCCESS)

    manager = GenerationManager(database, settings, HookProvider(fail_once))
    first = await _submit(manager, user, "first")
    failed = await _wait_status(manager, first.generation_id, {"failed"})
    assert failed["retryable"] is True
    retried = await manager.retry(
        generation_id=first.generation_id, requester_id=user.id, client_request_id="r1"
    )
    await _wait_status(manager, retried, {"completed"})
    # A stale tab retrying the original failure would append a second answer.
    with pytest.raises(GenerationError, match="cannot be retried"):
        await manager.retry(
            generation_id=first.generation_id, requester_id=user.id, client_request_id="r2"
        )
    # The same request id is still answered idempotently.
    assert (
        await manager.retry(
            generation_id=first.generation_id, requester_id=user.id, client_request_id="r1"
        )
        == retried
    )
    stale = await manager.get_snapshot(first.generation_id)
    assert stale is not None and stale["retryable"] is False
    async with database.sessions() as db:
        roles = list(
            (
                await db.execute(
                    select(Message.role)
                    .where(Message.thread_id == first.thread_id)
                    .order_by(Message.ordinal)
                )
            ).scalars()
        )
    assert roles == ["user", "assistant"]
    await manager.shutdown()


async def test_simultaneous_retries_create_one_new_attempt(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    gate = asyncio.Event()
    calls = {"chat": 0}

    async def chat(request: ProviderRequest, emit: Emit, cancel: asyncio.Event) -> None:
        del request, cancel
        calls["chat"] += 1
        if calls["chat"] == 1:
            raise ProviderError("provider_network")
        await gate.wait()
        await emit(_SUCCESS)

    manager = GenerationManager(database, settings, HookProvider(chat))
    first = await _submit(manager, user, "first")
    await _wait_status(manager, first.generation_id, {"failed"})
    results = await asyncio.gather(
        manager.retry(
            generation_id=first.generation_id, requester_id=user.id, client_request_id="a"
        ),
        manager.retry(
            generation_id=first.generation_id, requester_id=user.id, client_request_id="b"
        ),
        return_exceptions=True,
    )
    accepted = [result for result in results if isinstance(result, str)]
    rejected = [result for result in results if isinstance(result, GenerationError)]
    assert len(accepted) == 1 and len(rejected) == 1
    gate.set()
    await _wait_status(manager, accepted[0], {"completed"})
    assert await _assistant_count(database, first.thread_id) == 1
    await manager.shutdown()


# --- M-7: derived request snapshots -------------------------------------------------


async def test_terminal_generations_drop_their_request_snapshot(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    started = asyncio.Event()

    async def outcome(request: ProviderRequest, emit: Emit, cancel: asyncio.Event) -> None:
        text = str(request.snapshot)
        if "please fail" in text:
            raise ProviderError("provider_network")
        if "please stop" in text:
            started.set()
            await cancel.wait()
            raise asyncio.CancelledError
        await emit(_SUCCESS)

    provider = HookProvider(outcome)
    manager = GenerationManager(database, settings, provider)
    done = await _submit(manager, user, "done", text="please complete")
    failed = await _submit(manager, user, "failed", text="please fail")
    stopped = await _submit(manager, user, "stopped", text="please stop")
    await started.wait()
    await manager.stop(stopped.generation_id)
    await _wait_status(manager, done.generation_id, {"completed"})
    await _wait_status(manager, failed.generation_id, {"failed"})
    await _wait_status(manager, stopped.generation_id, {"stopped"})
    async with database.sessions() as db:
        rows = list(
            (
                await db.execute(
                    select(Generation).where(
                        Generation.id.in_(
                            (done.generation_id, failed.generation_id, stopped.generation_id)
                        )
                    )
                )
            ).scalars()
        )
    assert {row.status for row in rows} == {"completed", "failed", "stopped"}
    assert all(row.request_snapshot == {} for row in rows)
    # Content-free diagnostics stay.
    assert any(row.error_code == "provider_network" for row in rows)
    assert all(row.finished_at is not None for row in rows)
    # Retry rebuilds the context from current data, so nothing depended on it.
    retried = await manager.retry(
        generation_id=failed.generation_id, requester_id=user.id, client_request_id="again"
    )
    await _wait_status(manager, retried, {"failed"})
    assert "please fail" in str(provider.requests[-1].snapshot)
    await manager.shutdown()


# --- M-4 and H-2: input accepted only when it can be answered ----------------------


async def test_reference_documents_have_a_word_limit(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, base_settings, user = manager_database
    settings = base_settings.model_copy(update={"max_reference_words": 1_000})
    manager = GenerationManager(database, settings, HookProvider())
    text = " ".join(["word"] * 2_000)
    reference = ValidatedAttachment(
        original_name="reference.docx",
        content=b"synthetic",
        byte_count=9,
        word_count=2_000,
        sha256=hashlib.sha256(b"synthetic").hexdigest(),
        document_blocks=(DocxBlock(id="p000001", kind="paragraph", text=text),),
        plain_text=text,
    )
    with pytest.raises(GenerationError) as caught:
        await manager.submit_turn(
            requester_id=user.id,
            client_request_id="big-reference",
            text="Use this as the style reference.",
            attachment=reference,
            attachment_role="reference",
            owner_id=user.id,
            mode="revision",
        )
    assert caught.value.code == "reference_word_limit"
    async with database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Message)) == 0
    assert not list(settings.attachments_dir.glob("*.docx"))
    await manager.shutdown()


async def test_oversized_pinned_reference_fails_before_any_summary_call(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, base_settings, user = manager_database
    settings = base_settings.model_copy(
        update={"context_compaction_tokens": 12_000, "context_recent_messages": 4}
    )
    content = b"synthetic stored reference"
    async with database.sessions() as db:
        thread = Thread(owner_id=user.id, mode="revision", title="Existing")
        db.add(thread)
        await db.flush()
        for ordinal in range(1, 9):
            role = "user" if ordinal % 2 else "assistant"
            db.add(
                Message(
                    thread_id=thread.id,
                    ordinal=ordinal,
                    role=role,
                    actor_user_id=user.id if role == "user" else None,
                    content=[{"type": "conversation", "text": f"Turn {ordinal}."}],
                )
            )
        await db.flush()
        reference_message = await db.scalar(
            select(Message).where(Message.thread_id == thread.id, Message.ordinal == 7)
        )
        assert reference_message is not None
        storage_name = "00000000-0000-0000-0000-00000000aaaa.docx"
        persist_attachment(settings.attachments_dir, storage_name, content)
        db.add(
            Attachment(
                message_id=reference_message.id,
                storage_name=storage_name,
                original_name="huge-reference.docx",
                role="reference",
                document_blocks=[
                    {"id": "p000001", "kind": "paragraph", "text": "reference " * 15_000}
                ],
                byte_count=len(content),
                word_count=15_000,
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
        await db.commit()
        thread_id = thread.id

    provider = HookProvider()
    manager = GenerationManager(database, settings, provider)
    submitted = await _submit(manager, user, "after-reference", thread_id=thread_id, text="Hi.")
    failed = await _wait_status(manager, submitted.generation_id, {"failed"})
    assert failed["error_code"] == "context_budget_exceeded"
    assert "smaller reference" in str(failed["error_message"])
    assert [request.purpose for request in provider.requests] == []  # no summary calls
    await manager.shutdown()


async def test_source_whose_response_cannot_fit_is_refused_before_any_call(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, base_settings, user = manager_database
    settings = base_settings.model_copy(update={"chat_max_completion_tokens": 2_000})
    provider = HookProvider()
    manager = GenerationManager(database, settings, provider)
    long_text = " ".join(
        f"Sentence number {index} of the synthetic source." for index in range(400)
    )
    with pytest.raises(GenerationError) as caught:
        await _submit(manager, user, "too-long", text=long_text)
    assert caught.value.code == "response_budget_exceeded"
    assert "limit is 2,000" in caught.value.message
    assert provider.requests == []
    async with database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Thread)) == 0
    short = await _submit(manager, user, "fits", text="A short sentence to translate.")
    await _wait_status(manager, short.generation_id, {"completed"})
    await manager.shutdown()


# --- L-8 and L-18 -------------------------------------------------------------------


async def test_title_request_uses_only_the_bounded_first_request(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    provider = HookProvider()
    manager = GenerationManager(database, settings, provider)
    first_request = "Opening request. " + "detail " * 1_000
    submitted = await _submit(manager, user, "titled", text=first_request)
    await _wait_status(manager, submitted.generation_id, {"completed"})
    for _ in range(100):
        if any(request.purpose == "title" for request in provider.requests):
            break
        await asyncio.sleep(0.01)
    title_request = next(request for request in provider.requests if request.purpose == "title")
    untrusted = title_request.snapshot["provider_messages"][1]["content"]
    assert "Opening request." in untrusted
    assert "Translated text" not in untrusted  # the assistant answer is not sent
    assert len(untrusted) < 3_000
    await manager.shutdown()


async def test_provider_calls_share_a_global_concurrency_cap(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, base_settings, user = manager_database
    settings = base_settings.model_copy(update={"max_concurrent_provider_calls": 1})

    async def slow(request: ProviderRequest, emit: Emit, cancel: asyncio.Event) -> None:
        del request, cancel
        await asyncio.sleep(0.05)
        await emit(_SUCCESS)

    provider = HookProvider(slow)
    manager = GenerationManager(database, settings, provider)
    submissions = [await _submit(manager, user, f"parallel-{index}") for index in range(3)]
    for submission in submissions:
        await _wait_status(manager, submission.generation_id, {"completed"})
    assert provider.max_in_flight == 1
    await manager.shutdown()


# --- M-8: usage of stopped, aborted, retried and orphaned calls ----------------------


def _stream_response(chunks: list[str], generation_id: str, *, delay: float = 0) -> httpx.Response:
    async def body() -> AsyncIterator[bytes]:
        for chunk in chunks:
            payload = {"id": generation_id, "choices": [{"delta": {"content": chunk}}]}
            yield f"data: {json.dumps(payload)}\n\n".encode()
            if delay:
                await asyncio.sleep(delay)
        final = {"id": generation_id, "choices": [], "usage": {"cost": "0.25"}}
        yield f"data: {json.dumps(final)}\n\ndata: [DONE]\n\n".encode()

    return httpx.Response(200, headers={"x-request-id": f"req-{generation_id}"}, content=body())


_GOOD = [
    '{"v":1,"event":"response_start"}\n',
    '{"v":1,"event":"block_start","id":"b1","type":"deliverable"}\n',
    '{"v":1,"event":"block_delta","id":"b1","text":"Bonjour"}\n',
    '{"v":1,"event":"block_end","id":"b1"}\n',
    '{"v":1,"event":"state","operation":"none"}\n',
    '{"v":1,"event":"response_end"}\n',
]


def _openrouter_manager(
    database: Database, settings: Settings, handler: Callable[[httpx.Request], httpx.Response]
) -> GenerationManager:
    configured = settings.model_copy(
        update={"openrouter_api_key": SecretStr("sk-or-v1-" + "0" * 64)}
    )
    provider = OpenRouterProvider(configured)
    provider._client = OpenRouterClient(
        configured, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return GenerationManager(database, configured, provider)


async def _ledger(database: Database) -> list[tuple[str, str, int | None, str | None]]:
    async with database.sessions() as db:
        rows = (
            await db.execute(
                select(UsageEvent).where(UsageEvent.provider_generation_id.is_not(None))
            )
        ).scalars()
        return sorted(
            (row.event_type, row.purpose, row.amount_microusd, row.provider_generation_id)
            for row in rows
        )


async def test_protocol_retry_and_stop_leave_every_provider_call_chargeable(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    attempts = {"chat": 0}
    metadata_costs = {"gen-bad": "0.25", "gen-stopped": "0.40"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":  # /generation metadata used by reconciliation
            cost = metadata_costs.get(request.url.params["id"])
            if cost is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"data": {"total_cost": cost}})
        body = json.loads(request.content)
        if body["max_completion_tokens"] == 512:
            return _stream_response(["Synthetic Title"], "gen-title")
        attempts["chat"] += 1
        if attempts["chat"] == 1:
            return _stream_response([*_GOOD[:2], "not json\n", *_GOOD[2:]], "gen-bad")
        if attempts["chat"] == 2:
            return _stream_response(_GOOD, "gen-good")
        return _stream_response(
            [*_GOOD[:2], *['{"v":1,"event":"block_delta","id":"b1","text":"x"}\n'] * 200],
            "gen-stopped",
            delay=0.01,
        )

    manager = _openrouter_manager(database, settings, handler)
    first = await _submit(manager, user, "protocol")
    await _wait_status(manager, first.generation_id, {"completed"})
    second = await _submit(manager, user, "stopped", thread_id=first.thread_id)
    for _ in range(200):
        snapshot = await manager.get_snapshot(second.generation_id)
        if snapshot and snapshot["blocks"]:
            break
        await asyncio.sleep(0.01)
    await manager.stop(second.generation_id)
    await _wait_status(manager, second.generation_id, {"stopped"})
    await asyncio.sleep(0.05)

    before = await _ledger(database)
    assert ("pending", "chat", None, "gen-bad") in before
    assert ("pending", "chat", None, "gen-stopped") in before
    assert ("charge", "chat", 250_000, "gen-good") in before
    await manager.reconcile_pending_costs()
    after = await _ledger(database)
    charges = [row for row in after if row[0] == "charge"]
    # The failed attempt and the stopped stream are charged exactly once each; the
    # already-charged successful call is not charged again.
    assert sorted((row[3], row[2]) for row in charges if row[1] == "chat") == [
        ("gen-bad", 250_000),
        ("gen-good", 250_000),
        ("gen-stopped", 400_000),
    ]
    await manager.reconcile_pending_costs()
    assert await _ledger(database) == after
    await manager.shutdown()


async def test_title_charge_survives_deleting_the_conversation(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    title_started = asyncio.Event()
    release_title = asyncio.Event()

    class SlowTitleProvider(HookProvider):
        async def generate(
            self, request: ProviderRequest, emit: Emit, cancel_event: asyncio.Event
        ) -> ProviderCompletion:
            if request.purpose != "title":
                return await super().generate(request, emit, cancel_event)
            title_started.set()
            await release_title.wait()
            await emit(b"Deleted Conversation Title")
            return ProviderCompletion(provider_request_id="title-call", cost_microusd=5)

    manager = GenerationManager(database, settings, SlowTitleProvider())
    submitted = await _submit(manager, user, "deleted")
    await title_started.wait()
    async with database.sessions() as db:
        await db.execute(delete(Thread).where(Thread.id == submitted.thread_id))
        await db.commit()
    release_title.set()
    task = manager._tasks.get(submitted.generation_id)
    if task is not None:
        await asyncio.wait([task], timeout=5)
    async with database.sessions() as db:
        title_charges = list(
            (
                await db.execute(
                    select(UsageEvent.amount_microusd).where(
                        UsageEvent.event_type == "charge", UsageEvent.purpose == "title"
                    )
                )
            ).scalars()
        )
    assert title_charges == [5]
    await manager.shutdown()


# --- M-5: concurrent and repeated submissions over HTTP ------------------------------


@pytest.mark.parametrize("same_key", [True, False])
async def test_simultaneous_posts_to_one_thread_never_return_500(
    manager_database: tuple[Database, Settings, User], same_key: bool
) -> None:
    database, base_settings, user = manager_database
    settings = base_settings.model_copy(
        update={"public_origin": "http://testserver", "trusted_hosts": ["testserver"]}
    )
    gate = asyncio.Event()

    async def wait_for_gate(request: ProviderRequest, emit: Emit, cancel: asyncio.Event) -> None:
        del request, cancel
        await gate.wait()
        await emit(_SUCCESS)

    async with database.sessions() as db:
        yousra = User(username="yousra", display_name="Yousra", password_hash=user.password_hash)
        db.add(yousra)
        await db.commit()
    app = create_app(settings=settings, database=database, provider=HookProvider(wait_for_gate))
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "charles", "password": "test password"},
                headers={"Origin": "http://testserver"},
            )
            headers = {"X-CSRF-Token": login.json()["csrf_token"], "Origin": "http://testserver"}
            created = await client.post(
                "/api/threads",
                data={"mode": "translate", "text": "Seed.", "client_request_id": "seed"},
                headers=headers,
            )
            thread_id = created.json()["thread_id"]
            gate.set()
            for _ in range(200):
                detail = (await client.get(f"/api/threads/{thread_id}")).json()
                if detail["active_generation_id"] is None:
                    break
                await asyncio.sleep(0.01)
            gate.clear()

            async def post(index: int) -> httpx.Response:
                return await client.post(
                    f"/api/threads/{thread_id}/messages",
                    data={
                        "text": f"Concurrent synthetic turn {index}.",
                        "client_request_id": "same-key" if same_key else f"key-{index}",
                    },
                    headers=headers,
                )

            responses = await asyncio.gather(post(1), post(2))
            gate.set()
            codes = sorted(response.status_code for response in responses)
            if same_key:
                assert codes == [200, 200]
                assert responses[0].json() == responses[1].json()
            else:
                assert codes == [200, 409]
                conflict = next(r for r in responses if r.status_code == 409)
                assert conflict.json()["code"] == "active_generation"
            for _ in range(200):
                detail = (await client.get(f"/api/threads/{thread_id}")).json()
                if detail["active_generation_id"] is None:
                    break
                await asyncio.sleep(0.01)
            ordinals = [message["role"] for message in detail["messages"]]
            assert ordinals == ["user", "assistant", "user", "assistant"]


async def test_running_handoff_is_reported_separately_from_the_chat_turn(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, base_settings, user = manager_database
    settings = base_settings.model_copy(
        update={"public_origin": "http://testserver", "trusted_hosts": ["testserver"]}
    )
    handoff_gate = asyncio.Event()
    calls = {"chat": 0}

    async def chat(request: ProviderRequest, emit: Emit, cancel: asyncio.Event) -> None:
        del cancel
        if request.purpose == "prompt_handoff":
            await handoff_gate.wait()
            await emit(
                b'{"v":1,"event":"response_start"}\n'
                b'{"v":1,"event":"block_start","id":"b1","type":"deliverable"}\n'
                b'{"v":1,"event":"block_delta","id":"b1","text":"Handoff text"}\n'
                b'{"v":1,"event":"block_end","id":"b1"}\n'
                b'{"v":1,"event":"state","operation":"none"}\n'
                b'{"v":1,"event":"response_end"}\n'
            )
            return
        calls["chat"] += 1
        if calls["chat"] == 2:
            raise ProviderError("provider_network")
        await emit(_SUCCESS)

    async with database.sessions() as db:
        db.add(User(username="yousra", display_name="Yousra", password_hash=user.password_hash))
        await db.commit()
    app = create_app(settings=settings, database=database, provider=HookProvider(chat))
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "charles", "password": "test password"},
                headers={"Origin": "http://testserver"},
            )
            headers = {"X-CSRF-Token": login.json()["csrf_token"], "Origin": "http://testserver"}
            created = (
                await client.post(
                    "/api/threads",
                    data={"mode": "translate", "text": "One.", "client_request_id": "c1"},
                    headers=headers,
                )
            ).json()
            await asyncio.sleep(0.2)
            second = (
                await client.post(
                    f"/api/threads/{created['thread_id']}/messages",
                    data={"text": "Two.", "client_request_id": "c2"},
                    headers=headers,
                )
            ).json()
            await asyncio.sleep(0.2)
            handoff = (
                await client.post(
                    f"/api/threads/{created['thread_id']}/prompt-handoff",
                    json={"client_request_id": "h1"},
                    headers=headers,
                )
            ).json()
            await asyncio.sleep(0.1)
            during = (await client.get(f"/api/threads/{created['thread_id']}")).json()
            assert during["generation"]["id"] == second["generation_id"]
            assert during["generation"]["status"] == "failed"
            assert during["handoff"]["id"] == handoff["generation_id"]
            handoff_gate.set()
            await asyncio.sleep(0.2)
            after = (await client.get(f"/api/threads/{created['thread_id']}")).json()
            assert after["generation"]["id"] == second["generation_id"]
            assert after["generation"]["retryable"] is True
            assert after["handoff"] is None
            retry = await client.post(
                f"/api/generations/{handoff['generation_id']}/retry",
                json={"client_request_id": "retry-handoff"},
                headers=headers,
            )
            assert retry.status_code == 409  # handoffs are re-requested, not retried


async def test_submission_to_an_active_thread_is_rejected_cleanly(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    gate = asyncio.Event()

    async def wait_for_gate(request: ProviderRequest, emit: Emit, cancel: asyncio.Event) -> None:
        del request, cancel
        await gate.wait()
        await emit(_SUCCESS)

    manager = GenerationManager(database, settings, HookProvider(wait_for_gate))
    first = await _submit(manager, user, "first")
    with pytest.raises(ActiveGenerationError):
        await _submit(manager, user, "second", thread_id=first.thread_id)
    gate.set()
    await _wait_status(manager, first.generation_id, {"completed"})
    await manager.shutdown()
