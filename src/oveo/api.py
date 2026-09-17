from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile as StarletteUploadFile

from oveo.attachments import AttachmentError, validate_text_upload
from oveo.auth import (
    InvalidCredentials,
    LoginThrottled,
    SessionPrincipal,
    authenticate_user,
    create_session,
    get_session_principal,
    revoke_session,
    validate_csrf,
)
from oveo.authorization import may_access_thread
from oveo.config import Settings
from oveo.db import Database
from oveo.generation import GenerationError, GenerationManager
from oveo.models import Attachment, Generation, Message, Thread, User
from oveo.usage import format_lifetime_cost, lifetime_total

router = APIRouter()
ThreadModeInput = Literal["translate", "revision", "internal_comms"]


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


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
        "role": user.role,
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
async def logout(request: Request, response: Response, _: CsrfPrincipal, db: Db) -> None:
    settings = _settings(request)
    await revoke_session(db, request.cookies.get(settings.session_cookie_name))
    await db.commit()
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
                        "byte_size": attachment.byte_count,
                        "word_count": attachment.word_count,
                    }
                    if attachment is not None
                    else None
                ),
                "created_at": message.created_at.isoformat(),
            }
        )
    latest_generation = await db.scalar(
        select(Generation)
        .where(Generation.thread_id == thread.id)
        .order_by(Generation.created_at.desc())
        .limit(1)
    )
    generation_snapshot = None
    if latest_generation is not None and latest_generation.status != "completed":
        generation_snapshot = await _manager(request).get_snapshot(latest_generation.id)
    return {**summary, "messages": serialized_messages, "generation": generation_snapshot}


async def _validated_upload(request: Request, attachment: UploadFile | None) -> Any:
    if attachment is None:
        return None
    try:
        return await validate_text_upload(attachment, max_bytes=_settings(request).max_upload_bytes)
    except AttachmentError as exc:
        raise ApiError(exc.status_code, exc.code, exc.message) from exc
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
        "Only one source attachment is allowed per message.",
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
async def generation_events(
    generation_id: str, request: Request, principal: Principal, db: Db
) -> StreamingResponse:
    await _require_generation(db, principal, generation_id)
    return StreamingResponse(
        _manager(request).events(generation_id),
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
    del request
    return {
        "formatted": format_lifetime_cost(await lifetime_total(db, requester_id=principal.user.id))
    }


@router.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready(db: Db) -> dict[str, str]:
    accounts = {
        username: role
        for username, role in (await db.execute(select(User.username, User.role))).all()
    }
    if accounts != {"charles": "owner", "yousra": "user"}:
        raise ApiError(503, "accounts_not_ready", "Required accounts are not configured.")
    return {"status": "ready"}
