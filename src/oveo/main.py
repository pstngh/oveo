from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from argon2 import extract_parameters
from argon2.exceptions import InvalidHashError
from argon2.low_level import Type
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.trustedhost import TrustedHostMiddleware

from oveo.api import ApiError, router
from oveo.config import Settings, get_settings
from oveo.db import Database
from oveo.generation import (
    GenerationManager,
    GenerationProvider,
    OpenRouterProvider,
    UnavailableProvider,
)
from oveo.models import User


def _validated_argon2id_hash(encoded: str, *, username: str) -> str:
    try:
        parameters = extract_parameters(encoded)
    except (InvalidHashError, ValueError) as exc:
        raise RuntimeError(
            f"configured password hash for {username} is not a valid Argon2id hash"
        ) from exc
    if parameters.type is not Type.ID:
        raise RuntimeError(f"configured password hash for {username} is not a valid Argon2id hash")
    return encoded


def _is_missing_or_placeholder(value: str | None) -> bool:
    if value is None or not value.strip():
        return True
    lowered = value.casefold()
    return any(marker in lowered for marker in ("replace", "placeholder", "changeme"))


def validate_production_settings(settings: Settings) -> None:
    """Fail closed before readiness when required production secrets are absent."""

    if not settings.production:
        return
    api_key = (
        settings.openrouter_api_key.get_secret_value()
        if settings.openrouter_api_key is not None
        else None
    )
    if _is_missing_or_placeholder(api_key):
        raise RuntimeError("a non-placeholder OpenRouter API key is required in production")
    for username, configured_hash in (
        ("charles", settings.charles_password_hash),
        ("yousra", settings.yousra_password_hash),
    ):
        if configured_hash is None:
            raise RuntimeError(f"a password hash for {username} is required in production")
        _validated_argon2id_hash(configured_hash.get_secret_value(), username=username)


async def seed_configured_accounts(database: Database, settings: Settings) -> None:
    """Create missing permanent accounts without overwriting an admin-reset password."""

    configured = (
        ("charles", "Charles", "owner", settings.charles_password_hash),
        ("yousra", "Yousra", "user", settings.yousra_password_hash),
    )
    async with database.sessions() as db:
        for username, display_name, role, secret_hash in configured:
            encoded = (
                _validated_argon2id_hash(secret_hash.get_secret_value(), username=username)
                if secret_hash is not None
                else None
            )
            user = await db.scalar(select(User).where(User.username == username))
            if user is None:
                if encoded is None:
                    continue
                db.add(
                    User(
                        username=username,
                        display_name=display_name,
                        role=role,
                        password_hash=encoded,
                    )
                )
            else:
                # Role/display metadata are configuration-owned. The hash is deliberately
                # left alone so `oveo-admin reset-password` survives a service restart.
                user.display_name = display_name
                user.role = role
        await db.commit()


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    provider: GenerationProvider | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    app_database = database or Database(app_settings.database_url)
    owns_database = database is None
    if provider is None:
        provider = (
            OpenRouterProvider(app_settings)
            if app_settings.openrouter_api_key is not None
            else UnavailableProvider()
        )
    manager = GenerationManager(app_database, app_settings, provider)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        validate_production_settings(app_settings)
        app_settings.ensure_directories()
        await seed_configured_accounts(app_database, app_settings)
        await manager.reconcile_orphans()
        await manager.reconcile_pending_costs()
        await manager.sweep_orphan_attachments()
        try:
            yield
        finally:
            await manager.shutdown()
            if owns_database:
                await app_database.dispose()

    app = FastAPI(
        title="Oveo",
        version="2.0.0",
        docs_url=None if app_settings.production else "/api/docs",
        redoc_url=None,
        openapi_url=None if app_settings.production else "/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.database = app_database
    app.state.generation_manager = manager
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=app_settings.trusted_hosts)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: object) -> object:
        response = await call_next(request)  # type: ignore[operator]
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        if app_settings.production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"code": "invalid_request", "message": "The request is invalid."},
        )

    @app.exception_handler(Exception)
    async def internal_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
        # Do not stringify arbitrary exceptions: provider/SQL errors can retain private
        # payload or parameter data. Operational correlation belongs in opaque IDs.
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "message": "The request could not be completed."},
        )

    app.include_router(router)

    frontend_dir = app_settings.frontend_dir
    assets_dir = frontend_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    async def spa(path: str) -> Response:
        if path.startswith("api/") or path.startswith("health/"):
            return JSONResponse(
                status_code=404,
                content={"code": "not_found", "message": "Endpoint not found."},
            )
        index = Path(frontend_dir) / "index.html"
        if not index.is_file():
            return JSONResponse(
                status_code=404,
                content={"code": "frontend_unavailable", "message": "Frontend is unavailable."},
            )
        return FileResponse(index)

    return app


app = create_app()
