from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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
    build_handoff_merge_messages,
    build_provider_messages,
)
from oveo.db import Database
from oveo.diagnostics import log_unexpected
from oveo.docx import (
    DOCX_MEDIA_TYPE,
    DocxError,
    DocxReplacement,
    ExtractedDocx,
    docx_blocks_from_storage,
    docx_uncompressed_limit,
    extract_docx,
    plain_text_from_replacements,
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
from oveo.provider import OpenRouterClient, ProviderMessage, count_input_tokens
from oveo.provider import ProviderError as OpenRouterError
from oveo.usage import append_usage_event

EmitChunk = Callable[[bytes], Awaitable[None]]


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


def _snapshot_input_tokens(snapshot: Mapping[str, Any]) -> int:
    return count_input_tokens(_snapshot_messages(snapshot))


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    generation_id: str
    purpose: str
    mode: str
    snapshot: Mapping[str, Any]


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


class OpenRouterProvider:
    """Adapt the tested OpenRouter client to the generation-manager callback contract."""

    def __init__(self, settings: Settings) -> None:
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

        async def on_delta(delta: str) -> None:
            if cancel_event.is_set():
                raise asyncio.CancelledError
            await emit(delta.encode("utf-8"))

        try:
            token_limit = _COMPLETION_TOKEN_LIMITS.get(request.purpose, 32_000)
            completion = await self._client.stream_chat(
                messages,
                max_completion_tokens=token_limit,
                on_delta=on_delta,
            )
        except OpenRouterError as exc:
            # OpenRouterClient has already exhausted safe pre-content retries.
            code = (
                "provider_stream_error"
                if exc.code in {"provider_incomplete_stream", "provider_malformed_stream"}
                else exc.code
            )
            raise ProviderError(
                code,
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
_COMPLETION_TOKEN_LIMITS = {"title": 64, "summary": 2_048, "prompt_handoff": 4_096}
_MIN_RECENT_MESSAGES = 4
_RECONCILE_BATCH_SIZE = 8
_CANCEL_WAIT_SECONDS = 1.0
_LOGGER = logging.getLogger("oveo.background")
_ERROR_MESSAGES = {
    "provider_not_configured": "The model provider is not configured.",
    "provider_network": "The model provider could not be reached.",
    "provider_transient": "The model provider is temporarily unavailable.",
    "provider_rejected": "The model provider rejected the request.",
    "provider_stream_error": (
        "The model provider returned an incomplete or invalid response stream."
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
    "context_compaction_failed": "Oveo could not safely fit this conversation in context.",
    "handoff_turn_too_large": (
        "One user turn is too large to create a safe prompt handoff. "
        "Split that turn into smaller messages and try again."
    ),
    "restart_interrupted": "Generation was interrupted by an application restart.",
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


def _validate_visible_state_correspondence(
    document: ProtocolDocument,
    *,
    resulting_output: str,
) -> None:
    """Require one unambiguous visible representation for every mutation."""

    state = document.state
    if isinstance(state, NoState):
        return
    deliverables = [block.text for block in document.blocks if block.type == "deliverable"]
    if len(deliverables) != 1:
        raise ProtocolError("state_deliverable_count_mismatch")
    visible = deliverables[0]
    if isinstance(state, AppendState):
        # Append deliberately displays just the new passage by default, while a user may
        # explicitly request the complete updated document.
        if visible not in {state.output_addition, resulting_output}:
            raise ProtocolError("state_deliverable_mismatch")
        return
    if visible != resulting_output:
        raise ProtocolError("state_deliverable_mismatch")


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
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel: dict[str, asyncio.Event] = {}
        self._conditions: dict[str, asyncio.Condition] = {}
        self._reconcile_lock = asyncio.Lock()
        self._reconcile_task: asyncio.Task[None] | None = None
        self._reconcile_requested = False
        self._shutting_down = False

    async def reconcile_orphans(self) -> int:
        async with self.database.sessions() as db:
            result = await db.execute(
                update(Generation)
                .where(Generation.status.in_(_ACTIVE))
                .values(
                    status="failed",
                    error_code="restart_interrupted",
                    partial_blocks=[],
                    finished_at=utc_now(),
                    stream_revision=Generation.stream_revision + 1,
                )
            )
            await db.commit()
            return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def reconcile_pending_costs(self) -> int:
        """Append charges for provider calls whose final stream omitted usage."""

        reconcile = getattr(self.provider, "reconcile_cost", None)
        if reconcile is None:
            return 0
        async with self._reconcile_lock:
            charge = aliased(UsageEvent)
            reconciled_charge_exists = (
                select(charge.id)
                .where(
                    charge.event_type == "charge",
                    charge.dedupe_key
                    == (
                        UsageEvent.provider_generation_id
                        + ":"
                        + UsageEvent.purpose
                        + ":reconciled-charge"
                    ),
                )
                .correlate(UsageEvent)
                .exists()
            )
            async with self.database.sessions() as db:
                pending = list(
                    (
                        await db.execute(
                            select(UsageEvent)
                            .where(
                                UsageEvent.event_type == "pending",
                                UsageEvent.provider_generation_id.is_not(None),
                                ~reconciled_charge_exists,
                            )
                            .order_by(UsageEvent.created_at.desc(), UsageEvent.id.desc())
                            .limit(_RECONCILE_BATCH_SIZE)
                        )
                    ).scalars()
                )

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
                except Exception:  # noqa: S112 -- best-effort metadata
                    # Reconciliation is best effort and must never make startup/readiness
                    # depend on a metadata endpoint. No provider body is retained or logged.
                    continue
                if cost_microusd is None:
                    continue
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
            return reconciled

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
        if after_ordinal is not None:
            attachment_query = attachment_query.where(Message.ordinal > after_ordinal)
        if through_ordinal is not None:
            attachment_query = attachment_query.where(Message.ordinal <= through_ordinal)
        attachment_rows = list((await db.execute(attachment_query)).scalars())
        attachments: dict[str, AttachmentDocument] = {}
        for attachment in attachment_rows:
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
            except (OSError, DocxError) as exc:
                raise GenerationError(
                    "attachment_unavailable",
                    "The saved source attachment is unavailable.",
                ) from exc
            attachments[attachment.message_id] = AttachmentDocument(
                word_count=attachment.word_count,
                document_blocks=document_blocks,
            )
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
            canonical_state=canonical,
        )
        return {
            "schema_version": 1,
            "mode": thread.mode,
            "provider_messages": [
                {"role": message.role, "content": message.content} for message in provider_messages
            ],
        }

    async def submit_turn(
        self,
        *,
        requester_id: str,
        client_request_id: str,
        text: str,
        attachment: ValidatedAttachment | None,
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
                thread_id=thread_id,
                owner_id=owner_id,
                mode=mode,
                staged_files=staged_files,
            )
        except BaseException:
            for staged_file in staged_files:
                staged_file.unlink(missing_ok=True)
            raise

    async def _submit_turn(
        self,
        *,
        requester_id: str,
        client_request_id: str,
        text: str,
        attachment: ValidatedAttachment | None,
        thread_id: str | None,
        owner_id: str | None,
        mode: str | None,
        staged_files: list[Path],
    ) -> Submission:
        clean_text = text.strip()
        if not clean_text and attachment is None:
            raise GenerationError("empty_message", "Enter a message or attach a source file.")
        if not client_request_id or len(client_request_id) > 100:
            raise GenerationError("invalid_request_id", "The request identifier is invalid.")

        storage_path: Path | None = None
        async with self.database.sessions() as db:
            existing = await db.scalar(
                select(Generation).where(
                    Generation.requester_id == requester_id,
                    Generation.client_request_id == client_request_id,
                )
            )
            if existing is not None:
                if existing.thread_id is None:
                    raise GenerationError("invalid_generation", "The saved request is invalid.")
                existing_owner = await db.scalar(
                    select(Thread.owner_id).where(Thread.id == existing.thread_id)
                )
                if existing_owner != requester_id:
                    raise GenerationError(
                        "thread_not_found", "Conversation not found.", status_code=404
                    )
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
                if existing_thread is None:
                    raise GenerationError(
                        "thread_not_found", "Conversation not found.", status_code=404
                    )
                if existing_thread.owner_id != requester_id:
                    raise GenerationError(
                        "thread_not_found", "Conversation not found.", status_code=404
                    )
                thread = existing_thread

            active = await db.scalar(
                select(Generation.id).where(
                    Generation.thread_id == thread.id,
                    Generation.status.in_(_ACTIVE),
                )
            )
            if active is not None:
                raise ActiveGenerationError

            latest_words = await db.scalar(
                select(WorkVersion.source_word_count)
                .join(WorkItem, WorkVersion.work_item_id == WorkItem.id)
                .where(WorkItem.thread_id == thread.id, WorkItem.active.is_(True))
                .order_by(WorkVersion.version_no.desc())
                .limit(1)
            )
            added_words = attachment.word_count if attachment is not None else 0
            if attachment is None:
                added_words = count_words(clean_text)
            measured_words = int(latest_words or 0) + added_words
            if measured_words > self.settings.max_source_words:
                raise GenerationError(
                    "source_word_limit",
                    f"Source contains {measured_words:,} words; the maximum is "
                    f"{self.settings.max_source_words:,} words.",
                )

            ordinal = (
                int(
                    await db.scalar(
                        select(func.coalesce(func.max(Message.ordinal), 0)).where(
                            Message.thread_id == thread.id
                        )
                    )
                    or 0
                )
                + 1
            )
            message = Message(
                id=new_id(),
                thread_id=thread.id,
                ordinal=ordinal,
                role="user",
                actor_user_id=requester_id,
                content=([{"type": "conversation", "text": text}] if text else []),
            )
            db.add(message)
            # Flush the parent row before the optional attachment. The models use IDs
            # rather than ORM relationships, so this explicit ordering is required.
            await db.flush()
            if attachment is not None:
                attachment_id = new_id()
                storage_name = f"{attachment_id}.docx"
                storage_path = persist_attachment(
                    self.settings.attachments_dir, storage_name, attachment.content
                )
                staged_files.append(storage_path)
                db.add(
                    Attachment(
                        id=attachment_id,
                        message_id=message.id,
                        storage_name=storage_name,
                        original_name=attachment.original_name,
                        media_type=DOCX_MEDIA_TYPE,
                        document_blocks=[block.to_model() for block in attachment.document_blocks],
                        byte_count=attachment.byte_count,
                        word_count=attachment.word_count,
                        sha256=attachment.sha256,
                    )
                )
                await db.flush()
            request_snapshot = await self._request_snapshot(db, thread, purpose="chat")
            generation = Generation(
                id=new_id(),
                thread_id=thread.id,
                requester_id=requester_id,
                source_message_id=message.id,
                client_request_id=client_request_id,
                purpose="chat",
                status="queued",
                request_snapshot=request_snapshot,
            )
            db.add(generation)
            thread.updated_at = utc_now()
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                if storage_path is not None:
                    storage_path.unlink(missing_ok=True)
                existing = await db.scalar(
                    select(Generation).where(
                        Generation.requester_id == requester_id,
                        Generation.client_request_id == client_request_id,
                    )
                )
                if existing is not None and existing.thread_id is not None:
                    return Submission(existing.thread_id, existing.id)
                raise ActiveGenerationError from exc

        self._schedule(generation.id)
        return Submission(thread.id, generation.id)

    async def submit_handoff(
        self,
        *,
        thread_id: str,
        requester_id: str,
        client_request_id: str,
    ) -> str:
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
                    raise GenerationError(
                        "thread_not_found", "Conversation not found.", status_code=404
                    )
                return existing.id
            thread = await db.get(Thread, thread_id)
            if thread is None:
                raise GenerationError(
                    "thread_not_found", "Conversation not found.", status_code=404
                )
            if thread.owner_id != requester_id:
                raise GenerationError(
                    "thread_not_found", "Conversation not found.", status_code=404
                )
            request_snapshot = await self._request_snapshot(db, thread, purpose="prompt_handoff")
            generation = Generation(
                id=new_id(),
                thread_id=thread.id,
                requester_id=requester_id,
                client_request_id=client_request_id,
                purpose="prompt_handoff",
                status="queued",
                request_snapshot=request_snapshot,
            )
            db.add(generation)
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                existing = await db.scalar(
                    select(Generation).where(
                        Generation.requester_id == requester_id,
                        Generation.client_request_id == client_request_id,
                    )
                )
                if existing is not None:
                    return str(existing.id)
                raise ActiveGenerationError from exc
        self._schedule(generation.id)
        return generation.id

    async def retry(
        self,
        *,
        generation_id: str,
        requester_id: str,
        client_request_id: str,
    ) -> str:
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
                    raise GenerationError(
                        "generation_not_retryable",
                        "This generation cannot be retried.",
                        status_code=409,
                    )
                return existing.id
            original = await db.get(Generation, generation_id)
            if original is None or original.status not in {"failed", "stopped"}:
                raise GenerationError(
                    "generation_not_retryable",
                    "This generation cannot be retried.",
                    status_code=409,
                )
            if original.thread_id is None:
                raise GenerationError(
                    "generation_not_retryable",
                    "This generation cannot be retried.",
                    status_code=409,
                )
            thread = await db.get(Thread, original.thread_id)
            if thread is None:
                raise GenerationError(
                    "thread_not_found", "Conversation not found.", status_code=404
                )
            if thread.owner_id != requester_id:
                raise GenerationError(
                    "generation_not_retryable",
                    "This generation cannot be retried.",
                    status_code=409,
                )
            request_snapshot = await self._request_snapshot(
                db,
                thread,
                purpose=original.purpose,
            )
            retry = Generation(
                id=new_id(),
                thread_id=original.thread_id,
                requester_id=requester_id,
                source_message_id=original.source_message_id,
                retry_of_generation_id=original.id,
                client_request_id=client_request_id,
                purpose=original.purpose,
                status="queued",
                request_snapshot=request_snapshot,
            )
            db.add(retry)
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                raise ActiveGenerationError from exc
        self._schedule(retry.id)
        return retry.id

    async def stop(self, generation_id: str) -> None:
        async with self.database.sessions() as db:
            generation = await db.get(Generation, generation_id)
            if generation is None:
                raise GenerationError(
                    "generation_not_found", "Generation not found.", status_code=404
                )
            if generation.status not in _ACTIVE:
                return
            generation.status = "stopping"
            generation.stream_revision += 1
            await db.commit()
        await self._notify(generation_id)
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
                "retryable": generation.status in {"failed", "stopped"},
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
            generation = await db.get(Generation, generation_id)
            if generation is None or generation.status != "queued":
                return None
            generation.status = "running"
            generation.started_at = utc_now()
            generation.stream_revision += 1
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
            generation.partial_blocks = blocks
            generation.stream_revision += 1
            await db.commit()
        await self._notify(generation_id)

    async def _commit_success(
        self,
        generation_id: str,
        document: ProtocolDocument,
        completion: ProviderCompletion,
    ) -> None:
        blocks = cast(list[dict[str, str]], document.to_storage()["blocks"])
        mutates_state = not isinstance(document.state, NoState)
        try:
            async with self.database.sessions() as db:
                generation = await db.get(Generation, generation_id)
                if generation is None:
                    return
                if generation.status == "stopping":
                    await self._mark_stopped(db, generation)
                    await db.commit()
                    return
                if generation.status != "running":
                    return
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
                    ordinal = (
                        int(
                            await db.scalar(
                                select(func.coalesce(func.max(Message.ordinal), 0)).where(
                                    Message.thread_id == generation.thread_id
                                )
                            )
                            or 0
                        )
                        + 1
                    )
                    message = Message(
                        id=new_id(),
                        thread_id=generation.thread_id,
                        ordinal=ordinal,
                        role="assistant",
                        actor_user_id=None,
                        content=blocks,
                    )
                    db.add(message)
                    # Ensure the assistant row exists before updating the generation's
                    # result-message foreign key.
                    await db.flush()
                    try:
                        await self._apply_state_operation(
                            db,
                            generation=generation,
                            thread=thread,
                            document=document,
                        )
                    except ProtocolError as exc:
                        if exc.code in {
                            "state_deliverable_count_mismatch",
                            "state_deliverable_mismatch",
                        }:
                            raise
                        if exc.code in {"state_base_missing", "state_base_mismatch"}:
                            raise StaleStateError from exc
                        raise StatePersistenceError from exc
                    generation.result_message_id = message.id
                    thread.updated_at = utc_now()
                generation.partial_blocks = blocks
                generation.provider_request_id = completion.provider_request_id
                generation.provider_generation_id = completion.provider_generation_id
                generation.status = "completed"
                generation.finished_at = utc_now()
                generation.stream_revision += 1
                await db.commit()
        except SQLAlchemyError as exc:
            if mutates_state:
                raise StatePersistenceError from exc
            raise
        await self._notify(generation_id)

    async def _apply_state_operation(
        self,
        db: AsyncSession,
        *,
        generation: Generation,
        thread: Thread | None,
        document: ProtocolDocument,
    ) -> None:
        """Validate and persist one hidden canonical mutation in the response commit."""

        state = document.state
        if thread is None:
            raise ProtocolError("state_thread_missing")
        if isinstance(state, NoState):
            return

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
            if source_attachment is not None and source_attachment.media_type == DOCX_MEDIA_TYPE
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
            source = state.source
            output = state.output
            brief = state.brief
            version_no = 1
            parent_version_id = None
            operation = "establish"
            if source_docx is not None:
                if state_docx_blocks is None:
                    raise ProtocolError("state_docx_blocks_missing")
                extracted = self._read_docx_template(source_docx)
                if source != extracted.plain_text:
                    raise ProtocolError("state_docx_source_mismatch")
                docx_template_attachment_id = source_docx.id
                template_blocks = extracted.blocks
            elif state_docx_blocks is not None:
                raise ProtocolError("state_docx_template_missing")
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
                extracted = self._read_docx_template(template_attachment)
                docx_template_attachment_id = template_attachment.id
                template_blocks = extracted.blocks
            elif state_docx_blocks is not None:
                raise ProtocolError("state_docx_template_missing")

            if isinstance(state, AppendState):
                source = _append_text(source, state.source_addition, state.source_separator)
                output = _append_text(output, state.output_addition, state.output_separator)
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
                brief = state.brief if state.brief is not None else brief
            elif isinstance(state, FullState):
                source = state.source if state.source is not None else source
                output = state.output
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

        _validate_visible_state_correspondence(document, resulting_output=output)
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

    def _read_docx_template(self, attachment: Attachment) -> ExtractedDocx:
        if attachment.media_type != DOCX_MEDIA_TYPE:
            raise ProtocolError("state_docx_template_invalid")
        root = self.settings.attachments_dir.resolve()
        path = (root / attachment.storage_name).resolve()
        if path.parent != root:
            raise ProtocolError("state_docx_template_invalid")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ProtocolError("state_docx_template_missing") from exc
        if (
            len(content) != attachment.byte_count
            or hashlib.sha256(content).hexdigest() != attachment.sha256
        ):
            raise ProtocolError("state_docx_template_invalid")
        try:
            return extract_docx(
                content,
                max_uncompressed_bytes=docx_uncompressed_limit(self.settings.max_upload_bytes),
            )
        except DocxError as exc:
            raise ProtocolError("state_docx_template_invalid") from exc

    async def _record_provider_completion(
        self,
        generation_id: str,
        completion: ProviderCompletion,
        *,
        purpose: str,
        record_ids: bool = True,
        dedupe_scope: str | None = None,
    ) -> None:
        async with self.database.sessions() as db:
            generation = await db.get(Generation, generation_id)
            if generation is None:
                return
            if record_ids:
                generation.provider_request_id = (
                    completion.provider_request_id or generation.provider_request_id
                )
                generation.provider_generation_id = (
                    completion.provider_generation_id or generation.provider_generation_id
                )
            if completion.cost_microusd is not None:
                provider_key = completion.provider_request_id or dedupe_scope or generation.id
                await append_usage_event(
                    db,
                    dedupe_key=f"{provider_key}:{purpose}:charge",
                    event_type="charge",
                    purpose=purpose,
                    amount_microusd=completion.cost_microusd,
                    generation_id=generation.id,
                    thread_id=generation.thread_id,
                    requester_id=generation.requester_id,
                    provider_request_id=completion.provider_request_id,
                    provider_generation_id=completion.provider_generation_id,
                )
            elif completion.provider_generation_id is not None:
                await append_usage_event(
                    db,
                    dedupe_key=(f"{completion.provider_generation_id}:{purpose}:reconcile-pending"),
                    event_type="pending",
                    purpose=purpose,
                    amount_microusd=None,
                    generation_id=generation.id,
                    thread_id=generation.thread_id,
                    requester_id=generation.requester_id,
                    provider_request_id=completion.provider_request_id,
                    provider_generation_id=completion.provider_generation_id,
                )
            await db.commit()

    async def _record_provider_error(
        self,
        generation_id: str,
        error: ProviderError,
        *,
        purpose: str,
    ) -> None:
        async with self.database.sessions() as db:
            generation = await db.get(Generation, generation_id)
            if generation is None:
                return
            generation.provider_request_id = (
                error.provider_request_id or generation.provider_request_id
            )
            generation.provider_generation_id = (
                error.provider_generation_id or generation.provider_generation_id
            )
            if error.cost_microusd is not None:
                provider_key = error.provider_request_id or generation.id
                await append_usage_event(
                    db,
                    dedupe_key=f"{provider_key}:{purpose}:failed-charge",
                    event_type="charge",
                    purpose=purpose,
                    amount_microusd=error.cost_microusd,
                    generation_id=generation.id,
                    thread_id=generation.thread_id,
                    requester_id=generation.requester_id,
                    provider_request_id=error.provider_request_id,
                    provider_generation_id=error.provider_generation_id,
                )
            elif error.provider_generation_id is not None:
                await append_usage_event(
                    db,
                    dedupe_key=(f"{error.provider_generation_id}:{purpose}:reconcile-pending"),
                    event_type="pending",
                    purpose=purpose,
                    amount_microusd=None,
                    generation_id=generation.id,
                    thread_id=generation.thread_id,
                    requester_id=generation.requester_id,
                    provider_request_id=error.provider_request_id,
                    provider_generation_id=error.provider_generation_id,
                )
            await db.commit()

    async def _maybe_generate_title(self, generation_id: str, thread_id: str) -> None:
        """Generate the first title as a non-transcript, separately accounted model call."""

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
            snapshot = await self._request_snapshot(db, thread, purpose="title")
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
            completion = await self.provider.generate(request, collect, asyncio.Event())
            await self._record_provider_completion(
                generation_id,
                completion,
                purpose="title",
                record_ids=False,
            )
            title = b"".join(parts).decode("utf-8").strip()
            if not title or len(title) > 60 or "\n" in title or not 2 <= len(title.split()) <= 6:
                return
            async with self.database.sessions() as db:
                thread = await db.get(Thread, thread_id)
                generation = await db.get(Generation, generation_id)
                if thread is None or generation is None:
                    return
                if thread.title == "New conversation":
                    thread.title = title
                    thread.updated_at = utc_now()
                await db.commit()
        except ProviderError as exc:
            await self._record_provider_error(generation_id, exc, purpose="title")
        except (UnicodeDecodeError, ValueError):
            return

    async def _mark_stopped(self, db: AsyncSession, generation: Generation) -> None:
        generation.status = "stopped"
        generation.partial_blocks = []
        generation.error_code = None
        generation.finished_at = utc_now()
        generation.stream_revision += 1

    async def _finish_stopped(self, generation_id: str) -> None:
        async with self.database.sessions() as db:
            generation = await db.get(Generation, generation_id)
            if generation is not None and generation.status in _ACTIVE:
                await self._mark_stopped(db, generation)
                await db.commit()
        await self._notify(generation_id)

    async def _maybe_compact_context(self, generation: Generation) -> Mapping[str, Any]:
        snapshot = generation.request_snapshot
        if generation.purpose not in {"chat", "prompt_handoff"}:
            return snapshot
        if _snapshot_input_tokens(snapshot) <= self.settings.context_compaction_tokens:
            return snapshot
        if generation.thread_id is None:
            raise ProviderError("context_compaction_failed")
        if generation.purpose == "prompt_handoff":
            return await self._compact_handoff_context(generation)
        return await self._compact_chat_context(generation)

    async def _compact_chat_context(self, generation: Generation) -> Mapping[str, Any]:
        snapshot: Mapping[str, Any] = generation.request_snapshot
        budget = self.settings.context_compaction_tokens
        while _snapshot_input_tokens(snapshot) > budget:
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
            if _snapshot_input_tokens(candidate) <= self.settings.context_compaction_tokens:
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
                completion = await self.provider.generate(request, collect, asyncio.Event())
                await self._record_provider_completion(
                    generation.id,
                    completion,
                    purpose="summary",
                    record_ids=False,
                    dedupe_scope=request.generation_id,
                )
            except ProviderError as exc:
                await self._record_provider_error(
                    generation.id,
                    exc,
                    purpose="summary",
                )
                raise

            try:
                return _parse_context_summary(b"".join(parts))
            except SummaryFormatError as exc:
                if attempt == 1:
                    continue
                error = ProviderError("context_compaction_failed")
                await self._record_provider_error(
                    generation.id,
                    error,
                    purpose="summary",
                )
                raise error from exc
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
                    if _snapshot_input_tokens(candidate) <= budget:
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
            if _snapshot_input_tokens(final_snapshot) <= budget:
                break
            groups = self._bounded_handoff_groups(generation.request_snapshot, extracts)
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

    def _bounded_handoff_groups(
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
                _snapshot_input_tokens(candidate_snapshot)
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
            completion = await self.provider.generate(request, collect, asyncio.Event())
            document = decoder.finish()
            if (
                not isinstance(document.state, NoState)
                or len(document.blocks) != 1
                or document.blocks[0].type != "deliverable"
                or HANDOFF_SENTINEL in document.blocks[0].text
            ):
                raise ProviderError("context_compaction_failed")
            await self._record_provider_completion(
                generation.id,
                completion,
                purpose="prompt_handoff_compaction",
                record_ids=False,
                dedupe_scope=(f"{generation.id}:prompt_handoff_compaction:{request_suffix}"),
            )
            return document.blocks[0].text
        except (ProviderError, ProtocolError) as exc:
            error = (
                exc
                if isinstance(exc, ProviderError)
                else ProviderError("context_compaction_failed")
            )
            await self._record_provider_error(
                generation.id,
                error,
                purpose="prompt_handoff_compaction",
            )
            raise error from exc

    async def _prepare_protocol_retry(self, generation_id: str) -> bool:
        """Reset only an attempt that has not exposed substantive visible content."""

        async with self.database.sessions() as db:
            generation = await db.get(Generation, generation_id)
            if generation is None or generation.status != "running":
                return False
            blocks = (
                generation.partial_blocks if isinstance(generation.partial_blocks, list) else []
            )
            if any(
                isinstance(block, dict)
                and isinstance(block.get("text"), str)
                and cast(str, block["text"]).strip()
                for block in blocks
            ):
                return False
            generation.partial_blocks = []
            generation.stream_revision += 1
            await append_usage_event(
                db,
                dedupe_key=f"{generation.id}:protocol_retry:pending",
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
    def _protocol_retry_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        messages = _snapshot_messages(snapshot, error_code="invalid_request_snapshot")
        if not messages or messages[0].role != "system":
            raise ProviderError("invalid_request_snapshot")
        reminder = (
            "\n\nPROTOCOL RETRY: The preceding attempt failed strict response-protocol "
            "validation before any substantive visible content was accepted. Regenerate "
            "the response from the same data. Emit only the exact NDJSON grammar already "
            "specified; do not quote, explain, loosen, or work around it."
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
        recorded_code: str | None = None
        async with self.database.sessions() as db:
            generation = await db.get(Generation, generation_id)
            if generation is not None and generation.status in _ACTIVE:
                generation.status = "failed"
                if not preserve_blocks:
                    generation.partial_blocks = []
                recorded_code = code if code in _ERROR_MESSAGES else "provider_error"
                generation.error_code = recorded_code
                generation.finished_at = utc_now()
                generation.stream_revision += 1
                await db.commit()
        if recorded_code is not None:
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
                return
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
                for attempt in range(2):
                    request = ProviderRequest(
                        generation_id=(
                            generation.id if attempt == 0 else f"{generation.id}:protocol-retry"
                        ),
                        purpose=generation.purpose,
                        mode=str(request_snapshot.get("mode", "translate")),
                        snapshot=request_snapshot,
                    )
                    decoder = ProtocolDecoder()
                    emit = protocol_emitter(decoder)

                    try:
                        completion = await self.provider.generate(request, emit, cancel_event)
                        await self._record_provider_completion(
                            generation_id,
                            completion,
                            purpose=generation.purpose,
                            dedupe_scope=request.generation_id,
                        )
                        document = decoder.finish()
                        await self._commit_success(generation_id, document, completion)
                        break
                    except ProtocolError:
                        if attempt != 0 or not await self._prepare_protocol_retry(generation_id):
                            raise
                        request_snapshot = self._protocol_retry_snapshot(request_snapshot)
                if generation.purpose == "chat" and generation.thread_id is not None:
                    await self._maybe_generate_title(generation.id, generation.thread_id)
            except ProviderError as error:
                await self._record_provider_error(
                    generation_id,
                    error,
                    purpose=generation.purpose,
                )
                await self._fail(generation_id, error.code)
            except ProtocolError:
                await self._fail(generation_id, "protocol_error")
            except StaleStateError:
                await self._fail(generation_id, "stale_state", preserve_blocks=True)
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
