"""Request limits, sign-in protection, caching, configuration, and query shape."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import event, select

from oveo.auth import hash_password
from oveo.config import Settings
from oveo.db import Database
from oveo.generation import GenerationManager, ProviderCompletion, ProviderRequest
from oveo.main import create_app
from oveo.middleware import SMALL_BODY_LIMIT, UPLOAD_OVERHEAD
from oveo.models import Base, Message, Thread, User

ROOT = Path(__file__).resolve().parents[1]
_SUCCESS = (
    b'{"v":1,"event":"response_start"}\n'
    b'{"v":1,"event":"block_start","id":"b1","type":"deliverable"}\n'
    b'{"v":1,"event":"block_delta","id":"b1","text":"Bonjour"}\n'
    b'{"v":1,"event":"block_end","id":"b1"}\n'
    b'{"v":1,"event":"state","operation":"none"}\n'
    b'{"v":1,"event":"response_end"}\n'
)


class Immediate:
    async def generate(
        self, request: ProviderRequest, emit: Any, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del cancel_event
        await emit(b"Synthetic Title" if request.purpose == "title" else _SUCCESS)
        return ProviderCompletion()


async def _database(path: Path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{path}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with database.sessions() as db:
        for username in ("charles", "yousra"):
            db.add(
                User(
                    username=username,
                    display_name=username.title(),
                    password_hash=hash_password(f"{username} password"),
                )
            )
        await db.commit()
    return database


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "public_origin": "http://testserver",
        "trusted_hosts": ["testserver"],
        "data_dir": tmp_path,
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'http.sqlite3'}",
        "attachments_dir": tmp_path / "attachments",
        "frontend_dir": tmp_path / "frontend",
        "secure_cookies": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    database = await _database(tmp_path / "http.sqlite3")
    app = create_app(settings=_settings(tmp_path), database=database, provider=Immediate())
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            c.app = app  # type: ignore[attr-defined]
            yield c
    await database.dispose()


async def _login(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/auth/login",
        json={"username": "charles", "password": "charles password"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"], "Origin": "http://testserver"}


# --- H-4: request bodies are bounded before anything reads or parses them ------------


async def test_declared_oversized_body_is_refused_without_reading_it(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/auth/login",
        content=b'{"username":"x","password":"y"}',
        headers={"Content-Type": "application/json", "Content-Length": str(100 * 1024 * 1024)},
    )
    assert response.status_code == 413
    assert response.json() == {"code": "request_too_large", "message": "The request is too large."}


async def test_streamed_body_is_cut_off_at_the_limit(client: httpx.AsyncClient) -> None:
    sent = {"bytes": 0}

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(64):
            sent["bytes"] += 8 * 1024
            yield b"a" * (8 * 1024)

    response = await client.post(
        "/api/auth/login", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"


async def test_invalid_content_length_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/auth/login",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": "-5"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


async def test_upload_routes_allow_the_configured_file_size_and_no_more(
    client: httpx.AsyncClient,
) -> None:
    settings: Settings = client.app.state.settings  # type: ignore[attr-defined]
    upload_limit = settings.max_upload_bytes + UPLOAD_OVERHEAD
    # Unauthenticated: refused by size before the form is parsed (it used to be parsed
    # in full, up to 1,000 fields of 1 MiB, before the 401).
    too_large = await client.post(
        "/api/threads",
        content=b"x" * 16,
        headers={
            "Content-Type": "multipart/form-data; boundary=abc",
            "Content-Length": str(upload_limit + 1),
        },
    )
    assert too_large.status_code == 413
    # Other routes keep the small JSON limit.
    rename = await client.patch(
        "/api/threads/any",
        content=b"x" * 16,
        headers={"Content-Type": "application/json", "Content-Length": str(SMALL_BODY_LIMIT + 1)},
    )
    assert rename.status_code == 413
    headers = await _login(client)
    malformed = await client.post(
        "/api/threads",
        content=b"--wrong\r\nnot a valid part",
        headers={**headers, "Content-Type": "multipart/form-data; boundary=abc"},
    )
    assert malformed.status_code == 400
    assert malformed.json() == {"code": "invalid_request", "message": "The request is invalid."}


# --- M-1: sign-in work is bounded and the lockout cannot be raced ---------------------


async def test_concurrent_wrong_passwords_cannot_bypass_the_lockout(
    client: httpx.AsyncClient,
) -> None:
    async def attempt(index: int) -> int:
        response = await client.post(
            "/api/auth/login",
            json={"username": "charles", "password": f"wrong-guess-{index}"},
            headers={"Origin": "http://testserver"},
        )
        return response.status_code

    codes = await asyncio.gather(*(attempt(index) for index in range(20)))
    assert codes.count(401) <= 5  # the audit saw all 20 evaluated
    assert set(codes) <= {401, 429}
    database: Database = client.app.state.database  # type: ignore[attr-defined]
    async with database.sessions() as db:
        user = await db.scalar(select(User).where(User.username == "charles"))
        assert user is not None
        assert user.failed_login_count == codes.count(401)
    # Keep guessing until the lock engages; it must engage at five failures.
    for index in range(20, 26):
        await attempt(index)
    async with database.sessions() as db:
        user = await db.scalar(select(User).where(User.username == "charles"))
        assert user is not None and user.login_locked_until is not None
    correct = await client.post(
        "/api/auth/login",
        json={"username": "charles", "password": "charles password"},
        headers={"Origin": "http://testserver"},
    )
    assert correct.status_code == 429
    assert int(correct.headers["retry-after"]) > 0


async def test_failures_from_one_client_are_refused_before_any_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = await _database(tmp_path / "http.sqlite3")
    app = create_app(
        settings=_settings(tmp_path, login_client_failure_limit=5),
        database=database,
        provider=Immediate(),
    )
    checks = {"count": 0}
    guard = app.state.login_guard
    original = guard.verify

    async def counted(password_hash: str, password: str) -> bool:
        checks["count"] += 1
        return await original(password_hash, password)

    monkeypatch.setattr(guard, "verify", counted)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            for index in range(5):
                failed = await c.post(
                    "/api/auth/login",
                    json={"username": f"nobody-{index}", "password": "x"},
                    headers={"Origin": "http://testserver"},
                )
                assert failed.status_code == 401
            refused = await c.post(
                "/api/auth/login",
                json={"username": "yousra", "password": "yousra password"},
                headers={"Origin": "http://testserver"},
            )
    await database.dispose()
    assert refused.status_code == 429
    assert refused.json()["code"] == "login_throttled"
    assert int(refused.headers["retry-after"]) > 0
    assert checks["count"] == 5  # the refused attempt never reached Argon2


async def test_busy_password_checker_refuses_instead_of_queueing(
    client: httpx.AsyncClient,
) -> None:
    guard = client.app.state.login_guard  # type: ignore[attr-defined]
    guard._worker._pending = 4  # one running and three waiting
    try:
        response = await client.post(
            "/api/auth/login",
            json={"username": "charles", "password": "charles password"},
            headers={"Origin": "http://testserver"},
        )
    finally:
        guard._worker._pending = 0
    assert response.status_code == 429
    assert response.json()["code"] == "login_busy"
    assert response.headers["retry-after"] == "1"


async def test_password_checks_leave_the_event_loop_responsive(
    client: httpx.AsyncClient,
) -> None:
    async def unknown_login(index: int) -> None:
        await client.post(
            "/api/auth/login",
            json={"username": f"unknown-{index}", "password": "x"},
            headers={"Origin": "http://testserver"},
        )

    logins = asyncio.gather(*(unknown_login(index) for index in range(4)))
    await asyncio.sleep(0.02)
    started = time.perf_counter()
    health = await client.get("/health/live")
    elapsed = time.perf_counter() - started
    await logins
    assert health.status_code == 200
    # Argon2 runs on its own thread; the audit measured 0.7-1.4 s here on one CPU.
    assert elapsed < 0.25


# --- L-5, M-3 gate ---------------------------------------------------------------------


async def test_shell_revalidates_while_hashed_assets_are_cached(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<!doctype html><title>Oveo</title>", encoding="utf-8")
    (frontend / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    database = await _database(tmp_path / "http.sqlite3")
    app = create_app(settings=_settings(tmp_path), database=database, provider=Immediate())
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            shell = await c.get("/conversations/some-thread")
            asset = await c.get("/assets/index-abc123.js")
            api = await c.get("/api/usage/lifetime")
    await database.dispose()
    assert shell.headers["cache-control"] == "no-cache"
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert api.headers["cache-control"] == "no-store"


async def test_maintenance_marker_holds_writes_but_not_reads(
    client: httpx.AsyncClient,
) -> None:
    settings: Settings = client.app.state.settings  # type: ignore[attr-defined]
    settings.maintenance_marker.write_text("", encoding="utf-8")
    try:
        held = await client.post(
            "/api/auth/login",
            json={"username": "charles", "password": "charles password"},
            headers={"Origin": "http://testserver"},
        )
        assert held.status_code == 503
        assert held.json()["code"] == "maintenance"
        assert held.headers["retry-after"] == "30"
        assert (await client.get("/health/ready")).status_code == 200
        assert (await client.get("/api/threads")).status_code == 401  # reads unaffected
    finally:
        settings.maintenance_marker.unlink()
    await _login(client)


# --- L-13: configuration, not the working directory --------------------------------------


async def test_prompts_come_from_the_configured_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts = tmp_path / "custom-prompts"
    shutil.copytree(ROOT / "prompts", prompts)
    with (prompts / "protocol.md").open("a", encoding="utf-8") as handle:
        handle.write("\nCONFIGURED-PROMPT-MARKER\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # no ./prompts here
    settings = _settings(tmp_path, prompts_dir=prompts)
    database = await _database(tmp_path / "http.sqlite3")
    manager = GenerationManager(database, settings, Immediate())
    async with database.sessions() as db:
        user = await db.scalar(select(User).where(User.username == "charles"))
        assert user is not None
        thread = Thread(owner_id=user.id, mode="translate")
        db.add(thread)
        await db.flush()
        snapshot = await manager._request_snapshot(db, thread, purpose="chat")
    assert "CONFIGURED-PROMPT-MARKER" in snapshot["provider_messages"][0]["content"]
    await manager.shutdown()
    await database.dispose()


def test_importing_the_application_creates_no_directories(tmp_path: Path) -> None:
    # Coverage variables are dropped too: the child would otherwise write statement-only
    # data that cannot be combined with this run's branch data.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OVEO_", "COV_CORE_", "COVERAGE_"))
    }
    environment["PYTHONPATH"] = str(ROOT / "src")
    subprocess.run(
        [sys.executable, "-c", "import oveo.main"],
        cwd=tmp_path,
        env=environment,
        check=True,
        timeout=60,
    )
    assert list(tmp_path.iterdir()) == []


# --- L-2: list and history payload shape -------------------------------------------------


@pytest.fixture
def statements(client: httpx.AsyncClient) -> Iterator[list[str]]:
    executed: list[str] = []
    database: Database = client.app.state.database  # type: ignore[attr-defined]

    def record(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        executed.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        yield executed
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record)


async def test_thread_list_is_one_query_regardless_of_size(
    client: httpx.AsyncClient, statements: list[str]
) -> None:
    await _login(client)
    database: Database = client.app.state.database  # type: ignore[attr-defined]
    async with database.sessions() as db:
        user = await db.scalar(select(User).where(User.username == "charles"))
        assert user is not None
        db.add(Thread(owner_id=user.id, mode="translate", title="One"))
        await db.commit()
    statements.clear()
    assert len((await client.get("/api/threads")).json()) == 1
    small = len(statements)
    async with database.sessions() as db:
        db.add_all(
            [Thread(owner_id=user.id, mode="revision", title=f"T{index}") for index in range(40)]
        )
        await db.commit()
    statements.clear()
    listing = (await client.get("/api/threads")).json()
    assert len(listing) == 41
    assert len(statements) == small  # the audit counted 2N+2 statements
    assert all(item["owner_username"] == "charles" for item in listing)


async def test_history_refresh_returns_only_newer_messages(client: httpx.AsyncClient) -> None:
    await _login(client)
    database: Database = client.app.state.database  # type: ignore[attr-defined]
    async with database.sessions() as db:
        user = await db.scalar(select(User).where(User.username == "charles"))
        assert user is not None
        thread = Thread(owner_id=user.id, mode="translate", title="History")
        db.add(thread)
        await db.flush()
        for ordinal in range(1, 5):
            role = "user" if ordinal % 2 else "assistant"
            db.add(
                Message(
                    thread_id=thread.id,
                    ordinal=ordinal,
                    role=role,
                    actor_user_id=user.id if role == "user" else None,
                    content=[{"type": "conversation", "text": f"Message {ordinal}"}],
                )
            )
        await db.commit()
        thread_id = thread.id
    full = (await client.get(f"/api/threads/{thread_id}")).json()
    assert [message["ordinal"] for message in full["messages"]] == [1, 2, 3, 4]
    newer = (await client.get(f"/api/threads/{thread_id}?after_ordinal=2")).json()
    assert [message["ordinal"] for message in newer["messages"]] == [3, 4]
    assert newer["title"] == "History"
    invalid = await client.get(f"/api/threads/{thread_id}?after_ordinal=-1")
    assert invalid.status_code == 422
