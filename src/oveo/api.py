from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile as StarletteUploadFile

from oveo.attachments import AttachmentError, validate_attachment_upload
from oveo.auth import (
    InvalidCredentials,
    LoginThrottled,
    SessionPrincipal,
    authenticate_user,
    create_session,
    get_session_principal,
    revoke_session,
    session_is_active,
    validate_csrf,
)
from oveo.authorization import may_access_thread
from oveo.config import Settings
from oveo.db import Database
from oveo.docx import DOCX_MEDIA_TYPE, DocxError, docx_uncompressed_limit, render_docx
from oveo.generation import GenerationError, GenerationManager
from oveo.models import (
    Attachment,
    Generation,
    Message,
    Thread,
    User,
    WorkItem,
    WorkVersion,
)
from oveo.usage import format_lifetime_cost, lifetime_total
from oveo.workers import WorkerBusy

router = APIRouter()
ThreadModeInput = Literal["translate", "revision", "internal_comms"]
AttachmentRoleInput = Literal["source", "reference"]


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class RenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=160)


class RetryBody(BaseModel):
    client_request_id: str = Field(min_length=1, max_length=100)


def _database(request: Request) -> Database:
    return request.app.state.database  # type: ignore[no-any-return]


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _manager(request: Request) -> GenerationManager:
    return request.app.state.generation_manager  # type: ignore[no-any-return]


async def database_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with _database(request).sessions() as db:
        yield db


Db = Annotated[AsyncSession, Depends(database_session)]


async def current_principal(request: Request, db: Db) -> SessionPrincipal:
    settings = _settings(request)
    token = request.cookies.get(settings.session_cookie_name)
    principal = await get_session_principal(db, token)
    if principal is None:
        await db.commit()
        raise ApiError(401, "authentication_required", "Sign in to continue.")
    return principal


Principal = Annotated[SessionPrincipal, Depends(current_principal)]


async def csrf_principal(request: Request, principal: Principal) -> SessionPrincipal:
    settings = _settings(request)
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") != settings.public_origin:
        raise ApiError(403, "csrf_failed", "The request could not be verified.")
    if not validate_csrf(
        principal.session,
        request.cookies.get(settings.csrf_cookie_name),
        request.headers.get("x-csrf-token"),
    ):
        raise ApiError(403, "csrf_failed", "The request could not be verified.")
    return principal


CsrfPrincipal = Annotated[SessionPrincipal, Depends(csrf_principal)]


def _account(user: User) -> dict[str, str]:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
    }


def _require_thread(principal: SessionPrincipal, thread: Thread | None) -> Thread:
    if thread is None:
        raise ApiError(404, "thread_not_found", "Conversation not found.")
    if not may_access_thread(principal.user, thread):
        # Match the missing-record response so conversation IDs cannot be used to
        # discover another account's records.
        raise ApiError(404, "thread_not_found", "Conversation not found.")
    return thread


async def _require_generation(
    db: AsyncSession, principal: SessionPrincipal, generation_id: str
) -> Generation:
    generation = await db.get(Generation, generation_id)
    if generation is None:
        raise ApiError(404, "generation_not_found", "Generation not found.")
    if generation.thread_id is not None:
        thread = await db.get(Thread, generation.thread_id)
        _require_thread(principal, thread)
    elif generation.requester_id != principal.user.id:
        raise ApiError(404, "generation_not_found", "Generation not found.")
    return generation


def _set_auth_cookies(
    response: Response,
    *,
    settings: Settings,
    token: str,
    csrf_token: str,
) -> None:
    max_age = settings.session_days * 86_400
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=max_age,
        secure=settings.secure_cookies,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token,
        max_age=max_age,
        secure=settings.secure_cookies,
        httponly=False,
        samesite="strict",
        path="/",
    )


@router.post("/api/auth/login")
async def login(request: Request, response: Response, body: LoginBody, db: Db) -> dict[str, str]:
    settings = _settings(request)
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") != settings.public_origin:
        raise ApiError(403, "csrf_failed", "The request could not be verified.")
    try:
        user = await authenticate_user(
            db,
            username=body.username,
            password=body.password,
            max_attempts=settings.login_attempts,
            window_seconds=settings.login_window_seconds,
            lock_seconds=settings.login_lock_seconds,
        )
    except LoginThrottled as exc:
        await db.commit()
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        raise ApiError(429, "login_throttled", "Try signing in again later.") from exc
    except InvalidCredentials as exc:
        await db.commit()
        raise ApiError(
            401, "invalid_credentials", "The username or password is incorrect."
        ) from exc
    issued = await create_session(db, user=user, session_days=settings.session_days)
    await db.commit()
    _set_auth_cookies(
        response,
        settings=settings,
        token=issued.token,
        csrf_token=issued.csrf_token,
    )
    return {**_account(user), "csrf_token": issued.csrf_token}


