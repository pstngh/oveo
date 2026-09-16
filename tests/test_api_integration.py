from __future__ import annotations

import asyncio
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from oveo.auth import change_password, hash_password, verify_password
from oveo.config import Settings
from oveo.db import Database
from oveo.generation import ProviderCompletion, ProviderRequest
from oveo.main import create_app, seed_configured_accounts, validate_production_settings
from oveo.models import Base, User

_SUCCESS = (
    b'{"v":1,"event":"response_start"}\n'
    b'{"v":1,"event":"block_start","id":"b1","type":"deliverable"}\n'
    b'{"v":1,"event":"block_delta","id":"b1","text":"Bonjour monde"}\n'
    b'{"v":1,"event":"block_end","id":"b1"}\n'
    b'{"v":1,"event":"state","operation":"none"}\n'
    b'{"v":1,"event":"response_end"}\n'
)


class ImmediateProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del cancel_event
        self.calls += 1
        content = b"Canadian French Translation" if request.purpose == "title" else _SUCCESS
        await emit(content)  # type: ignore[operator]
        return ProviderCompletion(
            provider_request_id=f"provider-api-{self.calls}", cost_microusd=123
        )


async def _prepare_database(database: Database) -> None:
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with database.sessions() as db:
        db.add_all(
            [
                User(
                    username="charles",
                    display_name="Charles",
                    role="owner",
                    password_hash=hash_password("charles password"),
                ),
                User(
                    username="yousra",
                    display_name="Yousra",
                    role="user",
                    password_hash=hash_password("yousra password"),
                ),
            ]
        )
        await db.commit()


@pytest.fixture
def api_client(tmp_path: Path) -> Generator[TestClient, None, None]:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'api.sqlite3'}"
    setup_database = Database(database_url)
    asyncio.run(_prepare_database(setup_database))
    asyncio.run(setup_database.dispose())
    database = Database(database_url)
    settings = Settings(
        environment="test",
        public_origin="http://testserver",
        trusted_hosts=["testserver"],
        data_dir=tmp_path,
        database_url=database_url,
        attachments_dir=tmp_path / "attachments",
        frontend_dir=tmp_path / "frontend",
        secure_cookies=False,
        provider_retry_attempts=1,
    )
    app = create_app(settings=settings, database=database, provider=ImmediateProvider())
    with TestClient(app, base_url="http://testserver") as client:
        yield client
    asyncio.run(database.dispose())


