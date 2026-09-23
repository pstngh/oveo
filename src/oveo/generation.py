from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, cast

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from oveo.attachments import (
    ValidatedAttachment,
    count_words,
    is_managed_attachment_name,
    persist_attachment,
)
from oveo.config import Settings
from oveo.context import (
    HANDOFF_SENTINEL,
    AttachmentDocument,
    AttachmentRole,
    build_handoff_merge_messages,
    build_provider_messages,
)
from oveo.db import Database
from oveo.diagnostics import log_unexpected
from oveo.docx import (
    DOCX_MEDIA_TYPE,
    DocxBlock,
    DocxError,
    DocxReplacement,
    ExtractedDocx,
    docx_blocks_from_storage,
    docx_uncompressed_limit,
    extract_docx,
    plain_text_from_replacements,
    require_matching_blocks,
)
from oveo.models import (
    Attachment,
    Generation,
    Message,
    Thread,
    UsageEvent,
    User,
    WorkItem,
    WorkVersion,
    new_id,
    utc_now,
)
from oveo.protocol import (
    AppendState,
    EstablishState,
    FullState,
    NoState,
    OutputReplacement,
    ProtocolDecoder,
    ProtocolDocument,
    ProtocolError,
    ProtocolEvent,
    ReplaceState,
    SourceOutputReplacement,
)
from oveo.provider import (
    IdsCallback,
    OpenRouterClient,
    ProviderMessage,
    ReasoningEffort,
    count_input_tokens,
    count_text_tokens,
)
from oveo.provider import ProviderError as OpenRouterError
from oveo.usage import append_usage_event
from oveo.workers import BoundedWorker, WorkerBusy

EmitChunk = Callable[[bytes], Awaitable[None]]
_T = TypeVar("_T")


class GenerationError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ActiveGenerationError(GenerationError):
    def __init__(self) -> None:
        super().__init__(
            "active_generation",
            "This conversation already has an active generation.",
            status_code=409,
        )


class ProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        provider_request_id: str | None = None,
        provider_generation_id: str | None = None,
        cost_microusd: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.provider_request_id = provider_request_id
        self.provider_generation_id = provider_generation_id
        self.cost_microusd = cost_microusd


class StaleStateError(RuntimeError):
    """The model targeted canonical state that is no longer current."""


class StatePersistenceError(RuntimeError):
    """A valid visible response could not be applied to canonical state."""


class DocxTemplateOutdatedError(RuntimeError):
    """The stored DOCX block map no longer describes its template safely."""


@dataclass(frozen=True, slots=True)
class _TemplateRef:
    """Plain attachment fields handed to the DOCX worker thread."""

    id: str
    storage_name: str
    media_type: str
    byte_count: int
    sha256: str
    stored_blocks: object


def _prepared_template(
    templates: Mapping[str, ExtractedDocx] | None, attachment_id: str
) -> ExtractedDocx:
    extracted = (templates or {}).get(attachment_id)
    if extracted is None:
        raise ProtocolError("state_docx_template_missing")
    return extracted


class SummaryFormatError(ValueError):
    """A maintenance summary did not satisfy its closed response schema."""


def _snapshot_messages(
    snapshot: Mapping[str, Any], *, error_code: str = "context_compaction_failed"
) -> list[ProviderMessage]:
    raw_messages = snapshot.get("provider_messages")
    if not isinstance(raw_messages, list):
        raise ProviderError(error_code)
    messages: list[ProviderMessage] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            raise ProviderError(error_code)
        role = item.get("role")
        content = item.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            raise ProviderError(error_code)
        messages.append(
            ProviderMessage(
                role=cast(Literal["system", "user", "assistant"], role),
                content=content,
            )
        )
    if not messages:
        raise ProviderError(error_code)
    return messages


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    generation_id: str
    purpose: str
    mode: str
    snapshot: Mapping[str, Any]
    # Optional observer for the provider's call identifiers (see provider.IdsCallback).
    on_provider_ids: IdsCallback | None = None


@dataclass(frozen=True, slots=True)
class ProviderCompletion:
    provider_request_id: str | None = None
    provider_generation_id: str | None = None
    cost_microusd: int | None = None


class GenerationProvider(Protocol):
    async def generate(
        self,
        request: ProviderRequest,
        emit: EmitChunk,
        cancel_event: asyncio.Event,
    ) -> ProviderCompletion: ...


class UnavailableProvider:
    async def generate(
        self,
        request: ProviderRequest,
        emit: EmitChunk,
        cancel_event: asyncio.Event,
    ) -> ProviderCompletion:
        del request, emit, cancel_event
        raise ProviderError("provider_not_configured")


@dataclass(frozen=True, slots=True)
class _PurposeLimits:
    max_completion_tokens: int
    reasoning_effort: ReasoningEffort
    deadline_seconds: float | None = None


# Maintenance calls get room for reasoning plus their small answers. A 2-6 word title
# needs little reasoning, so it uses low effort with a short deadline. Summaries and
# handoffs keep the chat effort because their quality carries into later turns.
_MAINTENANCE_LIMITS: dict[str, _PurposeLimits] = {
    "title": _PurposeLimits(512, "low", deadline_seconds=120.0),
    "summary": _PurposeLimits(16_384, "high"),
    "prompt_handoff": _PurposeLimits(16_384, "high"),
}
_PROVIDER_ERROR_CODES = {
    "provider_incomplete_stream": "provider_stream_error",
    "provider_malformed_stream": "provider_stream_error",
    "provider_output_limit": "response_too_long",
    "provider_content_filter": "provider_content_filtered",
}