@router.post("/api/auth/logout", status_code=204)
async def logout(request: Request, response: Response, principal: CsrfPrincipal, db: Db) -> None:
    settings = _settings(request)
    session_id = principal.session.id
    await revoke_session(db, request.cookies.get(settings.session_cookie_name))
    await db.commit()
    # Live responses must stop reaching this browser as soon as it signs out.
    _manager(request).close_session_streams(session_id)
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")


@router.get("/api/auth/me")
async def me(request: Request, principal: Principal) -> dict[str, str]:
    csrf_token = request.cookies.get(_settings(request).csrf_cookie_name)
    result = _account(principal.user)
    if csrf_token:
        result["csrf_token"] = csrf_token
    return result


@router.get("/api/accounts")
async def accounts(principal: Principal) -> list[dict[str, str]]:
    return [_account(principal.user)]


async def _thread_summary(db: AsyncSession, thread: Thread) -> dict[str, Any]:
    active_generation = await db.scalar(
        select(Generation.id)
        .where(
            Generation.thread_id == thread.id,
            Generation.status.in_(("queued", "running", "stopping")),
        )
        .order_by(Generation.created_at.desc())
        .limit(1)
    )
    owner_username = await db.scalar(select(User.username).where(User.id == thread.owner_id))
    return {
        "id": thread.id,
        "owner_id": thread.owner_id,
        "owner_username": owner_username,
        "mode": thread.mode,
        "title": thread.title or "New conversation",
        "updated_at": thread.updated_at.isoformat(),
        "active_generation_id": active_generation,
    }


@router.get("/api/threads")
async def list_threads(
    principal: Principal, db: Db, owner_id: str | None = None
) -> list[dict[str, Any]]:
    if owner_id is not None and owner_id != principal.user.id:
        raise ApiError(403, "forbidden", "You cannot view that account.")
    threads = (
        await db.execute(
            select(Thread)
            .where(Thread.owner_id == principal.user.id)
            .order_by(Thread.updated_at.desc())
        )
    ).scalars()
    return [await _thread_summary(db, thread) for thread in threads]


def _message_blocks(message: Message) -> list[dict[str, str]]:
    if not isinstance(message.content, list):
        return []
    return [
        {"type": str(block.get("type", "conversation")), "text": str(block.get("text", ""))}
        for block in message.content
        if isinstance(block, dict)
    ]


async def _latest_work_version(db: AsyncSession, thread_id: str) -> WorkVersion | None:
    return cast(
        WorkVersion | None,
        await db.scalar(
            select(WorkVersion)
            .join(WorkItem, WorkVersion.work_item_id == WorkItem.id)
            .where(WorkItem.thread_id == thread_id, WorkItem.active.is_(True))
            .order_by(WorkVersion.version_no.desc())
            .limit(1)
        ),
    )


