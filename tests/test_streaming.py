"""Server-sent events: deltas, resynchronization, pool use, revocation, limits."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy import event, select

import oveo.generation as generation_module
from oveo.auth import hash_password
from oveo.config import Settings
from oveo.db import Database
from oveo.generation import GenerationManager, ProviderCompletion, ProviderRequest
from oveo.main import create_app
from oveo.models import Base, Generation, User
from tests.test_generation_manager import _wait_status


def _events(chunk_count: int, chunk: str) -> list[bytes]:
    lines: list[dict[str, object]] = [
        {"v": 1, "event": "response_start"},
        {"v": 1, "event": "block_start", "id": "b1", "type": "deliverable"},
    ]
    lines += [{"v": 1, "event": "block_delta", "id": "b1", "text": chunk}] * chunk_count
    lines += [
        {"v": 1, "event": "block_end", "id": "b1"},
        {"v": 1, "event": "state", "operation": "none"},
        {"v": 1, "event": "response_end"},
    ]
    return [(json.dumps(line) + "\n").encode() for line in lines]


class PacedProvider:
    """Streams NDJSON lines one at a time; can hold the stream open until released."""

    def __init__(self, lines: list[bytes], *, hold: bool = False) -> None:
        self.lines = lines
        self.release = asyncio.Event()
        if not hold:
            self.release.set()
        self.streaming = asyncio.Event()

    async def generate(
        self, request: ProviderRequest, emit: Any, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        if request.purpose == "title":
            await emit(b"Streaming Test Title")
            return ProviderCompletion()
        for index, line in enumerate(self.lines):
            await emit(line)
            if index == 2:
                self.streaming.set()
                await self.release.wait()
            await asyncio.sleep(0)
        return ProviderCompletion()


def _parse_sse(raw: str) -> list[tuple[str, dict[str, Any]]]:
    parsed = []
    for frame in raw.split("\n\n"):
        name = None
        data = None
        for line in frame.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name and data is not None:
            parsed.append((name, data))
    return parsed


def _replay(events: list[tuple[str, dict[str, Any]]]) -> str:
    """Rebuild the deliverable exactly as the browser does, checking sequences."""

    blocks: list[list[str]] = []
    last_seq = None
    for name, data in events:
        if name == "snapshot":
            blocks = [[block["text"]] for block in data["blocks"]]
            last_seq = data["seq"]
            continue
        assert data["from"] == last_seq + 1  # no gap, no overlap
        for op in data["ops"]:
            if op["op"] == "start":
                blocks.append([])
            elif op["op"] == "append":
                blocks[-1].append(op["text"])
        last_seq = data["seq"]
    return "".join("".join(parts) for parts in blocks)


async def _always_valid() -> bool:
    return True


async def test_stream_sends_one_snapshot_then_small_ordered_deltas(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    chunk = "x" * 100
    provider = PacedProvider(_events(300, chunk), hold=True)
    manager = GenerationManager(database, settings, provider)
    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="stream",
        text="Translate.",
        attachment=None,
        owner_id=user.id,
        mode="translate",
    )
    await provider.streaming.wait()
    stream = manager.open_event_stream(
        submitted.generation_id, user_id=user.id, session_id="s", session_valid=_always_valid
    )
    received: list[str] = [await anext(stream)]
    provider.release.set()
    async for frame in stream:
        received.append(frame)
    events = _parse_sse("".join(received))

    assert events[0][0] == "snapshot"
    assert events[-1][0] == "snapshot" and events[-1][1]["status"] == "completed"
    assert _replay(events[:-1]) == chunk * 300
    # Each delta carries only new text: the stream is proportional to the content
    # (it used to resend the whole draft for every delta, ~150x for this size).
    streamed = sum(len(frame) for frame in received)
    assert streamed < 3 * len(chunk) * 300
    async with database.sessions() as db:
        row = await db.get(Generation, submitted.generation_id)
        assert row is not None and row.stream_revision <= 4  # no per-delta database writes
    await manager.shutdown()


async def test_a_reader_that_falls_behind_resynchronizes_from_a_snapshot(
    manager_database: tuple[Database, Settings, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings, user = manager_database
    monkeypatch.setattr(generation_module, "_STREAM_QUEUE_SIZE", 2)
    provider = PacedProvider(_events(50, "yz"), hold=True)
    manager = GenerationManager(database, settings, provider)
    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="slow-reader",
        text="Translate.",
        attachment=None,
        owner_id=user.id,
        mode="translate",
    )
    await provider.streaming.wait()
    stream = manager.open_event_stream(
        submitted.generation_id, user_id=user.id, session_id="s", session_valid=_always_valid
    )
    received = [await anext(stream)]
    # Let the whole response arrive while this reader is not reading.
    provider.release.set()
    await _wait_status(manager, submitted.generation_id, {"completed"})
    async for frame in stream:
        received.append(frame)
    events = _parse_sse("".join(received))
    final = events[-1]
    assert final[0] == "snapshot"
    assert final[1]["blocks"] == [{"type": "deliverable", "text": "yz" * 50}]
    await manager.shutdown()


async def test_logout_and_revoked_sessions_end_open_streams(
    manager_database: tuple[Database, Settings, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings, user = manager_database
    monkeypatch.setattr(generation_module, "_SESSION_CHECK_SECONDS", 0.1)
    provider = PacedProvider(_events(5, "never finishes"), hold=True)
    manager = GenerationManager(database, settings, provider)
    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="revoked",
        text="Translate.",
        attachment=None,
        owner_id=user.id,
        mode="translate",
    )
    await provider.streaming.wait()

    closed_by_logout = manager.open_event_stream(
        submitted.generation_id, user_id=user.id, session_id="a", session_valid=_always_valid
    )
    await anext(closed_by_logout)
    manager.close_session_streams("a")
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(closed_by_logout), 1)

    valid = {"value": True}

    async def session_valid() -> bool:
        return valid["value"]

    revoked = manager.open_event_stream(
        submitted.generation_id, user_id=user.id, session_id="b", session_valid=session_valid
    )
    await anext(revoked)
    valid["value"] = False  # e.g. a password reset from the admin command
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(revoked), 2)
    assert manager._user_streams[user.id] == 0
    provider.release.set()
    await manager.shutdown()


async def test_each_user_has_a_bounded_number_of_open_streams(
    manager_database: tuple[Database, Settings, User],
) -> None:
    database, settings, user = manager_database
    provider = PacedProvider(_events(5, "open"), hold=True)
    manager = GenerationManager(database, settings, provider)
    submitted = await manager.submit_turn(
        requester_id=user.id,
        client_request_id="cap",
        text="Translate.",
        attachment=None,
        owner_id=user.id,
        mode="translate",
    )
    await provider.streaming.wait()
    streams = []
    for index in range(generation_module._MAX_STREAMS_PER_USER):
        stream = manager.open_event_stream(
            submitted.generation_id,
            user_id=user.id,
            session_id=f"s{index}",
            session_valid=_always_valid,
        )
        await anext(stream)
        streams.append(stream)
    with pytest.raises(generation_module.GenerationError) as caught:
        manager.open_event_stream(
            submitted.generation_id, user_id=user.id, session_id="x", session_valid=_always_valid
        )
    assert caught.value.status_code == 429
    for stream in streams:
        await stream.aclose()  # type: ignore[attr-defined]
    assert manager._user_streams[user.id] == 0
    provider.release.set()
    await manager.shutdown()


# --- A real server: open streams must not hold pooled database connections ----------


async def _prepared_database(path: Path) -> Database:
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


class NeverFinishes:
    async def generate(
        self, request: ProviderRequest, emit: Any, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del request
        await emit(b'{"v":1,"event":"response_start"}\n')
        await cancel_event.wait()
        raise asyncio.CancelledError


async def test_many_open_streams_leave_the_pool_free_for_other_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # More open streams than the pool has connections (5 + 10 overflow).
    monkeypatch.setattr(generation_module, "_MAX_STREAMS_PER_USER", 20)
    database = await _prepared_database(tmp_path / "pool.sqlite3")
    checked_out = {"now": 0, "max": 0}

    @event.listens_for(database.engine.sync_engine, "checkout")
    def on_checkout(*_args: object) -> None:
        checked_out["now"] += 1
        checked_out["max"] = max(checked_out["max"], checked_out["now"])

    @event.listens_for(database.engine.sync_engine, "checkin")
    def on_checkin(*_args: object) -> None:
        checked_out["now"] -= 1

    settings = Settings(
        environment="test",
        public_origin="http://testserver",
        trusted_hosts=["testserver"],
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'pool.sqlite3'}",
        attachments_dir=tmp_path / "attachments",
        frontend_dir=tmp_path / "frontend",
        secure_cookies=False,
    )
    app = create_app(settings=settings, database=database, provider=NeverFinishes())
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical", lifespan="on")
    )
    serving = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    streams: list[tuple[Any, httpx.Response, AsyncIterator[str], asyncio.Task[None]]] = []
    try:
        limits = httpx.Limits(max_connections=100, max_keepalive_connections=50)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Host": "testserver"},
            timeout=10,
            limits=limits,
        ) as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "charles", "password": "charles password"},
                headers={"Origin": "http://testserver"},
            )
            headers = {"X-CSRF-Token": login.json()["csrf_token"], "Origin": "http://testserver"}
            created = (
                await client.post(
                    "/api/threads",
                    data={"mode": "translate", "text": "x", "client_request_id": "c"},
                    headers=headers,
                )
            ).json()
            generation_id = created["generation_id"]
            url = f"/api/generations/{generation_id}/events"

            async def drain(lines: AsyncIterator[str]) -> None:
                async for _line in lines:
                    pass

            # Two sign-in sessions of the same user, ten streams each.
            second = httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                headers={"Host": "testserver"},
                timeout=10,
                limits=limits,
            )
            await second.post(
                "/api/auth/login",
                json={"username": "charles", "password": "charles password"},
                headers={"Origin": "http://testserver"},
            )
            opened = 0
            for owner in (client, second):
                for _ in range(10):
                    context = owner.stream("GET", url)
                    response = await asyncio.wait_for(context.__aenter__(), 5)
                    assert response.status_code == 200
                    assert response.headers["content-type"].startswith("text/event-stream")
                    lines = response.aiter_lines()
                    first = await asyncio.wait_for(anext(lines), 5)
                    assert first == "event: snapshot"
                    streams.append((context, response, lines, asyncio.create_task(drain(lines))))
                    opened += 1
            await asyncio.sleep(0.3)
            assert all(not task.done() for *_rest, task in streams)  # readers are alive
            # The audit measured one checked-out connection per open stream here.
            assert checked_out["now"] <= 1
            started = time.perf_counter()
            listing = await client.get("/api/threads")
            assert listing.status_code == 200
            assert time.perf_counter() - started < 2
            async with database.sessions() as db:
                status = await db.scalar(
                    select(Generation.status).where(Generation.id == generation_id)
                )
            assert status == "running"
            assert opened == 20
            for context, *_rest, task in streams:
                task.cancel()
                await context.__aexit__(None, None, None)
            await second.aclose()
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 30)
        await database.dispose()
    assert checked_out["now"] == 0


class Trickle:
    """Streams a text delta every 20 ms until stopped."""

    async def generate(
        self, request: ProviderRequest, emit: Any, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del request
        await emit(
            b'{"v":1,"event":"response_start"}\n'
            b'{"v":1,"event":"block_start","id":"b1","type":"deliverable"}\n'
        )
        index = 0
        while not cancel_event.is_set():
            await emit(
                f'{{"v":1,"event":"block_delta","id":"b1","text":"part {index} "}}\n'.encode()
            )
            index += 1
            await asyncio.sleep(0.02)
        raise asyncio.CancelledError


async def test_logout_over_http_closes_the_signed_out_browsers_stream(
    tmp_path: Path,
) -> None:
    database = await _prepared_database(tmp_path / "logout.sqlite3")
    settings = Settings(
        environment="test",
        public_origin="http://testserver",
        trusted_hosts=["testserver"],
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'logout.sqlite3'}",
        attachments_dir=tmp_path / "attachments",
        frontend_dir=tmp_path / "frontend",
        secure_cookies=False,
    )
    app = create_app(settings=settings, database=database, provider=Trickle())
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical", lifespan="on")
    )
    serving = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers={"Host": "testserver"}, timeout=10
        ) as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "charles", "password": "charles password"},
                headers={"Origin": "http://testserver"},
            )
            headers = {"X-CSRF-Token": login.json()["csrf_token"], "Origin": "http://testserver"}
            created = (
                await client.post(
                    "/api/threads",
                    data={"mode": "translate", "text": "x", "client_request_id": "c"},
                    headers=headers,
                )
            ).json()
            async with client.stream(
                "GET", f"/api/generations/{created['generation_id']}/events"
            ) as response:
                lines = response.aiter_lines()
                async for line in lines:
                    if '"op":"append"' in line:
                        break  # content is flowing
                logout = await client.post("/api/auth/logout", headers=headers)
                assert logout.status_code == 204
                after_logout: list[str] = []

                async def read_rest() -> None:
                    async for line in lines:
                        after_logout.append(line)

                await asyncio.wait_for(read_rest(), 5)  # the server ended the stream
            # Before this fix the stream kept delivering content until the response
            # ended (38 events in the audit probe). Only deltas already in flight may
            # still arrive now.
            assert sum('"op":"append"' in line for line in after_logout) <= 3
            reopened = await client.get(f"/api/generations/{created['generation_id']}/events")
            assert reopened.status_code == 401
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 30)
        await database.dispose()
