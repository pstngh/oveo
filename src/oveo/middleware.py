"""Small pure-ASGI guards that run before routing and body parsing."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from oveo.diagnostics import log_unexpected

_LOGGER = logging.getLogger("oveo.http")
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# The only routes that accept a DOCX upload (multipart form data).
_UPLOAD_ROUTES = re.compile(r"/api/threads(?:/[^/]+/messages)?")
# Everything else is a small JSON body or no body at all.
SMALL_BODY_LIMIT = 16 * 1024
# Multipart framing and the other form fields around the file itself.
UPLOAD_OVERHEAD = 256 * 1024


def _error(status_code: int, code: str, message: str, **headers: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message},
        headers=headers or None,
    )


class RequestBodyLimit:
    """Reject oversized bodies before anything buffers or parses them.

    A declared ``Content-Length`` over the limit is refused without reading; a chunked
    body is counted as it streams and stopped at the limit with the same 413.
    """

    def __init__(self, app: ASGIApp, *, max_upload_bytes: int) -> None:
        self.app = app
        self.upload_limit = max_upload_bytes + UPLOAD_OVERHEAD

    def _limit(self, scope: Scope) -> int:
        if scope["method"] == "POST" and _UPLOAD_ROUTES.fullmatch(scope["path"]):
            return self.upload_limit
        return SMALL_BODY_LIMIT

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope)
        declared = [value for name, value in scope["headers"] if name == b"content-length"]
        if declared:
            try:
                lengths = {int(value) for value in declared}
            except ValueError:
                lengths = {-1}
            if len(lengths) != 1 or min(lengths) < 0:
                await _error(400, "invalid_request", "The request is invalid.")(
                    scope, receive, send
                )
                return
            if lengths.pop() > limit:
                await _too_large(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # FastAPI re-raises HTTPException from body parsing unchanged.
                    raise HTTPException(status_code=413)
            return message

        await self.app(scope, limited_receive, send)


async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
    await _error(413, "request_too_large", "The request is too large.")(scope, receive, send)


class MaintenanceGate:
    """Refuse state-changing API requests while a deployment verifies a candidate.

    The deployment creates the marker file before starting a release that changes the
    database schema and removes it after every readiness check passed, so a rollback
    never discards user writes. Reads, event streams and health checks keep working.
    """

    def __init__(self, app: ASGIApp, *, marker: Path) -> None:
        self.app = app
        self.marker = marker

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] in _UNSAFE_METHODS
            and scope["path"].startswith("/api/")
            and self.marker.exists()
        ):
            await _error(
                503,
                "maintenance",
                "Oveo is being updated. Try again in a minute.",
                **{"Retry-After": "30"},
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


class ContentFreeErrors:
    """Turn unexpected exceptions into the opaque 500 without re-raising them.

    Starlette's own handler re-raises after responding, which makes the server print
    the full traceback and exception message to the container log. Here only the
    content-free record from `log_unexpected` is written.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception as exc:
            error_id = log_unexpected(_LOGGER, exc, area="http")
            if started:
                return  # The response is already under way; the server closes it.
            response = JSONResponse(
                status_code=500,
                content={
                    "code": "internal_error",
                    "message": "The request could not be completed.",
                    "error_id": error_id,
                },
            )
            await response(scope, receive, send)


class ImmutableStaticFiles(StaticFiles):
    """Serve content-hashed build assets as cacheable forever."""

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response