@router.get("/api/threads/{thread_id}")
async def thread_detail(
    thread_id: str, request: Request, principal: Principal, db: Db
) -> dict[str, Any]:
    thread = _require_thread(principal, await db.get(Thread, thread_id))
    summary = await _thread_summary(db, thread)
    messages = list(
        (
            await db.execute(
                select(Message).where(Message.thread_id == thread.id).order_by(Message.ordinal)
            )
        ).scalars()
    )
    actor_ids = {message.actor_user_id for message in messages if message.actor_user_id}
    actors = {
        user.id: user.username
        for user in (await db.execute(select(User).where(User.id.in_(actor_ids)))).scalars()
    }
    attachment_rows = list(
        (
            await db.execute(
                select(Attachment)
                .join(Message, Attachment.message_id == Message.id)
                .where(Message.thread_id == thread.id)
            )
        ).scalars()
    )
    attachments = {attachment.message_id: attachment for attachment in attachment_rows}
    serialized_messages: list[dict[str, Any]] = []
    for message in messages:
        attachment = attachments.get(message.id)
        actor_username = None
        if message.actor_user_id is not None and message.actor_user_id != thread.owner_id:
            actor_username = actors.get(message.actor_user_id)
        serialized_messages.append(
            {
                "id": message.id,
                "role": message.role,
                "actor_username": actor_username,
                "blocks": _message_blocks(message),
                "attachment": (
                    {
                        "filename": attachment.original_name,
                        "role": attachment.role,
                        "byte_size": attachment.byte_count,
                        "word_count": attachment.word_count,
                        "media_type": attachment.media_type,
                    }
                    if attachment is not None
                    else None
                ),
                "created_at": message.created_at.isoformat(),
            }
        )
    manager = _manager(request)
    # The chat view follows chat generations only; a prompt handoff has its own field so
    # it can neither appear as the assistant's reply nor hide a failed turn's Retry.
    latest_chat_row = (
        await db.execute(
            select(Generation.id, Generation.status)
            .where(Generation.thread_id == thread.id, Generation.purpose == "chat")
            .order_by(Generation.created_at.desc(), Generation.id.desc())
            .limit(1)
        )
    ).first()
    generation_snapshot = None
    if latest_chat_row is not None and latest_chat_row.status != "completed":
        generation_snapshot = await manager.get_snapshot(latest_chat_row.id)
    active_handoff = await db.scalar(
        select(Generation.id).where(
            Generation.thread_id == thread.id,
            Generation.purpose == "prompt_handoff",
            Generation.status.in_(("queued", "running", "stopping")),
        )
    )
    handoff_snapshot = (
        await manager.get_snapshot(active_handoff) if active_handoff is not None else None
    )
    latest_version = await _latest_work_version(db, thread.id)
    return {
        **summary,
        "messages": serialized_messages,
        "generation": generation_snapshot,
        "handoff": handoff_snapshot,
        "docx_exportable": bool(
            latest_version is not None
            and latest_version.docx_template_attachment_id is not None
            and latest_version.docx_blocks is not None
        ),
    }


_BUSY_RETRY_AFTER = {"Retry-After": "5"}


async def _validated_upload(request: Request, attachment: UploadFile | None) -> Any:
    if attachment is None:
        return None
    try:
        return await validate_attachment_upload(
            attachment,
            max_bytes=_settings(request).max_upload_bytes,
            worker=_manager(request).docx_worker,
        )
    except AttachmentError as exc:
        headers = _BUSY_RETRY_AFTER if exc.status_code == 503 else None
        raise ApiError(exc.status_code, exc.code, exc.message, headers=headers) from exc
    finally:
        await attachment.close()


async def _reject_multiple_attachments(request: Request) -> None:
    form = await request.form()
    uploads = form.getlist("attachment")
    if len(uploads) <= 1:
        return
    for upload in uploads:
        if isinstance(upload, StarletteUploadFile):
            await upload.close()
    raise ApiError(
        422,
        "multiple_attachments",
        "Only one DOCX attachment is allowed per message.",
    )


@router.post("/api/threads")
async def create_thread(
    request: Request,
    principal: CsrfPrincipal,
    db: Db,
    text: Annotated[str, Form()] = "",
    client_request_id: Annotated[str, Form()] = "",
    owner_id: Annotated[str, Form()] = "",
    mode: Annotated[ThreadModeInput | None, Form()] = None,
    attachment: Annotated[UploadFile | None, File()] = None,
    attachment_role: Annotated[AttachmentRoleInput, Form()] = "source",
) -> dict[str, str]:
    if owner_id and owner_id != principal.user.id:
        raise ApiError(403, "forbidden", "You cannot create a conversation for that account.")
    await _reject_multiple_attachments(request)
    validated = await _validated_upload(request, attachment)
    try:
        result = await _manager(request).submit_turn(
            requester_id=principal.user.id,
            client_request_id=client_request_id,
            text=text,
            attachment=validated,
            attachment_role=attachment_role,
            owner_id=principal.user.id,
            mode=mode,
        )
    except GenerationError as exc:
        raise ApiError(exc.status_code, exc.code, exc.message) from exc
    return {"thread_id": result.thread_id, "generation_id": result.generation_id}