class OpenRouterProvider:
    """Adapt the tested OpenRouter client to the generation-manager callback contract."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = OpenRouterClient(settings)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def reconcile_cost(self, provider_generation_id: str) -> int | None:
        try:
            return await self._client.generation_cost(provider_generation_id)
        except OpenRouterError:
            return None

    async def generate(
        self,
        request: ProviderRequest,
        emit: EmitChunk,
        cancel_event: asyncio.Event,
    ) -> ProviderCompletion:
        messages = _snapshot_messages(request.snapshot, error_code="invalid_request_snapshot")
        limits = _MAINTENANCE_LIMITS.get(
            request.purpose,
            _PurposeLimits(self._settings.chat_max_completion_tokens, "high"),
        )

        async def on_delta(delta: str) -> None:
            if cancel_event.is_set():
                raise asyncio.CancelledError
            await emit(delta.encode("utf-8"))

        try:
            completion = await self._client.stream_chat(
                messages,
                max_completion_tokens=limits.max_completion_tokens,
                reasoning_effort=limits.reasoning_effort,
                on_delta=on_delta,
                on_ids=request.on_provider_ids,
                deadline_seconds=limits.deadline_seconds,
            )
        except OpenRouterError as exc:
            # OpenRouterClient has already exhausted safe pre-content retries.
            if exc.code == "provider_output_limit":
                # Content-free numbers that let an operator calibrate the output cap.
                _LOGGER.error(
                    "provider_output_limit purpose=%s max_completion_tokens=%s "
                    "output_tokens=%s reasoning_tokens=%s",
                    request.purpose,
                    limits.max_completion_tokens,
                    exc.usage.output_tokens if exc.usage else None,
                    exc.usage.reasoning_tokens if exc.usage else None,
                )
            raise ProviderError(
                _PROVIDER_ERROR_CODES.get(exc.code, exc.code),
                provider_request_id=exc.provider_request_id,
                provider_generation_id=exc.provider_generation_id,
                cost_microusd=(exc.usage.cost_microusd if exc.usage else None),
            ) from exc
        return ProviderCompletion(
            provider_request_id=completion.provider_request_id,
            provider_generation_id=completion.provider_generation_id,
            cost_microusd=(completion.usage.cost_microusd if completion.usage else None),
        )


@dataclass(frozen=True, slots=True)
class Submission:
    thread_id: str
    generation_id: str


_TERMINAL = frozenset({"completed", "failed", "stopped"})
_ACTIVE = frozenset({"queued", "running", "stopping"})
_TRUNCATED_RESPONSE_CODES = frozenset({"response_too_long", "provider_content_filtered"})
_MIN_RECENT_MESSAGES = 4
_RECONCILE_BATCH_SIZE = 8
_RECONCILE_CANDIDATES = 64
_RECONCILE_MAX_ATTEMPTS = 8
_RECONCILE_BASE_DELAY_SECONDS = 5.0
_RECONCILE_MAX_DELAY_SECONDS = 600.0
_CANCEL_WAIT_SECONDS = 1.0
# Bounded backoff for terminal status writes that hit a locked or failing database.
_WRITE_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
# Title input comes from the first request only, bounded to this many characters.
_TITLE_INPUT_CHARS = 2_000
# Framing measured with o200k_base: 18 tokens per NDJSON delta line around its text
# (at the largest recommended delta size), 11 per DOCX block entry, ~100 for the rest.
_DELTA_FRAMING_TOKENS = 18
_DELTA_CHARS = 200
_STATE_FRAMING_TOKENS = 100
_DOCX_BLOCK_FRAMING_TOKENS = 11


@dataclass(frozen=True, slots=True)
class _UsageContext:
    """Ledger identity for one generation's provider calls.

    Usage rows deliberately do not depend on the generation row, which disappears with
    its conversation: a call made just before a deletion is still charged.
    """

    generation_id: str
    thread_id: str | None
    requester_id: str


_LOGGER = logging.getLogger("oveo.background")
_ERROR_MESSAGES = {
    "provider_not_configured": "The model provider is not configured.",
    "provider_network": "The model provider could not be reached.",
    "provider_transient": "The model provider is temporarily unavailable.",
    "provider_rejected": "The model provider rejected the request.",
    "provider_stream_error": (
        "The model provider returned an incomplete or invalid response stream."
    ),
    "provider_timeout": "The model provider did not finish this response in time.",
    "provider_content_filtered": "The model provider's content filter stopped this response.",
    "response_too_long": (
        "This response reached the model's output limit before it was complete. "
        "Shorten the document or split the request into smaller parts, then try again."
    ),
    "protocol_error": "The model returned an invalid response format.",
    "stale_state": (
        "The saved work changed before this response could be applied. "
        "Retry to use the latest version."
    ),
    "state_persistence_failed": (
        "The response was generated, but its document update could not be saved. "
        "The visible response is preserved; retry before continuing."
    ),
    "docx_template_outdated": (
        "This Word document was imported by an earlier Oveo version that could not edit "
        "its layout safely. Upload the document again to continue editing it."
    ),
    "context_compaction_failed": "Oveo could not safely fit this conversation in context.",
    "handoff_turn_too_large": (
        "One user turn is too large to create a safe prompt handoff. "
        "Split that turn into smaller messages and try again."
    ),
    "restart_interrupted": "Generation was interrupted by an application restart.",
    "generation_interrupted": "Generation was interrupted before it could finish.",
    "context_budget_exceeded": (
        "The active reference document, current work, and latest messages are too large "
        "to fit in the model's context. Upload a smaller reference document or start a "
        "new conversation."
    ),
    "provider_error": "Oveo could not complete this response.",
}

_APPEND_SEPARATORS = {
    "none": "",
    "space": " ",
    "line": "\n",
    "paragraph": "\n\n",
}


def _replacement_span(text: str, anchor: str, *, label: str) -> tuple[int, int]:
    first = text.find(anchor)
    if first < 0:
        raise ProtocolError(f"state_missing_{label}_anchor")
    if text.find(anchor, first + len(anchor)) >= 0:
        raise ProtocolError(f"state_ambiguous_{label}_anchor")
    return first, first + len(anchor)


def _apply_base_replacements(
    text: str,
    replacements: Sequence[tuple[str, str]],
    *,
    label: str,
) -> str:
    edits = [
        (*_replacement_span(text, anchor, label=label), replacement)
        for anchor, replacement in replacements
    ]
    ordered = sorted(edits, key=lambda edit: edit[0])
    if any(left[1] > right[0] for left, right in pairwise(ordered)):
        raise ProtocolError(f"state_overlapping_{label}_anchors")
    result = text
    for start, end, replacement in reversed(ordered):
        result = f"{result[:start]}{replacement}{result[end:]}"
    return result


def _append_text(base: str, addition: str, separator: str) -> str:
    try:
        joiner = _APPEND_SEPARATORS[separator]
    except KeyError as exc:  # Defensive: the decoder owns the closed enum.
        raise ProtocolError("invalid_append_separator") from exc
    return f"{base}{joiner}{addition}"


def _usage_of(generation: Generation) -> _UsageContext:
    return _UsageContext(
        generation_id=generation.id,
        thread_id=generation.thread_id,
        requester_id=generation.requester_id,
    )


def _count_pair(first: str, second: str) -> tuple[int, int]:
    return count_text_tokens(first), count_text_tokens(second)


def _single_deliverable(document: ProtocolDocument) -> str:
    """Return the one visible deliverable that every state mutation must carry."""

    deliverables = [block.text for block in document.blocks if block.type == "deliverable"]
    if len(deliverables) != 1:
        raise ProtocolError("state_deliverable_count_mismatch")
    return deliverables[0]


# Model mistakes in the state event that a strict protocol retry can repair.
_REPAIRABLE_STATE_CODES = frozenset(
    {
        "state_deliverable_count_mismatch",
        "state_deliverable_mismatch",
        "state_source_missing",
        "state_docx_source_mismatch",
        "state_docx_output_mismatch",
        "state_output_addition_missing",
    }
)


def _parse_context_summary(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SummaryFormatError from exc
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "summary",
        "unresolved",
    }:
        raise SummaryFormatError
    version = payload.get("version")
    summary = payload.get("summary")
    unresolved = payload.get("unresolved")
    if (
        isinstance(version, bool)
        or version != 1
        or not isinstance(summary, str)
        or not summary.strip()
        or not isinstance(unresolved, list)
        or any(not isinstance(item, str) for item in unresolved)
    ):
        raise SummaryFormatError
    compacted = summary.strip()
    unresolved_text = [item.strip() for item in unresolved if item.strip()]
    if unresolved_text:
        compacted += "\n\nUnresolved:\n" + "\n".join(f"- {item}" for item in unresolved_text)
    return compacted


class GenerationManager:
    def __init__(
        self, database: Database, settings: Settings, provider: GenerationProvider
    ) -> None:
        self.database = database
        self.settings = settings
        self.provider = provider
        # One thread each: a DOCX parse can use tens of MiB, so request-path parses are
        # admitted one running plus one waiting. Token counting is lighter but still
        # CPU-bound and kept off the event loop.
        self.docx_worker = BoundedWorker("oveo-docx", max_pending=2)
        self.token_worker = BoundedWorker("oveo-tokens", max_pending=4)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel: dict[str, asyncio.Event] = {}
        self._conditions: dict[str, asyncio.Condition] = {}
        # Generations committed but not yet scheduled. One process owns every
        # generation, so an active row that is neither here nor in _tasks is orphaned.
        self._starting: set[str] = set()
        self._provider_slots = asyncio.Semaphore(settings.max_concurrent_provider_calls)
        self._reconcile_lock = asyncio.Lock()
        self._reconcile_task: asyncio.Task[None] | None = None
        self._reconcile_requested = False
        self._reconcile_timer: asyncio.TimerHandle | None = None
        # Per pending-row attempts and next eligible time (monotonic seconds).
        self._reconcile_backoff: dict[str, tuple[int, float]] = {}
        self._shutting_down = False

    def _is_live(self, generation_id: str) -> bool:
        task = self._tasks.get(generation_id)
        return generation_id in self._starting or (task is not None and not task.done())

    async def reconcile_orphans(self) -> int:
        """Finish rows left active by a previous process, keeping committed answers.

        Nothing runs yet, so every active row is orphaned. A row whose answer was
        committed is completed (an unconditional Stop could previously overwrite
        `completed`); a pending Stop becomes `stopped`; unfinished work becomes a
        retryable failure. Transcript messages are never touched.
        """

        now = utc_now()
        finished: dict[str, Any] = {
            "request_snapshot": {},
            "stream_revision": Generation.stream_revision + 1,
        }
        async with self.database.sessions() as db:
            answered = await db.execute(
                update(Generation)
                .where(
                    Generation.result_message_id.is_not(None),
                    Generation.status != "completed",
                )
                .values(
                    status="completed",
                    error_code=None,
                    finished_at=func.coalesce(Generation.finished_at, now),
                    **finished,
                )
                .execution_options(synchronize_session=False)
            )
            stopped = await db.execute(
                update(Generation)
                .where(Generation.status == "stopping")
                .values(
                    status="stopped",
                    error_code=None,
                    partial_blocks=[],
                    finished_at=now,
                    **finished,
                )
                .execution_options(synchronize_session=False)
            )
            interrupted = await db.execute(
                update(Generation)
                .where(Generation.status.in_(("queued", "running")))
                .values(
                    status="failed",
                    error_code="restart_interrupted",
                    partial_blocks=[],
                    finished_at=now,
                    **finished,
                )
                .execution_options(synchronize_session=False)
            )
            await db.commit()
        finished_rows = 0
        for result in (answered, stopped, interrupted):
            finished_rows += int(result.rowcount or 0)  # type: ignore[attr-defined]
        return finished_rows

    @staticmethod
    async def _transition(
        db: AsyncSession,
        generation_id: str,
        *,
        allowed: tuple[str, ...],
        status: str,
        **values: Any,
    ) -> bool:
        """Compare-and-set a status in one statement; False if the row moved on.

        SQLite reads outside the write lock, so read-modify-write on a status can
        overwrite a concurrent terminal state. Every transition is conditional instead.
        Terminal states drop the derived request snapshot in the same statement.
        """

        if status in _TERMINAL:
            values.setdefault("finished_at", utc_now())
            values.setdefault("request_snapshot", {})
        result = await db.execute(
            update(Generation)
            .where(Generation.id == generation_id, Generation.status.in_(allowed))
            .values(status=status, stream_revision=Generation.stream_revision + 1, **values)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0) == 1  # type: ignore[attr-defined]

    async def _with_write_retries(self, write: Callable[[], Awaitable[_T]]) -> _T:
        """Retry a self-contained, idempotent write on a locked or failing database."""

        for delay in (*_WRITE_RETRY_DELAYS, None):
            try:
                return await write()
            except OperationalError:
                if delay is None:
                    raise
                _LOGGER.error("database_write_retry delay_seconds=%s", delay)
                await asyncio.sleep(delay)
        raise AssertionError("write retry loop did not return or raise")

    async def reconcile_pending_costs(self) -> int:
        """Append charges for provider calls whose final stream omitted usage."""

        reconcile = getattr(self.provider, "reconcile_cost", None)
        if reconcile is None:
            return 0
        async with self._reconcile_lock:
            # Any charge for the same provider call settles it, whether it came from the
            # stream's own usage or from an earlier reconciliation.
            charge = aliased(UsageEvent)
            charged = (
                select(charge.id)
                .where(
                    charge.event_type == "charge",
                    charge.provider_generation_id == UsageEvent.provider_generation_id,
                )
                .correlate(UsageEvent)
                .exists()
            )
            async with self.database.sessions() as db:
                candidates = list(
                    (
                        await db.execute(
                            select(UsageEvent)
                            .where(
                                UsageEvent.event_type == "pending",
                                UsageEvent.provider_generation_id.is_not(None),
                                ~charged,
                            )
                            .order_by(UsageEvent.created_at, UsageEvent.id)
                            .limit(_RECONCILE_CANDIDATES)
                        )
                    ).scalars()
                )

            # Oldest first, skipping rows in backoff, so metadata that is not ready yet
            # (or never will be) cannot starve the rest of the queue.
            now = asyncio.get_running_loop().time()
            candidate_ids = {event.id for event in candidates}
            for event_id in tuple(self._reconcile_backoff):
                if event_id not in candidate_ids:  # settled elsewhere or no longer pending
                    del self._reconcile_backoff[event_id]
            pending = [
                event
                for event in candidates
                if self._reconcile_backoff.get(event.id, (0, 0.0))[1] <= now
                and self._reconcile_backoff.get(event.id, (0, 0.0))[0] < _RECONCILE_MAX_ATTEMPTS
            ][:_RECONCILE_BATCH_SIZE]
            reconciled = 0
            for event in pending:
                provider_generation_id = event.provider_generation_id
                if provider_generation_id is None:
                    continue
                dedupe_key = f"{provider_generation_id}:{event.purpose}:reconciled-charge"
                try:
                    cost_microusd = await asyncio.wait_for(
                        reconcile(provider_generation_id),
                        timeout=self.settings.provider_metadata_timeout_seconds,
                    )
                except Exception:
                    # Reconciliation is best effort and must never make startup/readiness
                    # depend on a metadata endpoint. No provider body is retained or logged.
                    cost_microusd = None
                if cost_microusd is None:
                    # Unknown amounts are never invented; the row waits and is retried.
                    self._defer_reconciliation(event.id)
                    continue
                self._reconcile_backoff.pop(event.id, None)
                async with self.database.sessions() as db:
                    added = await append_usage_event(
                        db,
                        dedupe_key=dedupe_key,
                        event_type="charge",
                        purpose=event.purpose,
                        amount_microusd=cost_microusd,
                        generation_id=event.generation_id,
                        thread_id=event.thread_id,
                        requester_id=event.requester_id,
                        provider_request_id=event.provider_request_id,
                        provider_generation_id=provider_generation_id,
                    )
                    await db.commit()
                if added is not None:
                    reconciled += 1
            self._schedule_deferred_reconciliation()
            return reconciled

    def _defer_reconciliation(self, event_id: str) -> None:
        attempts = self._reconcile_backoff.get(event_id, (0, 0.0))[0] + 1
        delay = min(
            _RECONCILE_BASE_DELAY_SECONDS * 2 ** (attempts - 1), _RECONCILE_MAX_DELAY_SECONDS
        )
        self._reconcile_backoff[event_id] = (
            attempts,
            asyncio.get_running_loop().time() + delay,
        )

    def _schedule_deferred_reconciliation(self) -> None:
        """Wake up once for the earliest row still in backoff (bounded attempts)."""

        waiting = [
            ready_at
            for attempts, ready_at in self._reconcile_backoff.values()
            if attempts < _RECONCILE_MAX_ATTEMPTS
        ]
        if not waiting or self._shutting_down:
            return
        loop = asyncio.get_running_loop()
        if self._reconcile_timer is not None:
            self._reconcile_timer.cancel()
        wake_at = max(min(waiting), loop.time() + 1.0)
        self._reconcile_timer = loop.call_at(wake_at, self.reconcile_later)

    def reconcile_later(self) -> None:
        """Coalesce a best-effort reconciliation pass outside request/readiness paths."""

        if self._shutting_down:
            return
        if self._reconcile_task is not None and not self._reconcile_task.done():
            self._reconcile_requested = True
            return
        self._reconcile_requested = False
        task = asyncio.create_task(self._reconcile_safely())
        self._reconcile_task = task

        def finished(_task: asyncio.Task[None]) -> None:
            self._reconcile_task = None
            if self._reconcile_requested and not self._shutting_down:
                self.reconcile_later()

        task.add_done_callback(finished)

    async def _reconcile_safely(self) -> None:
        try:
            await self.reconcile_pending_costs()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_unexpected(_LOGGER, error, area="cost_reconciliation")

    async def sweep_orphan_attachments(self) -> int:
        """Remove only generated attachment files that have no live database row."""

        directory = self.settings.attachments_dir
        if not directory.is_dir():
            return 0
        async with self.database.sessions() as db:
            referenced = set((await db.execute(select(Attachment.storage_name))).scalars())
        removed = 0
        for candidate in directory.iterdir():
            if (
                not candidate.is_file()
                or not is_managed_attachment_name(candidate.name)
                or candidate.name in referenced
            ):
                continue
            try:
                candidate.unlink()
            except OSError:
                continue
            removed += 1
        return removed

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._reconcile_timer is not None:
            self._reconcile_timer.cancel()
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        reconcile_task = self._reconcile_task
        if reconcile_task is not None and not reconcile_task.done():
            reconcile_task.cancel()
            await asyncio.gather(reconcile_task, return_exceptions=True)
        close = getattr(self.provider, "aclose", None)
        if close is not None:
            await close()
        self.docx_worker.close()
        self.token_worker.close()

    async def _snapshot_tokens(self, snapshot: Mapping[str, Any]) -> int:
        messages = _snapshot_messages(snapshot)
        return await self.token_worker.run(partial(count_input_tokens, messages), wait=True)

    def _schedule(self, generation_id: str) -> None:
        if generation_id in self._tasks:
            return
        cancel_event = asyncio.Event()
        self._cancel[generation_id] = cancel_event
        task = asyncio.create_task(self._run(generation_id, cancel_event))
        self._tasks[generation_id] = task

        def finished(done_task: asyncio.Task[None]) -> None:
            self._tasks.pop(generation_id, None)
            self._cancel.pop(generation_id, None)
            self._conditions.pop(generation_id, None)
            if not done_task.cancelled() and (error := done_task.exception()) is not None:
                log_unexpected(_LOGGER, error, area="generation_task")

        task.add_done_callback(finished)

    async def _request_snapshot(
        self,
        db: AsyncSession,
        thread: Thread,
        *,
        purpose: str,
        after_ordinal: int | None = None,
        through_ordinal: int | None = None,
    ) -> dict[str, Any]:
        if purpose != "prompt_handoff" and thread.summary_through_ordinal is not None:
            after_ordinal = max(after_ordinal or 0, thread.summary_through_ordinal)
        message_query = select(Message).where(Message.thread_id == thread.id)
        if after_ordinal is not None:
            message_query = message_query.where(Message.ordinal > after_ordinal)
        if through_ordinal is not None:
            message_query = message_query.where(Message.ordinal <= through_ordinal)
        messages = list((await db.execute(message_query.order_by(Message.ordinal))).scalars())
        actor_ids = {message.actor_user_id for message in messages if message.actor_user_id}
        actors = {
            user.id: user.display_name
            for user in (await db.execute(select(User).where(User.id.in_(actor_ids)))).scalars()
        }
        attachment_query = (
            select(Attachment)
            .join(Message, Attachment.message_id == Message.id)
            .where(Message.thread_id == thread.id)
        )
        if purpose != "prompt_handoff":
            attachment_query = attachment_query.where(Attachment.role == "source")
        if after_ordinal is not None:
            attachment_query = attachment_query.where(Message.ordinal > after_ordinal)
        if through_ordinal is not None:
            attachment_query = attachment_query.where(Message.ordinal <= through_ordinal)
        attachment_rows = list((await db.execute(attachment_query)).scalars())
        attachments: dict[str, AttachmentDocument] = {}
        for attachment in attachment_rows:
            attachments[attachment.message_id] = self._attachment_document(attachment)
        active_reference_document = None
        if purpose == "chat":
            active_reference = await db.scalar(
                select(Attachment)
                .join(Message, Attachment.message_id == Message.id)
                .where(
                    Message.thread_id == thread.id,
                    Attachment.role == "reference",
                )
                .order_by(Message.ordinal.desc())
                .limit(1)
            )
            if active_reference is not None:
                active_reference_document = self._attachment_document(active_reference)
        canonical = await db.scalar(
            select(WorkVersion)
            .join(WorkItem, WorkVersion.work_item_id == WorkItem.id)
            .where(WorkItem.thread_id == thread.id, WorkItem.active.is_(True))
            .order_by(WorkVersion.version_no.desc())
            .limit(1)
        )
        provider_messages = build_provider_messages(
            thread,
            purpose=cast(Any, purpose),
            recent_messages=messages,
            actor_labels=actors,
            attachments=attachments,
            active_reference_document=active_reference_document,
            canonical_state=canonical,
        )
        return {
            "schema_version": 1,
            "mode": thread.mode,
            "provider_messages": [
                {"role": message.role, "content": message.content} for message in provider_messages
            ],
        }

    def _attachment_document(self, attachment: Attachment) -> AttachmentDocument:
        path = self.settings.attachments_dir / attachment.storage_name
        try:
            content = path.read_bytes()
            if (
                len(content) != attachment.byte_count
                or hashlib.sha256(content).hexdigest() != attachment.sha256
            ):
                raise OSError("attachment integrity mismatch")
            if attachment.media_type != DOCX_MEDIA_TYPE:
                raise OSError("unsupported attachment media type")
            document_blocks = docx_blocks_from_storage(attachment.document_blocks)
            if attachment.role not in {"source", "reference"}:
                raise OSError("unsupported attachment role")
        except (OSError, DocxError) as exc:
            raise GenerationError(
                "attachment_unavailable",
                "The saved attachment is unavailable.",
            ) from exc
        return AttachmentDocument(
            word_count=attachment.word_count,
            document_blocks=document_blocks,
            role=cast(AttachmentRole, attachment.role),
        )

    async def submit_turn(
        self,
        *,
        requester_id: str,
        client_request_id: str,
        text: str,
        attachment: ValidatedAttachment | None,
        attachment_role: AttachmentRole = "source",
        thread_id: str | None = None,
        owner_id: str | None = None,
        mode: str | None = None,
    ) -> Submission:
        staged_files: list[Path] = []
        try:
            return await self._submit_turn(
                requester_id=requester_id,
                client_request_id=client_request_id,
                text=text,
                attachment=attachment,
                attachment_role=attachment_role,
                thread_id=thread_id,
                owner_id=owner_id,
                mode=mode,
                staged_files=staged_files,
            )
        finally:
            # Staged files survive only if their rows were committed.
            for staged_file in staged_files:
                staged_file.unlink(missing_ok=True)

    async def _submit_turn(
        self,
        *,
        requester_id: str,
        client_request_id: str,
        text: str,
        attachment: ValidatedAttachment | None,
        attachment_role: AttachmentRole,
        thread_id: str | None,
        owner_id: str | None,
        mode: str | None,
        staged_files: list[Path],
    ) -> Submission:
        clean_text = text.strip()
        if not clean_text and attachment is None:
            raise GenerationError("empty_message", "Enter a message or attach a DOCX file.")
        if attachment_role not in {"source", "reference"}:
            raise GenerationError("invalid_attachment_role", "Choose how to use the attachment.")
        if attachment is None and attachment_role != "source":
            raise GenerationError(
                "invalid_attachment_role",
                "An attachment role requires an attachment.",
            )
        if not client_request_id or len(client_request_id) > 100:
            raise GenerationError("invalid_request_id", "The request identifier is invalid.")
        await self._check_response_budget(clean_text, attachment, attachment_role)

        generation_id = new_id()
        self._starting.add(generation_id)
        try:
            try:
                return await self._insert_turn(
                    generation_id=generation_id,
                    requester_id=requester_id,
                    client_request_id=client_request_id,
                    text=text,
                    clean_text=clean_text,
                    attachment=attachment,
                    attachment_role=attachment_role,
                    thread_id=thread_id,
                    owner_id=owner_id,
                    mode=mode,
                    staged_files=staged_files,
                )
            except IntegrityError as exc:
                # A concurrent submission won a unique constraint (idempotency key, one
                # active generation per thread, or the thread itself went away).
                return await self._resolve_submission_conflict(
                    requester_id=requester_id,
                    client_request_id=client_request_id,
                    thread_id=thread_id,
                    cause=exc,
                )
        finally:
            self._starting.discard(generation_id)

    async def _insert_turn(
        self,
        *,
        generation_id: str,
        requester_id: str,
        client_request_id: str,
        text: str,
        clean_text: str,
        attachment: ValidatedAttachment | None,
        attachment_role: AttachmentRole,
        thread_id: str | None,
        owner_id: str | None,
        mode: str | None,
        staged_files: list[Path],
    ) -> Submission:
        finalized: list[str] = []
        async with self.database.sessions() as db:
            existing = await self._existing_request(db, requester_id, client_request_id)
            if existing is not None:
                if existing.thread_id is None:
                    raise GenerationError("invalid_generation", "The saved request is invalid.")
                return Submission(existing.thread_id, existing.id)

            if thread_id is None:
                if owner_id is None or mode not in {
                    "translate",
                    "revision",
                    "internal_comms",
                }:
                    raise GenerationError("invalid_thread", "Choose a conversation mode.")
                if owner_id != requester_id:
                    raise GenerationError(
                        "thread_not_found", "Conversation not found.", status_code=404
                    )
                thread = Thread(
                    id=new_id(),
                    owner_id=owner_id,
                    mode=mode,
                    title="New conversation",
                )
                db.add(thread)
                await db.flush()
            else:
                existing_thread = await db.get(Thread, thread_id)
                if existing_thread is None or existing_thread.owner_id != requester_id:
                    raise GenerationError(
                        "thread_not_found", "Conversation not found.", status_code=404
                    )
                thread = existing_thread
                finalized = await self._finalize_orphans(db, thread.id)

            if (
                attachment is not None
                and attachment_role == "reference"
                and attachment.word_count > self.settings.max_reference_words
            ):
                raise GenerationError(
                    "reference_word_limit",
                    f"The reference document contains {attachment.word_count:,} words; the "
                    f"maximum is {self.settings.max_reference_words:,} words.",
                )
            if attachment is not None and attachment_role == "source":
                # A new source DOCX always establishes a new work item, so the words of
                # the work it would replace are not counted.
                measured_words = attachment.word_count
            else:
                latest_words = await db.scalar(
                    select(WorkVersion.source_word_count)
                    .join(WorkItem, WorkVersion.work_item_id == WorkItem.id)
                    .where(WorkItem.thread_id == thread.id, WorkItem.active.is_(True))
                    .order_by(WorkVersion.version_no.desc())
                    .limit(1)
                )
                measured_words = int(latest_words or 0) + count_words(clean_text)
            if measured_words > self.settings.max_source_words:
                raise GenerationError(
                    "source_word_limit",
                    f"Source contains {measured_words:,} words; the maximum is "
                    f"{self.settings.max_source_words:,} words.",
                )

            message = await self._insert_message(
                db,
                thread_id=thread.id,
                role="user",
                actor_user_id=requester_id,
                content=([{"type": "conversation", "text": text}] if text else []),
            )
            if attachment is not None:
                attachment_id = new_id()
                storage_name = f"{attachment_id}.docx"
                staged_files.append(
                    persist_attachment(
                        self.settings.attachments_dir, storage_name, attachment.content
                    )
                )
                db.add(
                    Attachment(
                        id=attachment_id,
                        message_id=message.id,
                        storage_name=storage_name,
                        original_name=attachment.original_name,
                        role=attachment_role,
                        media_type=DOCX_MEDIA_TYPE,
                        document_blocks=[block.to_model() for block in attachment.document_blocks],
                        byte_count=attachment.byte_count,
                        word_count=attachment.word_count,
                        sha256=attachment.sha256,
                    )
                )
                await db.flush()
            request_snapshot = await self._request_snapshot(db, thread, purpose="chat")
            db.add(
                Generation(
                    id=generation_id,
                    thread_id=thread.id,
                    requester_id=requester_id,
                    source_message_id=message.id,
                    client_request_id=client_request_id,
                    purpose="chat",
                    status="queued",
                    request_snapshot=request_snapshot,
                )
            )
            thread.updated_at = utc_now()
            await db.commit()
            staged_files.clear()
            # Schedule before any further await so the committed row is never seen
            # without its task (see _is_live).
            self._schedule(generation_id)
            thread_id = thread.id
        for orphan_id in finalized:
            await self._notify(orphan_id)
        return Submission(thread_id, generation_id)

    async def _check_response_budget(
        self,
        text: str,
        attachment: ValidatedAttachment | None,
        attachment_role: AttachmentRole,
    ) -> None:
        """Refuse new source material whose mandatory response cannot fit the cap.

        Transforming material means streaming the result as the deliverable plus one
        state copy (the text source, or the DOCX block map). The estimate is a lower
        bound: output as long as the input, NDJSON framing at the largest recommended
        delta size, and no reasoning. Anything above the output cap is certain to be cut
        off, so it is refused before any provider call instead of being billed.
        """

        if attachment is not None and attachment_role == "source":
            blocks = attachment.document_blocks
            deliverable = attachment.plain_text or "\n\n".join(block.text for block in blocks)
            state_copy = "\n\n".join(block.text for block in blocks)
            framing = _DOCX_BLOCK_FRAMING_TOKENS * len(blocks)
        elif text:
            deliverable = state_copy = text
            framing = 0
        else:
            return
        limit = self.settings.chat_max_completion_tokens
        framing += _STATE_FRAMING_TOKENS + _DELTA_FRAMING_TOKENS * (
            len(deliverable) // _DELTA_CHARS + 1
        )
        # Each token is at least one UTF-8 byte, so short material needs no counting.
        upper_bound = len(deliverable.encode()) + len(state_copy.encode()) + framing
        if upper_bound <= limit:
            return
        try:
            deliverable_tokens, state_tokens = await self.token_worker.run(
                partial(_count_pair, deliverable, state_copy)
            )
        except WorkerBusy as exc:
            raise GenerationError(
                "server_busy",
                "Oveo is busy measuring another document. Try again in a moment.",
                status_code=503,
            ) from exc
        required = deliverable_tokens + state_tokens + framing
        if required <= limit:
            return
        words = max(count_words(deliverable), 1)
        suggested = max(100, int(words * limit / required) // 100 * 100)
        raise GenerationError(
            "response_budget_exceeded",
            "This document is too long to process in one response: the complete result "
            f"needs about {required:,} output tokens and the limit is {limit:,}. Split it "
            f"into parts of about {suggested:,} words or fewer and send them one at a time.",
        )

    async def _resolve_submission_conflict(
        self,
        *,
        requester_id: str,
        client_request_id: str,
        thread_id: str | None,
        cause: IntegrityError,
    ) -> Submission:
        async with self.database.sessions() as db:
            existing = await self._existing_request(db, requester_id, client_request_id)
            if existing is not None and existing.thread_id is not None:
                # The same request already succeeded: answer it idempotently.
                return Submission(existing.thread_id, existing.id)
            if thread_id is not None:
                thread = await db.get(Thread, thread_id)
                if thread is None or thread.owner_id != requester_id:
                    raise GenerationError(
                        "thread_not_found", "Conversation not found.", status_code=404
                    ) from cause
                active = await db.scalar(
                    select(Generation.id).where(
                        Generation.thread_id == thread_id, Generation.status.in_(_ACTIVE)
                    )
                )
                if active is not None:
                    raise ActiveGenerationError from cause
        raise GenerationError(
            "submission_conflict",
            "The conversation changed while this message was being saved. Try again.",
            status_code=409,
        ) from cause

    async def _existing_request(
        self, db: AsyncSession, requester_id: str, client_request_id: str
    ) -> Generation | None:
        existing = await db.scalar(
            select(Generation).where(
                Generation.requester_id == requester_id,
                Generation.client_request_id == client_request_id,
            )
        )
        if existing is None:
            return None
        owner = await db.scalar(select(Thread.owner_id).where(Thread.id == existing.thread_id))
        if owner != requester_id:
            raise GenerationError("thread_not_found", "Conversation not found.", status_code=404)
        return existing

    @staticmethod
    async def _insert_message(
        db: AsyncSession,
        *,
        thread_id: str,
        role: str,
        actor_user_id: str | None,
        content: list[dict[str, Any]],
    ) -> Message:
        """Insert a message with its ordinal allocated inside the INSERT itself.

        A separate `max(ordinal)` read runs outside SQLite's write lock, so two writers
        could pick the same ordinal; the subquery is evaluated under the lock.
        """

        message = Message(
            id=new_id(),
            thread_id=thread_id,
            ordinal=(
                select(func.coalesce(func.max(Message.ordinal), 0) + 1)
                .where(Message.thread_id == thread_id)
                .scalar_subquery()
            ),
            role=role,
            actor_user_id=actor_user_id,
            content=content,
        )
        db.add(message)
        await db.flush()
        await db.refresh(message, ["ordinal"])
        return message

    async def _finalize_orphans(self, db: AsyncSession, thread_id: str) -> list[str]:
        """Finish active rows of this thread that no live task owns (same transaction).

        A failed terminal write can leave a row active after its task ended. Without a
        task nothing would ever finish it, so the thread would reject every new turn.
        """

        rows = (
            await db.execute(
                select(Generation.id, Generation.status).where(
                    Generation.thread_id == thread_id, Generation.status.in_(_ACTIVE)
                )
            )
        ).all()
        finalized: list[str] = []
        for generation_id, status in rows:
            if self._is_live(generation_id):
                continue
            if status == "stopping":
                done = await self._transition(
                    db,
                    generation_id,
                    allowed=("stopping",),
                    status="stopped",
                    error_code=None,
                    partial_blocks=[],
                )
            else:
                done = await self._transition(
                    db,
                    generation_id,
                    allowed=("queued", "running"),
                    status="failed",
                    error_code="generation_interrupted",
                    partial_blocks=[],
                )
            if done:
                finalized.append(generation_id)
        return finalized

    async def submit_handoff(
        self,
        *,
        thread_id: str,
        requester_id: str,
        client_request_id: str,
    ) -> str:
        generation_id = new_id()
        self._starting.add(generation_id)
        finalized: list[str] = []
        try:
            try:
                async with self.database.sessions() as db:
                    existing = await self._existing_request(db, requester_id, client_request_id)
                    if existing is not None:
                        return existing.id
                    thread = await db.get(Thread, thread_id)
                    if thread is None or thread.owner_id != requester_id:
                        raise GenerationError(
                            "thread_not_found", "Conversation not found.", status_code=404
                        )
                    finalized = await self._finalize_orphans(db, thread.id)
                    request_snapshot = await self._request_snapshot(
                        db, thread, purpose="prompt_handoff"
                    )
                    db.add(
                        Generation(
                            id=generation_id,
                            thread_id=thread.id,
                            requester_id=requester_id,
                            client_request_id=client_request_id,
                            purpose="prompt_handoff",
                            status="queued",
                            request_snapshot=request_snapshot,
                        )
                    )
                    await db.commit()
                    self._schedule(generation_id)
            except IntegrityError as exc:
                async with self.database.sessions() as db:
                    existing = await self._existing_request(db, requester_id, client_request_id)
                    if existing is not None:
                        return existing.id
                raise ActiveGenerationError from exc
        finally:
            self._starting.discard(generation_id)
        for orphan_id in finalized:
            await self._notify(orphan_id)
        return generation_id

    async def retry(
        self,
        *,
        generation_id: str,
        requester_id: str,
        client_request_id: str,
    ) -> str:
        not_retryable = GenerationError(
            "generation_not_retryable",
            "This generation cannot be retried.",
            status_code=409,
        )
        retry_id = new_id()
        self._starting.add(retry_id)
        finalized: list[str] = []
        try:
            try:
                async with self.database.sessions() as db:
                    existing = await db.scalar(
                        select(Generation).where(
                            Generation.requester_id == requester_id,
                            Generation.client_request_id == client_request_id,
                        )
                    )
                    if existing is not None:
                        existing_owner = await db.scalar(
                            select(Thread.owner_id).where(Thread.id == existing.thread_id)
                        )
                        if existing_owner != requester_id:
                            raise not_retryable
                        return existing.id
                    original = await db.get(Generation, generation_id)
                    if original is None or original.thread_id is None:
                        raise not_retryable
                    thread = await db.get(Thread, original.thread_id)
                    if thread is None:
                        raise GenerationError(
                            "thread_not_found", "Conversation not found.", status_code=404
                        )
                    if thread.owner_id != requester_id or not await self._is_retryable(
                        db, original
                    ):
                        raise not_retryable
                    finalized = await self._finalize_orphans(db, thread.id)
                    request_snapshot = await self._request_snapshot(
                        db, thread, purpose=original.purpose
                    )
                    db.add(
                        Generation(
                            id=retry_id,
                            thread_id=original.thread_id,
                            requester_id=requester_id,
                            source_message_id=original.source_message_id,
                            retry_of_generation_id=original.id,
                            client_request_id=client_request_id,
                            purpose=original.purpose,
                            status="queued",
                            request_snapshot=request_snapshot,
                        )
                    )
                    await db.commit()
                    self._schedule(retry_id)
            except IntegrityError as exc:
                async with self.database.sessions() as db:
                    existing = await self._existing_request(db, requester_id, client_request_id)
                    if existing is not None:
                        return existing.id
                raise ActiveGenerationError from exc
        finally:
            self._starting.discard(retry_id)
        for orphan_id in finalized:
            await self._notify(orphan_id)
        return retry_id

    async def _is_retryable(self, db: AsyncSession, generation: Generation) -> bool:
        """Only the latest chat turn without an answer can be retried.

        Retrying an older or already answered generation would append a second answer
        to a turn that has one.
        """

        if (
            generation.purpose != "chat"
            or generation.status not in {"failed", "stopped"}
            or generation.result_message_id is not None
            or generation.thread_id is None
            or generation.source_message_id is None
        ):
            return False
        latest_chat = await db.scalar(
            select(Generation.id)
            .where(Generation.thread_id == generation.thread_id, Generation.purpose == "chat")
            .order_by(Generation.created_at.desc(), Generation.id.desc())
            .limit(1)
        )
        if latest_chat != generation.id:
            return False
        source_ordinal = await db.scalar(
            select(Message.ordinal).where(Message.id == generation.source_message_id)
        )
        if source_ordinal is None:
            return False
        answered = await db.scalar(
            select(Message.id)
            .where(
                Message.thread_id == generation.thread_id,
                Message.role == "assistant",
                Message.ordinal > source_ordinal,
            )
            .limit(1)
        )
        return answered is None

    async def stop(self, generation_id: str) -> None:
        async with self.database.sessions() as db:
            requested = await self._transition(
                db, generation_id, allowed=("queued", "running"), status="stopping"
            )
            await db.commit()
            if not requested:
                status = await db.scalar(
                    select(Generation.status).where(Generation.id == generation_id)
                )
                if status is None:
                    raise GenerationError(
                        "generation_not_found", "Generation not found.", status_code=404
                    )
                if status != "stopping":
                    # Already finished: a late Stop must not reopen or relabel it.
                    return
        await self._notify(generation_id)
        if not self._is_live(generation_id):
            # No task will ever finish this row (see _is_live), so finish it now.
            await self._finish_stopped(generation_id)
            return
        cancel_event = self._cancel.get(generation_id)
        if cancel_event is not None:
            cancel_event.set()
        task = self._tasks.get(generation_id)
        if task is not None:
            task.cancel()

    async def cancel_thread(self, thread_id: str) -> None:
        async with self.database.sessions() as db:
            ids = tuple(
                (
                    await db.execute(
                        select(Generation.id).where(
                            Generation.thread_id == thread_id,
                            Generation.status.in_(_ACTIVE),
                        )
                    )
                ).scalars()
            )
        tasks: list[asyncio.Task[None]] = []
        for generation_id in ids:
            task = self._tasks.get(generation_id)
            await self.stop(generation_id)
            if task is not None:
                tasks.append(task)
        if tasks:
            await asyncio.wait(tasks, timeout=_CANCEL_WAIT_SECONDS)

    async def get_snapshot(self, generation_id: str) -> dict[str, Any] | None:
        async with self.database.sessions() as db:
            generation = await db.get(Generation, generation_id)
            if generation is None:
                return None
            blocks = (
                generation.partial_blocks if isinstance(generation.partial_blocks, list) else []
            )
            return {
                "id": generation.id,
                "thread_id": generation.thread_id,
                "status": generation.status,
                "blocks": blocks,
                "error_code": generation.error_code,
                "error_message": _ERROR_MESSAGES.get(generation.error_code or ""),
                "retryable": await self._is_retryable(db, generation),
                "seq": generation.stream_revision,
            }

    async def events(self, generation_id: str) -> AsyncIterator[str]:
        last_seq = -1
        while True:
            snapshot = await self.get_snapshot(generation_id)
            if snapshot is None:
                return
            seq = int(snapshot["seq"])
            if seq != last_seq:
                yield f"id: {seq}\ndata: {json.dumps(snapshot, separators=(',', ':'))}\n\n"
                last_seq = seq
            if snapshot["status"] in _TERMINAL:
                return
            condition = self._conditions.setdefault(generation_id, asyncio.Condition())
            try:
                async with condition:
                    await asyncio.wait_for(condition.wait(), timeout=15)
            except TimeoutError:
                yield ": keep-alive\n\n"

    async def _notify(self, generation_id: str) -> None:
        condition = self._conditions.setdefault(generation_id, asyncio.Condition())
        async with condition:
            condition.notify_all()

    async def _set_running(self, generation_id: str) -> Generation | None:
        async with self.database.sessions() as db:
            if not await self._transition(
                db, generation_id, allowed=("queued",), status="running", started_at=utc_now()
            ):
                return None
            generation = await db.get(Generation, generation_id)
            if generation is None:  # pragma: no cover - the update just matched this row
                return None
            await append_usage_event(
                db,
                dedupe_key=f"{generation.id}:pending",
                event_type="pending",
                purpose=generation.purpose,
                amount_microusd=None,
                generation_id=generation.id,
                thread_id=generation.thread_id,
                requester_id=generation.requester_id,
                provider_request_id=None,
            )
            await db.commit()
            return generation

    async def _record_events(self, generation_id: str, events: Sequence[ProtocolEvent]) -> None:
        if not events:
            return
        async with self.database.sessions() as db:
            generation = await db.get(Generation, generation_id)
            if generation is None or generation.status != "running":
                raise asyncio.CancelledError
            blocks = [dict(block) for block in generation.partial_blocks]
            for event in events:
                if event.event == "block_start":
                    blocks.append({"type": event.block_type, "text": ""})
                elif event.event == "block_delta":
                    if not blocks or event.text is None:
                        raise ProtocolError("delta_without_active_block")
                    blocks[-1]["text"] = f"{blocks[-1].get('text', '')}{event.text}"
            result = await db.execute(
                update(Generation)
                .where(Generation.id == generation_id, Generation.status == "running")
                .values(partial_blocks=blocks, stream_revision=Generation.stream_revision + 1)
                .execution_options(synchronize_session=False)
            )
            if int(result.rowcount or 0) != 1:  # type: ignore[attr-defined]
                raise asyncio.CancelledError
            await db.commit()
        await self._notify(generation_id)

    async def _commit_success(
        self,
        generation_id: str,
        document: ProtocolDocument,
        completion: ProviderCompletion,
    ) -> None:
        mutates_state = not isinstance(document.state, NoState)
        templates: Mapping[str, ExtractedDocx] = {}
        if mutates_state:
            # Parse any DOCX template before the write transaction so the database write
            # lock is never held while the CPU-bound parse runs.
            try:
                templates = await self._prepare_docx_templates(generation_id, document)
            except ProtocolError as exc:
                if exc.code == "state_docx_template_outdated":
                    raise DocxTemplateOutdatedError from exc
                raise StatePersistenceError from exc
        try:
            # The completion claim is compare-and-set, so a retry after a failed write can
            # never commit the answer twice.
            claimed = await self._with_write_retries(
                partial(
                    self._commit_success_once,
                    generation_id,
                    document,
                    completion,
                    templates,
                )
            )
        except SQLAlchemyError as exc:
            if mutates_state:
                raise StatePersistenceError from exc
            raise
        if not claimed:
            # Stop won the race before the answer was committed: finish the stop.
            await self._finish_stopped(generation_id)
            return
        await self._notify(generation_id)

    async def _commit_success_once(
        self,
        generation_id: str,
        document: ProtocolDocument,
        completion: ProviderCompletion,
        templates: Mapping[str, ExtractedDocx],
    ) -> bool:
        blocks = cast(list[dict[str, str]], document.to_storage()["blocks"])
        mutates_state = not isinstance(document.state, NoState)
        async with self.database.sessions() as db:
            # Claim `running -> completed` first. This takes the write lock, and if a Stop
            # already moved the row to `stopping` nothing is written: the answer is never
            # committed under a stopped generation, and a later Stop cannot relabel it.
            provider_ids: dict[str, Any] = {}
            if completion.provider_request_id is not None:
                provider_ids["provider_request_id"] = completion.provider_request_id
            if completion.provider_generation_id is not None:
                provider_ids["provider_generation_id"] = completion.provider_generation_id
            if not await self._transition(
                db,
                generation_id,
                allowed=("running",),
                status="completed",
                partial_blocks=blocks,
                **provider_ids,
            ):
                return False
            generation = await db.get(Generation, generation_id)
            if generation is None:  # pragma: no cover - the update just matched this row
                return False
            if generation.purpose != "chat" and mutates_state:
                raise ProtocolError("state_invalid_purpose")
            if generation.purpose == "prompt_handoff" and (
                len(document.blocks) != 1
                or document.blocks[0].type != "deliverable"
                or HANDOFF_SENTINEL in document.blocks[0].text
            ):
                raise ProtocolError("invalid_handoff_response")
            if generation.purpose == "chat" and generation.thread_id is not None:
                thread = await db.get(Thread, generation.thread_id)
                if thread is None:
                    raise ProtocolError("state_thread_missing")
                message = await self._insert_message(
                    db,
                    thread_id=thread.id,
                    role="assistant",
                    actor_user_id=None,
                    content=blocks,
                )
                try:
                    await self._apply_state_operation(
                        db,
                        generation=generation,
                        thread=thread,
                        document=document,
                        templates=templates,
                    )
                except ProtocolError as exc:
                    if exc.code in _REPAIRABLE_STATE_CODES:
                        raise
                    if exc.code in {"state_base_missing", "state_base_mismatch"}:
                        raise StaleStateError from exc
                    raise StatePersistenceError from exc
                await db.execute(
                    update(Generation)
                    .where(Generation.id == generation_id)
                    .values(result_message_id=message.id)
                    .execution_options(synchronize_session=False)
                )
                thread.updated_at = utc_now()
            await db.commit()
        return True

    async def _apply_state_operation(
        self,
        db: AsyncSession,
        *,
        generation: Generation,
        thread: Thread | None,
        document: ProtocolDocument,
        templates: Mapping[str, ExtractedDocx] | None = None,
    ) -> None:
        """Validate and persist one hidden canonical mutation in the response commit.

        ``templates`` holds DOCX templates already parsed by `_prepare_docx_templates`.
        """

        state = document.state
        if thread is None:
            raise ProtocolError("state_thread_missing")
        if isinstance(state, NoState):
            return
        # The single visible deliverable is the canonical output (or append addition);
        # the protocol no longer asks the model to repeat it in the state event.
        visible = _single_deliverable(document)

        item = await db.scalar(
            select(WorkItem).where(
                WorkItem.thread_id == thread.id,
                WorkItem.active.is_(True),
            )
        )
        current: WorkVersion | None = None
        if item is not None:
            current = await db.scalar(
                select(WorkVersion)
                .where(WorkVersion.work_item_id == item.id)
                .order_by(WorkVersion.version_no.desc())
                .limit(1)
            )

        source_attachment = None
        if generation.source_message_id is not None:
            source_attachment = await db.scalar(
                select(Attachment).where(Attachment.message_id == generation.source_message_id)
            )
        source_docx = (
            source_attachment
            if source_attachment is not None
            and source_attachment.role == "source"
            and source_attachment.media_type == DOCX_MEDIA_TYPE
            else None
        )
        docx_template_attachment_id: str | None = None
        docx_blocks: list[dict[str, str]] | None = None
        template_blocks = None
        state_docx_blocks = getattr(state, "docx_blocks", None)

        if isinstance(state, EstablishState):
            if item is not None:
                item.active = False
            item = WorkItem(
                thread_id=thread.id,
                kind={
                    "translate": "translation",
                    "revision": "revision",
                    "internal_comms": "draft",
                }[thread.mode],
                active=True,
            )
            db.add(item)
            await db.flush()
            output = visible
            brief = state.brief
            version_no = 1
            parent_version_id = None
            operation = "establish"
            if source_docx is not None:
                if state_docx_blocks is None:
                    raise ProtocolError("state_docx_blocks_missing")
                extracted = _prepared_template(templates, source_docx.id)
                # The upload is the source. A redundant copy is accepted only if exact.
                if state.source is not None and state.source != extracted.plain_text:
                    raise ProtocolError("state_docx_source_mismatch")
                source = extracted.plain_text
                docx_template_attachment_id = source_docx.id
                template_blocks = extracted.blocks
            else:
                if state_docx_blocks is not None:
                    raise ProtocolError("state_docx_template_missing")
                if state.source is None:
                    raise ProtocolError("state_source_missing")
                source = state.source
        else:
            if item is None or current is None:
                raise ProtocolError("state_base_missing")
            if state.base_version != current.version_no:
                raise ProtocolError("state_base_mismatch")
            source = current.source_text
            output = current.output_text
            brief = current.brief
            version_no = current.version_no + 1
            parent_version_id = current.id
            operation = state.operation

            current_has_docx = current.docx_template_attachment_id is not None
            if current_has_docx != (current.docx_blocks is not None):
                raise ProtocolError("state_docx_base_invalid")
            if source_docx is not None:
                # A new uploaded Word document establishes a new immutable template;
                # it cannot silently replace the template of an existing work item.
                raise ProtocolError("state_docx_requires_establish")
            if current_has_docx:
                if isinstance(state, AppendState):
                    raise ProtocolError("state_docx_append_unsupported")
                if state_docx_blocks is None:
                    raise ProtocolError("state_docx_blocks_missing")
                template_attachment = await db.get(Attachment, current.docx_template_attachment_id)
                if template_attachment is None:
                    raise ProtocolError("state_docx_template_missing")
                extracted = _prepared_template(templates, template_attachment.id)
                docx_template_attachment_id = template_attachment.id
                template_blocks = extracted.blocks
            elif state_docx_blocks is not None:
                raise ProtocolError("state_docx_template_missing")

            if isinstance(state, AppendState):
                source = _append_text(source, state.source_addition, state.source_separator)
                # Normally the deliverable is exactly the addition. When the user asked for
                # the whole work, the state names the addition and the deliverable must be
                # the complete joined output.
                if state.output_addition is not None:
                    addition = state.output_addition
                elif visible.startswith(_append_text(output, "", state.output_separator)):
                    # A complete joined document without the named addition would be
                    # appended to itself.
                    raise ProtocolError("state_output_addition_missing")
                else:
                    addition = visible
                output = _append_text(output, addition, state.output_separator)
                if visible not in {addition, output}:
                    raise ProtocolError("state_deliverable_mismatch")
                brief = state.brief if state.brief is not None else brief
            elif isinstance(state, ReplaceState):
                source_replacements: list[tuple[str, str]] = []
                output_replacements: list[tuple[str, str]] = []
                for replacement in state.replacements:
                    if isinstance(replacement, SourceOutputReplacement):
                        source_replacements.append(
                            (replacement.source_anchor, replacement.source_replacement)
                        )
                    elif not isinstance(replacement, OutputReplacement):
                        raise ProtocolError("invalid_replacement")
                    output_replacements.append(
                        (replacement.output_anchor, replacement.output_replacement)
                    )
                source = _apply_base_replacements(source, source_replacements, label="source")
                output = _apply_base_replacements(output, output_replacements, label="output")
                if visible != output:
                    raise ProtocolError("state_deliverable_mismatch")
                brief = state.brief if state.brief is not None else brief
            elif isinstance(state, FullState):
                source = state.source if state.source is not None else source
                output = visible
                brief = state.brief if state.brief is not None else brief
            else:  # pragma: no cover - kept defensive for future protocol variants
                raise ProtocolError("invalid_state_operation")

        if template_blocks is not None:
            assert state_docx_blocks is not None
            replacements = tuple(
                DocxReplacement(id=block.id, text=block.text) for block in state_docx_blocks
            )
            try:
                docx_output = plain_text_from_replacements(template_blocks, replacements)
            except DocxError as exc:
                raise ProtocolError(exc.code) from exc
            if output != docx_output:
                raise ProtocolError("state_docx_output_mismatch")
            docx_blocks = [block.to_storage() for block in replacements]

        if not source.strip() or not output.strip() or not brief:
            raise ProtocolError("invalid_canonical_state")
        source_word_count = count_words(source)
        if source_word_count > self.settings.max_source_words:
            raise ProtocolError("state_source_word_limit")
        db.add(
            WorkVersion(
                work_item_id=item.id,
                version_no=version_no,
                parent_version_id=parent_version_id,
                cause_message_id=generation.source_message_id,
                operation=operation,
                source_text=source,
                output_text=output,
                source_word_count=source_word_count,
                brief=brief,
                docx_template_attachment_id=docx_template_attachment_id,
                docx_blocks=docx_blocks,
            )
        )
        await db.flush()

    async def _prepare_docx_templates(
        self, generation_id: str, document: ProtocolDocument
    ) -> dict[str, ExtractedDocx]:
        """Parse the DOCX template a mutation needs, outside any write transaction."""

        async with self.database.sessions() as db:
            generation = await db.get(Generation, generation_id)
            if generation is None or generation.purpose != "chat" or generation.thread_id is None:
                return {}
            attachment: Attachment | None
            if isinstance(document.state, EstablishState):
                if generation.source_message_id is None:
                    return {}
                attachment = await db.scalar(
                    select(Attachment).where(Attachment.message_id == generation.source_message_id)
                )
                if attachment is None or attachment.role != "source":
                    return {}
            else:
                current = await db.scalar(
                    select(WorkVersion)
                    .join(WorkItem, WorkVersion.work_item_id == WorkItem.id)
                    .where(WorkItem.thread_id == generation.thread_id, WorkItem.active.is_(True))
                    .order_by(WorkVersion.version_no.desc())
                    .limit(1)
                )
                if current is None or current.docx_template_attachment_id is None:
                    return {}
                attachment = await db.get(Attachment, current.docx_template_attachment_id)
                if attachment is None:
                    raise ProtocolError("state_docx_template_missing")
            template = _TemplateRef(
                id=attachment.id,
                storage_name=attachment.storage_name,
                media_type=attachment.media_type,
                byte_count=attachment.byte_count,
                sha256=attachment.sha256,
                stored_blocks=attachment.document_blocks,
            )
        extracted = await self.docx_worker.run(
            partial(self._load_docx_template, template), wait=True
        )
        return {template.id: extracted}

    def _load_docx_template(self, template: _TemplateRef) -> ExtractedDocx:
        """Read, verify and parse one template (runs on the DOCX worker thread)."""

        if template.media_type != DOCX_MEDIA_TYPE:
            raise ProtocolError("state_docx_template_invalid")
        root = self.settings.attachments_dir.resolve()
        path = (root / template.storage_name).resolve()
        if path.parent != root:
            raise ProtocolError("state_docx_template_invalid")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ProtocolError("state_docx_template_missing") from exc
        if len(content) != template.byte_count or hashlib.sha256(content).hexdigest() != (
            template.sha256
        ):
            raise ProtocolError("state_docx_template_invalid")
        try:
            extracted = extract_docx(
                content,
                max_uncompressed_bytes=docx_uncompressed_limit(self.settings.max_upload_bytes),
            )
            # The model worked from the stored block map; it must still describe this
            # package exactly (maps from the earlier text-box-unsafe extractor do not).
            require_matching_blocks(extracted.blocks, template.stored_blocks)
        except DocxError as exc:
            if exc.code == "docx_template_outdated":
                raise ProtocolError("state_docx_template_outdated") from exc
            raise ProtocolError("state_docx_template_invalid") from exc
        return extracted

    async def _call_provider(
        self,
        usage: _UsageContext,
        request: ProviderRequest,
        emit: EmitChunk,
        cancel_event: asyncio.Event,
        *,
        ledger_purpose: str,
        dedupe_scope: str,
        record_ids: bool = False,
    ) -> ProviderCompletion:
        """Run one provider call within the global concurrency cap and account for it.

        The provider generation id is stored as a reconcilable `pending` row as soon as
        the first chunk names it, so a call that is stopped, cut off, or fails protocol
        validation mid-stream can still be charged from OpenRouter's metadata later.
        """

        observed: dict[str, str | None] = {}

        async def on_provider_ids(request_id: str | None, provider_generation_id: str) -> None:
            observed["request_id"] = request_id
            observed["generation_id"] = provider_generation_id
            try:
                await self._record_provider_ids(
                    usage,
                    purpose=ledger_purpose,
                    request_id=request_id,
                    provider_generation_id=provider_generation_id,
                    record_ids=record_ids,
                )
            except SQLAlchemyError as error:
                # Best effort: bookkeeping must not abort a response that is streaming.
                log_unexpected(_LOGGER, error, area="usage_ids")

        async with self._provider_slots:
            try:
                completion = await self.provider.generate(
                    replace(request, on_provider_ids=on_provider_ids), emit, cancel_event
                )
            except ProviderError as error:
                await self._record_provider_error(
                    usage, error, purpose=ledger_purpose, scope=dedupe_scope, observed=observed
                )
                raise
        await self._record_provider_completion(
            usage, completion, purpose=ledger_purpose, scope=dedupe_scope, observed=observed
        )
        return completion

    async def _record_provider_ids(
        self,
        usage: _UsageContext,
        *,
        purpose: str,
        request_id: str | None,
        provider_generation_id: str,
        record_ids: bool,
    ) -> None:
        async with self.database.sessions() as db:
            await append_usage_event(
                db,
                dedupe_key=f"{provider_generation_id}:{purpose}:reconcile-pending",
                event_type="pending",
                purpose=purpose,
                amount_microusd=None,
                generation_id=usage.generation_id,
                thread_id=usage.thread_id,
                requester_id=usage.requester_id,
                provider_request_id=request_id,
                provider_generation_id=provider_generation_id,
            )
            if record_ids:
                await db.execute(
                    update(Generation)
                    .where(Generation.id == usage.generation_id)
                    .values(
                        provider_request_id=request_id,
                        provider_generation_id=provider_generation_id,
                    )
                    .execution_options(synchronize_session=False)
                )
            await db.commit()

    async def _record_provider_completion(
        self,
        usage: _UsageContext,
        completion: ProviderCompletion,
        *,
        purpose: str,
        scope: str,
        observed: Mapping[str, str | None],
    ) -> None:
        await self._record_usage(
            usage,
            purpose=purpose,
            scope=scope,
            suffix="charge",
            cost_microusd=completion.cost_microusd,
            request_id=completion.provider_request_id or observed.get("request_id"),
            provider_generation_id=(
                completion.provider_generation_id or observed.get("generation_id")
            ),
        )

    async def _record_provider_error(
        self,
        usage: _UsageContext,
        error: ProviderError,
        *,
        purpose: str,
        scope: str,
        observed: Mapping[str, str | None] | None = None,
    ) -> None:
        seen = observed or {}
        await self._record_usage(
            usage,
            purpose=purpose,
            scope=scope,
            suffix="failed-charge",
            cost_microusd=error.cost_microusd,
            request_id=error.provider_request_id or seen.get("request_id"),
            provider_generation_id=error.provider_generation_id or seen.get("generation_id"),
        )

    async def _record_usage(
        self,
        usage: _UsageContext,
        *,
        purpose: str,
        scope: str,
        suffix: str,
        cost_microusd: int | None,
        request_id: str | None,
        provider_generation_id: str | None,
    ) -> None:
        if cost_microusd is None and provider_generation_id is None:
            return
        async with self.database.sessions() as db:
            if cost_microusd is not None:
                # Keyed by the provider call, so a call is charged exactly once.
                call_key = provider_generation_id or request_id or scope
                await append_usage_event(
                    db,
                    dedupe_key=f"{call_key}:{purpose}:{suffix}",
                    event_type="charge",
                    purpose=purpose,
                    amount_microusd=cost_microusd,
                    generation_id=usage.generation_id,
                    thread_id=usage.thread_id,
                    requester_id=usage.requester_id,
                    provider_request_id=request_id,
                    provider_generation_id=provider_generation_id,
                )
            else:
                await append_usage_event(
                    db,
                    dedupe_key=f"{provider_generation_id}:{purpose}:reconcile-pending",
                    event_type="pending",
                    purpose=purpose,
                    amount_microusd=None,
                    generation_id=usage.generation_id,
                    thread_id=usage.thread_id,
                    requester_id=usage.requester_id,
                    provider_request_id=request_id,
                    provider_generation_id=provider_generation_id,
                )
            await db.commit()

    async def _maybe_generate_title(self, usage: _UsageContext, thread_id: str) -> None:
        """Generate the first title as a non-transcript, separately accounted model call."""

        generation_id = usage.generation_id
        async with self.database.sessions() as db:
            thread = await db.get(Thread, thread_id)
            generation = await db.get(Generation, generation_id)
            if (
                thread is None
                or generation is None
                or thread.title != "New conversation"
                or generation.status != "completed"
            ):
                return
            message_count = int(
                await db.scalar(
                    select(func.count()).select_from(Message).where(Message.thread_id == thread.id)
                )
                or 0
            )
            if message_count != 2:
                return
            snapshot = await self._title_snapshot(db, thread)
            if snapshot is None:
                return
            await append_usage_event(
                db,
                dedupe_key=f"{generation.id}:title:pending",
                event_type="pending",
                purpose="title",
                amount_microusd=None,
                generation_id=generation.id,
                thread_id=thread.id,
                requester_id=generation.requester_id,
                provider_request_id=None,
            )
            await db.commit()

        parts: list[bytes] = []

        async def collect(chunk: bytes) -> None:
            if sum(map(len, parts)) + len(chunk) > 1_024:
                raise ProviderError("title_too_large")
            parts.append(chunk)

        request = ProviderRequest(
            generation_id=f"{generation_id}:title",
            purpose="title",
            mode=str(snapshot.get("mode", "translate")),
            snapshot=snapshot,
        )
        try:
            await self._call_provider(
                usage,
                request,
                collect,
                asyncio.Event(),
                ledger_purpose="title",
                dedupe_scope=request.generation_id,
            )
            title = b"".join(parts).decode("utf-8").strip()
            if not title or len(title) > 60 or "\n" in title or not 2 <= len(title.split()) <= 6:
                return
            async with self.database.sessions() as db:
                thread = await db.get(Thread, thread_id)
                if thread is None:
                    return
                if thread.title == "New conversation":
                    thread.title = title
                    thread.updated_at = utc_now()
                await db.commit()
        except ProviderError:
            return  # A title is optional; its usage was already recorded.
        except (UnicodeDecodeError, ValueError):
            return

    async def _title_snapshot(self, db: AsyncSession, thread: Thread) -> dict[str, Any] | None:
        """Build a title request from the first user request only, bounded in size.

        The full transcript, canonical work and block maps are not needed for a 2-6 word
        title; an attachment-only first request contributes its opening text instead.
        """

        first = await db.scalar(
            select(Message)
            .where(Message.thread_id == thread.id, Message.role == "user")
            .order_by(Message.ordinal)
            .limit(1)
        )
        if first is None or first.actor_user_id is None:
            return None
        request_text = "\n".join(
            str(block.get("text", "")) for block in first.content if isinstance(block, dict)
        ).strip()
        attachments: dict[str, AttachmentDocument] = {}
        if len(request_text) < _TITLE_INPUT_CHARS:
            attachment = await db.scalar(
                select(Attachment).where(Attachment.message_id == first.id)
            )
            if attachment is not None:
                remaining = _TITLE_INPUT_CHARS - len(request_text)
                opening: list[DocxBlock] = []
                try:
                    stored = docx_blocks_from_storage(attachment.document_blocks)
                except DocxError:
                    stored = ()
                for block in stored:
                    if remaining <= 0:
                        break
                    opening.append(
                        DocxBlock(id=block.id, kind=block.kind, text=block.text[:remaining])
                    )
                    remaining -= len(block.text)
                attachments[first.id] = AttachmentDocument(
                    word_count=attachment.word_count,
                    document_blocks=tuple(opening),
                    role=cast(AttachmentRole, attachment.role),
                )
        bounded = Message(
            id=first.id,
            thread_id=thread.id,
            ordinal=first.ordinal,
            role="user",
            actor_user_id=first.actor_user_id,
            content=(
                [{"type": "conversation", "text": request_text[:_TITLE_INPUT_CHARS]}]
                if request_text
                else []
            ),
        )
        actor = await db.scalar(select(User.display_name).where(User.id == first.actor_user_id))
        provider_messages = build_provider_messages(
            thread,
            purpose="title",
            recent_messages=[bounded],
            actor_labels={first.actor_user_id: actor or "User"},
            attachments=attachments,
        )
        return {
            "schema_version": 1,
            "mode": thread.mode,
            "provider_messages": [
                {"role": message.role, "content": message.content} for message in provider_messages
            ],
        }

    async def _finish_stopped(self, generation_id: str) -> None:
        async def write() -> None:
            async with self.database.sessions() as db:
                await self._transition(
                    db,
                    generation_id,
                    allowed=tuple(_ACTIVE),
                    status="stopped",
                    error_code=None,
                    partial_blocks=[],
                )
                await db.commit()

        await self._with_write_retries(write)
        await self._notify(generation_id)

    async def _maybe_compact_context(self, generation: Generation) -> Mapping[str, Any]:
        snapshot = generation.request_snapshot
        if generation.purpose not in {"chat", "prompt_handoff"}:
            return snapshot
        if await self._snapshot_tokens(snapshot) <= self.settings.context_compaction_tokens:
            return snapshot
        if generation.thread_id is None:
            raise ProviderError("context_compaction_failed")
        if generation.purpose == "prompt_handoff":
            return await self._compact_handoff_context(generation)
        return await self._compact_chat_context(generation)

    async def _compact_chat_context(self, generation: Generation) -> Mapping[str, Any]:
        snapshot: Mapping[str, Any] = generation.request_snapshot
        budget = self.settings.context_compaction_tokens
        await self._require_compactable_floor(generation, budget)
        while await self._snapshot_tokens(snapshot) > budget:
            async with self.database.sessions() as db:
                thread = await db.get(Thread, generation.thread_id)
                if thread is None:
                    raise ProviderError("context_compaction_failed")
                unsummarized = list(
                    (
                        await db.execute(
                            select(Message)
                            .where(
                                Message.thread_id == thread.id,
                                Message.ordinal > (thread.summary_through_ordinal or 0),
                            )
                            .order_by(Message.ordinal)
                        )
                    ).scalars()
                )
                if len(unsummarized) <= _MIN_RECENT_MESSAGES:
                    raise ProviderError("context_compaction_failed")

                retain = (
                    self.settings.context_recent_messages
                    if len(unsummarized) > self.settings.context_recent_messages
                    else _MIN_RECENT_MESSAGES
                )
                candidates = unsummarized[:-retain]
                bounded = await self._largest_bounded_summary_snapshot(db, thread, candidates)
                if bounded is None:
                    raise ProviderError("context_compaction_failed")
                cutoff, summary_snapshot = bounded
                await append_usage_event(
                    db,
                    dedupe_key=f"{generation.id}:summary:{cutoff}:pending",
                    event_type="pending",
                    purpose="summary",
                    amount_microusd=None,
                    generation_id=generation.id,
                    thread_id=thread.id,
                    requester_id=generation.requester_id,
                    provider_request_id=None,
                )
                await db.commit()

            compacted = await self._generate_context_summary(
                generation,
                cutoff=cutoff,
                summary_snapshot=summary_snapshot,
            )

            async with self.database.sessions() as db:
                thread = await db.get(Thread, generation.thread_id)
                current = await db.get(Generation, generation.id)
                if thread is None or current is None or current.status != "running":
                    raise asyncio.CancelledError
                thread.context_summary = compacted
                thread.summary_through_ordinal = cutoff
                fresh_snapshot = await self._request_snapshot(db, thread, purpose="chat")
                current.request_snapshot = fresh_snapshot
                await db.commit()
                snapshot = fresh_snapshot
        return snapshot

    async def _require_compactable_floor(self, generation: Generation, budget: int) -> None:
        """Fail before any summary call when compaction cannot possibly help.

        Summaries only replace older transcript turns. The prompts, active canonical
        work, the pinned reference document and the latest turns always remain, so if
        they alone exceed the budget every summary call would be wasted.
        """

        async with self.database.sessions() as db:
            thread = await db.get(Thread, generation.thread_id)
            if thread is None:
                raise ProviderError("context_compaction_failed")
            recent = list(
                (
                    await db.execute(
                        select(Message.ordinal)
                        .where(
                            Message.thread_id == thread.id,
                            Message.ordinal > (thread.summary_through_ordinal or 0),
                        )
                        .order_by(Message.ordinal.desc())
                        .limit(_MIN_RECENT_MESSAGES)
                    )
                ).scalars()
            )
            floor_after = min(recent) - 1 if recent else thread.summary_through_ordinal
            floor = await self._request_snapshot(
                db, thread, purpose="chat", after_ordinal=floor_after
            )
        if await self._snapshot_tokens(floor) > budget:
            raise ProviderError("context_budget_exceeded")

    async def _largest_bounded_summary_snapshot(
        self,
        db: AsyncSession,
        thread: Thread,
        candidates: Sequence[Message],
    ) -> tuple[int, Mapping[str, Any]] | None:
        low = 0
        high = len(candidates) - 1
        best: tuple[int, Mapping[str, Any]] | None = None
        while low <= high:
            middle = (low + high) // 2
            cutoff = candidates[middle].ordinal
            candidate = await self._request_snapshot(
                db,
                thread,
                purpose="summary",
                through_ordinal=cutoff,
            )
            if await self._snapshot_tokens(candidate) <= self.settings.context_compaction_tokens:
                best = (cutoff, candidate)
                low = middle + 1
            else:
                high = middle - 1
        return best

    async def _generate_context_summary(
        self,
        generation: Generation,
        *,
        cutoff: int,
        summary_snapshot: Mapping[str, Any],
    ) -> str:
        def bounded_collector(parts: list[bytes]) -> EmitChunk:
            async def collect(chunk: bytes) -> None:
                if sum(map(len, parts)) + len(chunk) > 131_072:
                    raise ProviderError("context_compaction_failed")
                parts.append(chunk)

            return collect

        for attempt in range(1, 3):
            if attempt == 2:
                async with self.database.sessions() as db:
                    await append_usage_event(
                        db,
                        dedupe_key=f"{generation.id}:summary:{cutoff}:retry:pending",
                        event_type="pending",
                        purpose="summary",
                        amount_microusd=None,
                        generation_id=generation.id,
                        thread_id=generation.thread_id,
                        requester_id=generation.requester_id,
                        provider_request_id=None,
                    )
                    await db.commit()

            parts: list[bytes] = []
            collect = bounded_collector(parts)

            request = ProviderRequest(
                generation_id=f"{generation.id}:summary:{cutoff}:attempt:{attempt}",
                purpose="summary",
                mode=str(summary_snapshot.get("mode", "translate")),
                snapshot=summary_snapshot,
            )
            try:
                await self._call_provider(
                    _usage_of(generation),
                    request,
                    collect,
                    asyncio.Event(),
                    ledger_purpose="summary",
                    dedupe_scope=request.generation_id,
                )
            except ProviderError as exc:
                if exc.code in _TRUNCATED_RESPONSE_CODES:
                    # The user's turn is not what was cut off; report the compaction.
                    raise ProviderError("context_compaction_failed") from exc
                raise

            try:
                return _parse_context_summary(b"".join(parts))
            except SummaryFormatError as exc:
                if attempt == 1:
                    continue
                raise ProviderError("context_compaction_failed") from exc
        raise AssertionError("summary attempt loop did not return or raise")

    async def _compact_handoff_context(self, generation: Generation) -> Mapping[str, Any]:
        budget = self.settings.context_compaction_tokens
        chunks: list[Mapping[str, Any]] = []
        async with self.database.sessions() as db:
            thread = await db.get(Thread, generation.thread_id)
            if thread is None:
                raise ProviderError("context_compaction_failed")
            user_ordinals = list(
                (
                    await db.execute(
                        select(Message.ordinal)
                        .where(Message.thread_id == thread.id, Message.role == "user")
                        .order_by(Message.ordinal)
                    )
                ).scalars()
            )
            start = 0
            after_ordinal: int | None = None
            while start < len(user_ordinals):
                low = start
                high = len(user_ordinals) - 1
                best_end: int | None = None
                best_snapshot: Mapping[str, Any] | None = None
                while low <= high:
                    middle = (low + high) // 2
                    candidate = await self._request_snapshot(
                        db,
                        thread,
                        purpose="prompt_handoff",
                        after_ordinal=after_ordinal,
                        through_ordinal=user_ordinals[middle],
                    )
                    if await self._snapshot_tokens(candidate) <= budget:
                        best_end = middle
                        best_snapshot = candidate
                        low = middle + 1
                    else:
                        high = middle - 1
                if best_end is None or best_snapshot is None:
                    # Ordinal bounds cannot split one user-authored turn without changing
                    # its structure. Fail explicitly so the user can split that turn.
                    raise ProviderError("handoff_turn_too_large")
                chunks.append(best_snapshot)
                after_ordinal = user_ordinals[best_end]
                start = best_end + 1

        extracts = [
            await self._generate_handoff_extract(
                generation,
                snapshot=chunk,
                request_suffix=f"source:{index}",
            )
            for index, chunk in enumerate(chunks, start=1)
        ]
        while True:
            final_snapshot = self._snapshot_with_messages(
                generation.request_snapshot,
                build_handoff_merge_messages(extracts),
            )
            if await self._snapshot_tokens(final_snapshot) <= budget:
                break
            groups = await self._bounded_handoff_groups(generation.request_snapshot, extracts)
            if len(groups) >= len(extracts):
                raise ProviderError("context_compaction_failed")
            extracts = [
                await self._generate_handoff_extract(
                    generation,
                    snapshot=self._snapshot_with_messages(
                        generation.request_snapshot,
                        build_handoff_merge_messages(group),
                    ),
                    request_suffix=f"merge:{index}",
                )
                for index, group in enumerate(groups, start=1)
            ]

        async with self.database.sessions() as db:
            current = await db.get(Generation, generation.id)
            if current is None or current.status != "running":
                raise asyncio.CancelledError
            current.request_snapshot = dict(final_snapshot)
            await db.commit()
        return final_snapshot

    async def _bounded_handoff_groups(
        self,
        base_snapshot: Mapping[str, Any],
        extracts: Sequence[str],
    ) -> list[list[str]]:
        groups: list[list[str]] = []
        current: list[str] = []
        for extract in extracts:
            candidate = [*current, extract]
            candidate_snapshot = self._snapshot_with_messages(
                base_snapshot,
                build_handoff_merge_messages(candidate),
            )
            within_budget = (
                await self._snapshot_tokens(candidate_snapshot)
                <= self.settings.context_compaction_tokens
            )
            if within_budget:
                current = candidate
                continue
            if not current:
                raise ProviderError("context_compaction_failed")
            groups.append(current)
            current = [extract]
        if current:
            groups.append(current)
        return groups

    @staticmethod
    def _snapshot_with_messages(
        base_snapshot: Mapping[str, Any],
        messages: Sequence[ProviderMessage],
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "mode": str(base_snapshot.get("mode", "translate")),
            "provider_messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
        }

    async def _generate_handoff_extract(
        self,
        generation: Generation,
        *,
        snapshot: Mapping[str, Any],
        request_suffix: str,
    ) -> str:
        async with self.database.sessions() as db:
            await append_usage_event(
                db,
                dedupe_key=f"{generation.id}:prompt_handoff_compaction:{request_suffix}:pending",
                event_type="pending",
                purpose="prompt_handoff_compaction",
                amount_microusd=None,
                generation_id=generation.id,
                thread_id=generation.thread_id,
                requester_id=generation.requester_id,
                provider_request_id=None,
            )
            await db.commit()

        decoder = ProtocolDecoder()

        async def collect(chunk: bytes) -> None:
            decoder.feed(chunk)

        request = ProviderRequest(
            generation_id=f"{generation.id}:prompt_handoff_compaction:{request_suffix}",
            purpose="prompt_handoff",
            mode=str(snapshot.get("mode", "translate")),
            snapshot=snapshot,
        )
        try:
            await self._call_provider(
                _usage_of(generation),
                request,
                collect,
                asyncio.Event(),
                ledger_purpose="prompt_handoff_compaction",
                dedupe_scope=request.generation_id,
            )
            document = decoder.finish()
            if (
                not isinstance(document.state, NoState)
                or len(document.blocks) != 1
                or document.blocks[0].type != "deliverable"
                or HANDOFF_SENTINEL in document.blocks[0].text
            ):
                raise ProviderError("context_compaction_failed")
            return document.blocks[0].text
        except ProtocolError as exc:
            raise ProviderError("context_compaction_failed") from exc
        except ProviderError as exc:
            if exc.code in _TRUNCATED_RESPONSE_CODES:
                raise ProviderError("context_compaction_failed") from exc
            raise

    async def _prepare_protocol_retry(self, generation_id: str, retry_number: int) -> bool:
        """Discard one uncommitted attempt so the model can regenerate it strictly."""

        async with self.database.sessions() as db:
            generation = await db.get(Generation, generation_id)
            if generation is None:
                return False
            # Partial blocks are explicitly draft streaming state. A response that fails
            # protocol validation was never committed as an assistant message, so reset
            # that draft before each bounded retry even when text had started to
            # stream. This avoids turning a repairable late state/terminal-event mistake
            # into a user-visible hard failure.
            reset = await db.execute(
                update(Generation)
                .where(Generation.id == generation_id, Generation.status == "running")
                .values(partial_blocks=[], stream_revision=Generation.stream_revision + 1)
                .execution_options(synchronize_session=False)
            )
            if int(reset.rowcount or 0) != 1:  # type: ignore[attr-defined]
                return False
            await append_usage_event(
                db,
                dedupe_key=f"{generation.id}:protocol_retry:{retry_number}:pending",
                event_type="pending",
                purpose=generation.purpose,
                amount_microusd=None,
                generation_id=generation.id,
                thread_id=generation.thread_id,
                requester_id=generation.requester_id,
                provider_request_id=None,
            )
            await db.commit()
        await self._notify(generation_id)
        return True

    @staticmethod
    def _protocol_retry_snapshot(snapshot: Mapping[str, Any], error_code: str) -> dict[str, Any]:
        messages = _snapshot_messages(snapshot, error_code="invalid_request_snapshot")
        if not messages or messages[0].role != "system":
            raise ProviderError("invalid_request_snapshot")
        guidance = {
            "state_deliverable_mismatch": (
                "The visible deliverable differed from the canonical output. For "
                "replace, make the deliverable exactly equal the complete output after "
                "all replacements, character for character. For append, the deliverable "
                "is the exact output addition, or the complete joined output together "
                "with output_addition."
            ),
            "state_deliverable_count_mismatch": (
                "A state mutation requires exactly one deliverable block. If you "
                "present multiple alternatives, use operation none instead."
            ),
            "state_source_missing": (
                "An establish for text work must include the complete source. Only an "
                "uploaded source DOCX lets the application supply the source."
            ),
            "state_docx_source_mismatch": (
                "For an uploaded source DOCX, omit source: the application uses the "
                "uploaded document."
            ),
            "state_docx_output_mismatch": (
                "The deliverable must equal the returned docx_blocks' clean text: block "
                "text joined in order with blank lines, keeping hyperlink display text "
                "without its wrappers."
            ),
            "state_output_addition_missing": (
                "The append deliverable showed the complete joined output, so the state "
                "must name the exact output_addition."
            ),
            "invalid_event_fields": (
                "An event had missing or extra keys. Use only the exact keys for "
                "each event and the chosen state operation in the protocol. Never "
                "repeat the deliverable as an output field. Omit unchanged optional "
                "state fields rather than adding null values."
            ),
        }.get(error_code, "Check every event against the exact protocol schema.")
        reminder = (
            "\n\nPROTOCOL RETRY: The preceding attempt failed strict response-protocol "
            "validation and was discarded. Regenerate the response from the same "
            "data. Emit only the exact NDJSON grammar already specified; do not "
            "quote, explain, loosen, or work around it. " + guidance
        )
        repaired = [
            ProviderMessage(role="system", content=messages[0].content + reminder),
            *messages[1:],
        ]
        return {
            "schema_version": 1,
            "mode": str(snapshot.get("mode", "translate")),
            "provider_messages": [
                {"role": message.role, "content": message.content} for message in repaired
            ],
        }

    async def _fail(
        self,
        generation_id: str,
        code: str,
        *,
        preserve_blocks: bool = False,
    ) -> None:
        recorded_code = code if code in _ERROR_MESSAGES else "provider_error"

        async def write() -> bool:
            async with self.database.sessions() as db:
                values: dict[str, Any] = {"error_code": recorded_code}
                if not preserve_blocks:
                    values["partial_blocks"] = []
                failed = await self._transition(
                    db,
                    generation_id,
                    allowed=("queued", "running"),
                    status="failed",
                    **values,
                )
                if not failed:
                    # A pending Stop wins over a late failure.
                    await self._transition(
                        db,
                        generation_id,
                        allowed=("stopping",),
                        status="stopped",
                        error_code=None,
                        partial_blocks=[],
                    )
                await db.commit()
                return failed

        # If even the retried write fails, the row stays active without a task and is
        # finished by the next Stop, submission, retry or restart (see _is_live).
        if await self._with_write_retries(write):
            _LOGGER.error(
                "generation_failed generation_id=%s error_code=%s",
                generation_id,
                recorded_code,
            )
        await self._notify(generation_id)

    async def _run(self, generation_id: str, cancel_event: asyncio.Event) -> None:
        try:
            generation = await self._set_running(generation_id)
            if generation is None:
                # Stopped before it started; finish that stop (no-op if already final).
                await self._finish_stopped(generation_id)
                return
            usage = _usage_of(generation)
            await self._notify(generation_id)
            request_snapshot = await self._maybe_compact_context(generation)

            def protocol_emitter(attempt_decoder: ProtocolDecoder) -> EmitChunk:
                async def emit(chunk: bytes) -> None:
                    if cancel_event.is_set():
                        raise asyncio.CancelledError
                    await self._record_events(
                        generation_id,
                        attempt_decoder.feed(chunk),
                    )

                return emit

            try:
                for attempt in range(3):
                    request = ProviderRequest(
                        generation_id=(
                            generation.id
                            if attempt == 0
                            else f"{generation.id}:protocol-retry:{attempt}"
                        ),
                        purpose=generation.purpose,
                        mode=str(request_snapshot.get("mode", "translate")),
                        snapshot=request_snapshot,
                    )
                    decoder = ProtocolDecoder()
                    emit = protocol_emitter(decoder)

                    try:
                        completion = await self._call_provider(
                            usage,
                            request,
                            emit,
                            cancel_event,
                            ledger_purpose=generation.purpose,
                            dedupe_scope=request.generation_id,
                            record_ids=True,
                        )
                        document = decoder.finish()
                        await self._commit_success(generation_id, document, completion)
                        break
                    except ProtocolError as error:
                        _LOGGER.error(
                            "protocol_attempt_failed generation_id=%s attempt=%s protocol_code=%s",
                            generation_id,
                            attempt + 1,
                            error.code,
                        )
                        if attempt == 2 or not await self._prepare_protocol_retry(
                            generation_id, attempt + 1
                        ):
                            raise
                        request_snapshot = self._protocol_retry_snapshot(
                            request_snapshot, error.code
                        )
                if generation.purpose == "chat" and generation.thread_id is not None:
                    await self._maybe_generate_title(usage, generation.thread_id)
            except ProviderError as error:
                # Its usage was already recorded by _call_provider.
                await self._fail(generation_id, error.code)
            except ProtocolError:
                await self._fail(generation_id, "protocol_error")
            except StaleStateError:
                await self._fail(generation_id, "stale_state", preserve_blocks=True)
            except DocxTemplateOutdatedError:
                await self._fail(generation_id, "docx_template_outdated", preserve_blocks=True)
            except StatePersistenceError:
                await self._fail(
                    generation_id,
                    "state_persistence_failed",
                    preserve_blocks=True,
                )
        except asyncio.CancelledError:
            await asyncio.shield(self._finish_stopped(generation_id))
        except ProviderError as error:
            await self._fail(generation_id, error.code)
        except Exception as error:
            log_unexpected(_LOGGER, error, area="generation")
            await self._fail(generation_id, "provider_error")
        finally:
            self.reconcile_later()