def _login(client: TestClient, username: str, password: str) -> tuple[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200
    body = response.json()
    return body["id"], body["csrf_token"]


def _wait_generation(client: TestClient, generation_id: str) -> dict[str, object]:
    for _ in range(100):
        response = client.get(f"/api/generations/{generation_id}")
        assert response.status_code == 200
        snapshot = response.json()
        if snapshot["status"] in {"completed", "failed", "stopped"}:
            return cast(dict[str, Any], snapshot)
        time.sleep(0.01)
    raise AssertionError("generation did not finish")


def _wait_title(client: TestClient, thread_id: str) -> dict[str, Any]:
    for _ in range(100):
        detail = client.get(f"/api/threads/{thread_id}").json()
        if detail["title"] != "New conversation":
            return cast(dict[str, Any], detail)
        time.sleep(0.01)
    raise AssertionError("title was not generated")


def test_login_submit_idempotency_handoff_and_cost(api_client: TestClient) -> None:
    owner_id, csrf = _login(api_client, "charles", "charles password")
    headers = {"X-CSRF-Token": csrf, "Origin": "http://testserver"}
    payload = {
        "owner_id": owner_id,
        "mode": "translate",
        "text": "Translate hello world into Canadian French.",
        "client_request_id": "api-request-1",
    }
    submitted = api_client.post("/api/threads", data=payload, headers=headers)
    assert submitted.status_code == 200
    ids = submitted.json()
    completed = _wait_generation(api_client, ids["generation_id"])
    assert completed["blocks"] == [{"type": "deliverable", "text": "Bonjour monde"}]

    duplicate = api_client.post("/api/threads", data=payload, headers=headers)
    assert duplicate.status_code == 200
    assert duplicate.json() == ids
    detail = _wait_title(api_client, ids["thread_id"])
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
    assert detail["title"] == "Canadian French Translation"
    assert detail["owner_username"] == "charles"

    handoff = api_client.post(
        f"/api/threads/{ids['thread_id']}/prompt-handoff",
        json={"client_request_id": "handoff-1"},
        headers=headers,
    )
    assert handoff.status_code == 200
    handoff_snapshot = _wait_generation(api_client, handoff.json()["generation_id"])
    assert handoff_snapshot["status"] == "completed"
    detail_after = api_client.get(f"/api/threads/{ids['thread_id']}").json()
    assert len(detail_after["messages"]) == 2
    assert api_client.get("/api/usage/lifetime").json() == {"formatted": "$0.000369"}


def test_csrf_authorization_security_headers_and_deletion(api_client: TestClient) -> None:
    owner_id, csrf = _login(api_client, "charles", "charles password")
    headers = {"X-CSRF-Token": csrf, "Origin": "http://testserver"}
    missing_csrf = api_client.post(
        "/api/threads",
        data={
            "owner_id": owner_id,
            "mode": "translate",
            "text": "Synthetic text",
            "client_request_id": "missing-csrf",
        },
    )
    assert missing_csrf.status_code == 403

    repeated = api_client.post(
        "/api/threads",
        data={
            "owner_id": owner_id,
            "mode": "translate",
            "client_request_id": "two-files",
        },
        files=[
            ("attachment", ("one.txt", b"one", "text/plain")),
            ("attachment", ("two.txt", b"two", "text/plain")),
        ],
        headers=headers,
    )
    assert repeated.status_code == 422
    assert repeated.json()["code"] == "multiple_attachments"

    submitted = api_client.post(
        "/api/threads",
        data={
            "owner_id": owner_id,
            "mode": "translate",
            "text": "",
            "client_request_id": "delete-me",
        },
        files={"attachment": ("source.txt", b"Synthetic attachment", "text/plain")},
        headers=headers,
    )
    ids = submitted.json()
    _wait_generation(api_client, ids["generation_id"])

    assert api_client.post("/api/auth/logout", headers=headers).status_code == 204
    _, yousra_csrf = _login(api_client, "yousra", "yousra password")
    forbidden = api_client.get(f"/api/threads/{ids['thread_id']}")
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "forbidden"
    assert (
        api_client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": yousra_csrf, "Origin": "http://testserver"},
        ).status_code
        == 204
    )

    _, csrf = _login(api_client, "charles", "charles password")
    response = api_client.delete(
        f"/api/threads/{ids['thread_id']}",
        headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"},
    )
    assert response.status_code == 204
    health = api_client.get("/health/live")
    assert health.headers["x-content-type-options"] == "nosniff"
    assert api_client.get("/api/usage/lifetime").headers["cache-control"] == "no-store"


async def test_seed_only_missing_accounts_preserves_admin_reset(tmp_path: Path) -> None:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'seed.sqlite3'}",
        data_dir=tmp_path,
        attachments_dir=tmp_path / "attachments",
        frontend_dir=tmp_path / "frontend",
        secure_cookies=False,
        charles_password_hash=hash_password("configured Charles password"),
        yousra_password_hash=hash_password("configured Yousra password"),
    )
    database = Database(settings.database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await seed_configured_accounts(database, settings)
    async with database.sessions() as db:
        charles = await db.scalar(select(User).where(User.username == "charles"))
        assert charles is not None
        await change_password(
            db,
            user=charles,
            new_password="admin reset password",  # noqa: S106 -- test credential
        )
        await db.commit()
    await seed_configured_accounts(database, settings)
    async with database.sessions() as db:
        users = list((await db.execute(select(User).order_by(User.username))).scalars())
        assert [(user.username, user.role) for user in users] == [
            ("charles", "owner"),
            ("yousra", "user"),
        ]
        assert verify_password(users[0].password_hash, "admin reset password")
    assert database.engine.sync_engine.hide_parameters
    await database.dispose()


def test_production_configuration_rejects_missing_and_malformed_secrets() -> None:
    valid_hash = hash_password("synthetic production password")
    valid = Settings(
        environment="production",
        openrouter_api_key="sk-or-v1-synthetic",
        charles_password_hash=valid_hash,
        yousra_password_hash=valid_hash,
    )
    validate_production_settings(valid)

    with pytest.raises(RuntimeError, match="OpenRouter"):
        validate_production_settings(valid.model_copy(update={"openrouter_api_key": None}))
    malformed = valid.model_copy(
        update={"charles_password_hash": SecretStr("$argon2id$replace-this-placeholder")}
    )
    with pytest.raises(RuntimeError, match="valid Argon2id"):
        validate_production_settings(malformed)