@router.post("/api/threads/{thread_id}/messages")
async def submit_message(
    thread_id: str,
    request: Request,
    principal: CsrfPrincipal,
    db: Db,
    text: Annotated[str, Form()] = "",
    client_request_id: Annotated[str, Form()] = "",
    attachment: Annotated[UploadFile | None, File()] = None,
    attachment_role: Annotated[AttachmentRoleInput, Form()] = "source",
) -> dict[str, str]:
    _require_thread(principal, await db.get(Thread, thread_id))
    await _reject_multiple_attachments(request)
    validated = await _validated_upload(request, attachment)
    try:
        result = await _manager(request).submit_turn(
            requester_id=principal.user.id,
            client_request_id=client_request_id,
            text=text,
            attachment=validated,
            attachment_role=attachment_role,
            thread_id=thread_id,
        )
    except GenerationError as exc:
        raise ApiError(exc.status_code, exc.code, exc.message) from exc
    return {"thread_id": result.thread_id, "generation_id": result.generation_id}


@router.patch("/api/threads/{thread_id}")
async def rename_thread(
    thread_id: str, body: RenameBody, principal: CsrfPrincipal, db: Db
) -> dict[str, Any]:
    thread = _require_thread(principal, await db.get(Thread, thread_id))
    title = body.title.strip()
    if not title:
        raise ApiError(422, "invalid_title", "Enter a conversation title.")
    thread.title = title
    await db.commit()
    return await _thread_summary(db, thread)


@router.delete("/api/threads/{thread_id}", status_code=204)
async def delete_thread(thread_id: str, request: Request, principal: CsrfPrincipal, db: Db) -> None:
    thread = _require_thread(principal, await db.get(Thread, thread_id))
    await _manager(request).cancel_thread(thread.id)
    storage_names = tuple(
        (
            await db.execute(
                select(Attachment.storage_name)
                .join(Message, Attachment.message_id == Message.id)
                .where(Message.thread_id == thread.id)
            )
        ).scalars()
    )
    await db.execute(delete(Thread).where(Thread.id == thread.id))
    await db.commit()
    attachment_dir = _settings(request).attachments_dir.resolve()
    for storage_name in storage_names:
        candidate = (attachment_dir / storage_name).resolve()
        if candidate.parent == attachment_dir:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                # The database deletion is authoritative. Startup's conservative orphan
                # sweep retries generated files that could not be removed immediately.
                pass


@router.get("/api/threads/{thread_id}/document.docx")
async def download_document(
    thread_id: str, request: Request, principal: Principal, db: Db
) -> Response:
    thread = _require_thread(principal, await db.get(Thread, thread_id))
    version = await _latest_work_version(db, thread.id)
    if (
        version is None
        or version.docx_template_attachment_id is None
        or version.docx_blocks is None
    ):
        raise ApiError(
            409,
            "docx_export_unavailable",
            "This conversation has no committed DOCX document to download.",
        )
    attachment = await db.get(Attachment, version.docx_template_attachment_id)
    if attachment is None or attachment.media_type != DOCX_MEDIA_TYPE:
        raise ApiError(
            409,
            "docx_export_unavailable",
            "The DOCX template for this document is unavailable.",
        )
    attachment_root = _settings(request).attachments_dir.resolve()
    template_path = (attachment_root / attachment.storage_name).resolve()
    if template_path.parent != attachment_root:
        raise ApiError(409, "docx_export_unavailable", "The DOCX template is unavailable.")

    # Plain values only: the worker thread must not touch session-bound ORM objects.
    replacements = list(version.docx_blocks or [])
    stored_blocks = attachment.document_blocks
    expected_size = attachment.byte_count
    expected_sha256 = attachment.sha256
    max_uncompressed = docx_uncompressed_limit(_settings(request).max_upload_bytes)

    def export() -> bytes:
        template = template_path.read_bytes()
        if (
            len(template) != expected_size
            or hashlib.sha256(template).hexdigest() != expected_sha256
        ):
            raise OSError("attachment integrity mismatch")
        # A stored map from the earlier text-box-unsafe extractor fails closed here.
        return render_docx(
            template,
            replacements,
            max_uncompressed_bytes=max_uncompressed,
            stored_blocks=stored_blocks,
        )

    try:
        exported = await _manager(request).docx_worker.run(export)
    except WorkerBusy as exc:
        raise ApiError(
            503,
            "document_worker_busy",
            "Oveo is processing another document. Try again in a moment.",
            headers=_BUSY_RETRY_AFTER,
        ) from exc
    except DocxError as exc:
        if exc.code == "docx_template_outdated":
            raise ApiError(409, exc.code, exc.message) from exc
        raise ApiError(
            409,
            "docx_export_unavailable",
            "The committed DOCX document could not be reproduced safely.",
        ) from exc
    except OSError as exc:
        raise ApiError(
            409,
            "docx_export_unavailable",
            "The committed DOCX document could not be reproduced safely.",
        ) from exc

    stem = Path(attachment.original_name).stem.strip() or "document"
    ascii_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._") or "document"
    fallback = f"{ascii_stem[:120]}-oveo.docx"
    unicode_name = f"{stem[:120]}-oveo.docx"
    disposition = (
        f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(unicode_name, safe='')}"
    )
    return Response(
        content=exported,
        media_type=DOCX_MEDIA_TYPE,
        headers={
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
            "Content-Disposition": disposition,
        },
    )


@router.get("/api/generations/{generation_id}")
async def generation_snapshot(
    generation_id: str, request: Request, principal: Principal, db: Db
) -> dict[str, Any]:
    await _require_generation(db, principal, generation_id)
    snapshot = await _manager(request).get_snapshot(generation_id)
    if snapshot is None:
        raise ApiError(404, "generation_not_found", "Generation not found.")
    return snapshot


@router.get("/api/generations/{generation_id}/events")
async def generation_events(generation_id: str, request: Request) -> StreamingResponse:
    # No request-scoped session here: FastAPI would keep it (and its pooled connection)
    # until the stream ends. Authorize in a short session that closes first.
    database = _database(request)
    token = request.cookies.get(_settings(request).session_cookie_name)
    async with database.sessions() as db:
        principal = await get_session_principal(db, token)
        if principal is None:
            await db.commit()
            raise ApiError(401, "authentication_required", "Sign in to continue.")
        await _require_generation(db, principal, generation_id)
        session_id = principal.session.id
        user_id = principal.user.id

    async def session_valid() -> bool:
        async with database.sessions() as db:
            return await session_is_active(db, session_id)

    try:
        stream = _manager(request).open_event_stream(
            generation_id,
            user_id=user_id,
            session_id=session_id,
            session_valid=session_valid,
        )
    except GenerationError as exc:
        raise ApiError(exc.status_code, exc.code, exc.message) from exc
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/api/generations/{generation_id}/stop", status_code=204)
async def stop_generation(
    generation_id: str, request: Request, principal: CsrfPrincipal, db: Db
) -> None:
    await _require_generation(db, principal, generation_id)
    try:
        await _manager(request).stop(generation_id)
    except GenerationError as exc:
        raise ApiError(exc.status_code, exc.code, exc.message) from exc


@router.post("/api/generations/{generation_id}/retry")
async def retry_generation(
    generation_id: str,
    body: RetryBody,
    request: Request,
    principal: CsrfPrincipal,
    db: Db,
) -> dict[str, str]:
    await _require_generation(db, principal, generation_id)
    try:
        retry_id = await _manager(request).retry(
            generation_id=generation_id,
            requester_id=principal.user.id,
            client_request_id=body.client_request_id,
        )
    except GenerationError as exc:
        raise ApiError(exc.status_code, exc.code, exc.message) from exc
    return {"generation_id": retry_id}


@router.post("/api/threads/{thread_id}/prompt-handoff")
async def prompt_handoff(
    thread_id: str,
    body: RetryBody,
    request: Request,
    principal: CsrfPrincipal,
    db: Db,
) -> dict[str, str]:
    _require_thread(principal, await db.get(Thread, thread_id))
    try:
        generation_id = await _manager(request).submit_handoff(
            thread_id=thread_id,
            requester_id=principal.user.id,
            client_request_id=body.client_request_id,
        )
    except GenerationError as exc:
        raise ApiError(exc.status_code, exc.code, exc.message) from exc
    return {"generation_id": generation_id}


@router.get("/api/usage/lifetime")
async def usage_lifetime(request: Request, principal: Principal, db: Db) -> dict[str, str]:
    # Authentication remains required, but the displayed spend is intentionally the
    # shared Oveo total rather than an account-specific conversation attribute.
    del request, principal
    return {"formatted": format_lifetime_cost(await lifetime_total(db))}


@router.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready(db: Db) -> dict[str, str]:
    accounts = set((await db.execute(select(User.username))).scalars())
    if accounts != {"charles", "yousra"}:
        raise ApiError(503, "accounts_not_ready", "Required accounts are not configured.")
    return {"status": "ready"}
