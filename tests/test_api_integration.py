from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from starlette.requests import Request

import oveo.generation as generation_module
from oveo.auth import change_password, hash_password, verify_password
from oveo.config import Settings
from oveo.db import Database
from oveo.docx import DOCX_MEDIA_TYPE, ExtractedDocx, extract_docx
from oveo.generation import ProviderCompletion, ProviderRequest
from oveo.main import create_app, seed_configured_accounts, validate_production_settings
from oveo.models import Base, User
from tests.docx_fixtures import make_docx

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


def _docx_protocol_response(*, second_version: bool) -> bytes:
    protected_link = "{{OVEO_LINK_l000001}}portail{{/OVEO_LINK_l000001}}"
    if second_version:
        output = "Salut portail.\n\nCellule révisée"
        state: dict[str, object] = {
            "v": 1,
            "event": "state",
            "operation": "full",
            "base_version": 1,
            "docx_blocks": [
                {"id": "p000001", "text": f"Salut {protected_link}."},
                {"id": "p000002", "text": "Cellule révisée"},
            ],
        }
    else:
        # The deliverable is the output and the uploaded DOCX is the source.
        output = "Bonjour portail!\n\nTexte de cellule"
        state = {
            "v": 1,
            "event": "state",
            "operation": "establish",
            "brief": {"direction": "en-US-fr-CA"},
            "docx_blocks": [
                {"id": "p000001", "text": f"Bonjour {protected_link}!"},
                {"id": "p000002", "text": "Texte de cellule"},
            ],
        }
    events = [
        {"v": 1, "event": "response_start"},
        {"v": 1, "event": "block_start", "id": "b1", "type": "deliverable"},
        {"v": 1, "event": "block_delta", "id": "b1", "text": output},
        {"v": 1, "event": "block_end", "id": "b1"},
        state,
        {"v": 1, "event": "response_end"},
    ]
    return ("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n").encode()


class DocxProvider:
    def __init__(self) -> None:
        self.chat_requests: list[ProviderRequest] = []

    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del cancel_event
        if request.purpose == "title":
            await emit(b"DOCX Translation")  # type: ignore[operator]
        else:
            self.chat_requests.append(request)
            await emit(  # type: ignore[operator]
                _docx_protocol_response(second_version=len(self.chat_requests) == 2)
            )
        return ProviderCompletion(cost_microusd=1)


class ReferenceProvider:
    def __init__(self) -> None:
        self.chat_requests: list[ProviderRequest] = []

    async def generate(
        self, request: ProviderRequest, emit: object, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del cancel_event
        if request.purpose == "title":
            await emit(b"Policy Revision")  # type: ignore[operator]
            return ProviderCompletion(cost_microusd=1)
        self.chat_requests.append(request)
        if len(self.chat_requests) == 1:
            source = "La présente politique s'applique aux employés permanents."
            output = "Cette politique s'applique aux employés permanents."
            events = [
                {"v": 1, "event": "response_start"},
                {"v": 1, "event": "block_start", "id": "b1", "type": "deliverable"},
                {"v": 1, "event": "block_delta", "id": "b1", "text": output},
                {"v": 1, "event": "block_end", "id": "b1"},
                {
                    "v": 1,
                    "event": "state",
                    "operation": "establish",
                    "source": source,
                    "brief": {"depth": "revision", "locale": "fr-CA"},
                },
                {"v": 1, "event": "response_end"},
            ]
            payload = "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n"
            await emit(payload.encode())  # type: ignore[operator]
        else:
            await emit(_SUCCESS)  # type: ignore[operator]
        return ProviderCompletion(cost_microusd=1)


async def _prepare_database(database: Database) -> None:
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with database.sessions() as db:
        db.add_all(
            [
                User(
                    username="charles",
                    display_name="Charles",
                    password_hash=hash_password("charles password"),
                ),
                User(
                    username="yousra",
                    display_name="Yousra",
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


def test_lifetime_usage_is_a_database_only_read(api_client: TestClient) -> None:
    _login(api_client, "charles", "charles password")
    manager = api_client.app.state.generation_manager
    reconcile = AsyncMock(side_effect=AssertionError("metadata lookup must not run"))
    manager.reconcile_pending_costs = reconcile

    response = api_client.get("/api/usage/lifetime")

    assert response.status_code == 200
    assert response.json() == {"formatted": "$0.00"}
    reconcile.assert_not_awaited()


async def test_startup_does_not_wait_for_provider_metadata(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'startup.sqlite3'}"
    database = Database(database_url)
    await _prepare_database(database)
    settings = Settings(
        environment="test",
        public_origin="http://testserver",
        trusted_hosts=["testserver"],
        data_dir=tmp_path,
        database_url=database_url,
        attachments_dir=tmp_path / "attachments",
        frontend_dir=tmp_path / "frontend",
        secure_cookies=False,
    )
    app = create_app(settings=settings, database=database, provider=ImmediateProvider())
    manager = app.state.generation_manager
    started = asyncio.Event()

    async def blocked_reconciliation() -> int:
        started.set()
        await asyncio.Event().wait()
        return 0

    manager.reconcile_pending_costs = blocked_reconciliation
    try:
        async with asyncio.timeout(2):
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(started.wait(), timeout=0.5)
    finally:
        await database.dispose()


def test_unexpected_http_failure_returns_and_logs_an_opaque_error_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'errors.sqlite3'}"
    database = Database(database_url)
    asyncio.run(_prepare_database(database))
    settings = Settings(
        environment="test",
        public_origin="http://testserver",
        trusted_hosts=["testserver"],
        data_dir=tmp_path,
        database_url=database_url,
        attachments_dir=tmp_path / "attachments",
        frontend_dir=tmp_path / "frontend",
        secure_cookies=False,
    )
    app = create_app(settings=settings, database=database, provider=ImmediateProvider())
    caplog.set_level(logging.ERROR, logger="oveo.http")
    handler = app.exception_handlers[Exception]
    response = asyncio.run(
        handler(Request({"type": "http"}), RuntimeError("private request marker"))
    )
    asyncio.run(database.dispose())

    assert response.status_code == 500
    error_id = json.loads(response.body)["error_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", error_id)
    diagnostics = "\n".join(record.getMessage() for record in caplog.records)
    assert f"error_id={error_id}" in diagnostics
    assert "area=http" in diagnostics
    assert "exception_class=RuntimeError" in diagnostics
    assert "locations=" in diagnostics
    assert "private request marker" not in diagnostics


def test_only_the_three_current_modes_are_accepted(api_client: TestClient) -> None:
    owner_id, csrf = _login(api_client, "charles", "charles password")
    headers = {"X-CSRF-Token": csrf, "Origin": "http://testserver"}
    modes = ("translate", "revision", "internal_comms")
    for index, mode in enumerate(modes, start=1):
        payload = {
            "owner_id": owner_id,
            "mode": mode,
            "text": "Synthetic scoped request.",
            "client_request_id": f"mode-{index}",
        }
        response = api_client.post("/api/threads", data=payload, headers=headers)
        assert response.status_code == 200
        ids = response.json()
        _wait_generation(api_client, ids["generation_id"])
        detail = api_client.get(f"/api/threads/{ids['thread_id']}").json()
        assert detail["mode"] == mode
    unsupported = api_client.post(
        "/api/threads",
        data={
            "owner_id": owner_id,
            "mode": "unsupported",
            "text": "Synthetic scoped request.",
            "client_request_id": "unsupported-mode",
        },
        headers=headers,
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["code"] == "invalid_request"


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
            ("attachment", ("one.docx", b"one", DOCX_MEDIA_TYPE)),
            ("attachment", ("two.docx", b"two", DOCX_MEDIA_TYPE)),
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
        files={"attachment": ("source.docx", make_docx(), DOCX_MEDIA_TYPE)},
        headers=headers,
    )
    ids = submitted.json()
    _wait_generation(api_client, ids["generation_id"])

    assert api_client.post("/api/auth/logout", headers=headers).status_code == 204
    _, yousra_csrf = _login(api_client, "yousra", "yousra password")
    forbidden = api_client.get(f"/api/threads/{ids['thread_id']}")
    assert forbidden.status_code == 404
    assert forbidden.json()["code"] == "thread_not_found"
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


def test_charles_cannot_discover_or_access_yousra_conversations(
    api_client: TestClient,
) -> None:
    yousra_id, yousra_csrf = _login(api_client, "yousra", "yousra password")
    yousra_headers = {"X-CSRF-Token": yousra_csrf, "Origin": "http://testserver"}
    submitted = api_client.post(
        "/api/threads",
        data={
            "mode": "revision",
            "text": "Private Yousra text.",
            "client_request_id": "yousra-private-thread",
        },
        headers=yousra_headers,
    )
    assert submitted.status_code == 200
    ids = submitted.json()
    _wait_generation(api_client, ids["generation_id"])
    _wait_title(api_client, ids["thread_id"])
    assert api_client.get("/api/usage/lifetime").json() == {"formatted": "$0.000246"}
    assert api_client.post("/api/auth/logout", headers=yousra_headers).status_code == 204

    charles_id, charles_csrf = _login(api_client, "charles", "charles password")
    charles_headers = {"X-CSRF-Token": charles_csrf, "Origin": "http://testserver"}
    assert api_client.get("/api/accounts").json() == [
        {
            "id": charles_id,
            "username": "charles",
            "display_name": "Charles",
        }
    ]
    assert api_client.get("/api/threads").json() == []
    assert api_client.get(f"/api/threads?owner_id={yousra_id}").status_code == 403

    thread_response = api_client.get(f"/api/threads/{ids['thread_id']}")
    assert thread_response.status_code == 404
    assert thread_response.json()["code"] == "thread_not_found"
    generation_response = api_client.get(f"/api/generations/{ids['generation_id']}")
    assert generation_response.status_code == 404
    assert generation_response.json()["code"] == "thread_not_found"
    message_response = api_client.post(
        f"/api/threads/{ids['thread_id']}/messages",
        data={"text": "Cross-account attempt", "client_request_id": "blocked-message"},
        headers=charles_headers,
    )
    assert message_response.status_code == 404
    assert (
        api_client.post(
            "/api/threads",
            data={
                "owner_id": yousra_id,
                "mode": "translate",
                "text": "Cross-account attempt",
                "client_request_id": "blocked-create",
            },
            headers=charles_headers,
        ).status_code
        == 403
    )
    # Cost is shared operational metadata; conversation content remains private.
    assert api_client.get("/api/usage/lifetime").json() == {"formatted": "$0.000246"}


def test_docx_upload_latest_canonical_export_and_ownership(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = DocxProvider()
    api_client.app.state.generation_manager.provider = provider
    _, csrf = _login(api_client, "charles", "charles password")
    headers = {"X-CSRF-Token": csrf, "Origin": "http://testserver"}

    submitted = api_client.post(
        "/api/threads",
        data={
            "mode": "translate",
            "text": "Translate the attached Word document.",
            "client_request_id": "docx-establish",
        },
        files={"attachment": ("source.docx", make_docx(), DOCX_MEDIA_TYPE)},
        headers=headers,
    )
    assert submitted.status_code == 200
    ids = submitted.json()
    first = _wait_generation(api_client, ids["generation_id"])
    assert first["status"] == "completed"

    detail = api_client.get(f"/api/threads/{ids['thread_id']}").json()
    assert detail["docx_exportable"] is True
    assert detail["messages"][0]["attachment"]["media_type"] == DOCX_MEDIA_TYPE
    assert detail["messages"][0]["attachment"]["role"] == "source"
    trusted = provider.chat_requests[0].snapshot["provider_messages"][0]["content"]
    untrusted = provider.chat_requests[0].snapshot["provider_messages"][1]["content"]
    assert "DOCX protocol extension" in trusted
    assert '"id":"p000001"' in untrusted
    assert "source.docx" not in untrusted

    extraction_calls = 0
    original_extract = generation_module.extract_docx

    def counted_extract(content: bytes, *, max_uncompressed_bytes: int) -> ExtractedDocx:
        nonlocal extraction_calls
        extraction_calls += 1
        return original_extract(content, max_uncompressed_bytes=max_uncompressed_bytes)

    monkeypatch.setattr(generation_module, "extract_docx", counted_extract)

    revised = api_client.post(
        f"/api/threads/{ids['thread_id']}/messages",
        data={
            "text": "Use a warmer greeting and revise the table cell.",
            "client_request_id": "docx-version-2",
        },
        headers=headers,
    )
    assert revised.status_code == 200
    second = _wait_generation(api_client, revised.json()["generation_id"])
    assert second["status"] == "completed"
    # Snapshot reconstruction uses the stored block map. The only package parse on
    # this revision is the canonical-template integrity check during state commit.
    assert extraction_calls == 1

    downloaded = api_client.get(f"/api/threads/{ids['thread_id']}/document.docx")
    assert downloaded.status_code == 200
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert downloaded.headers["content-type"].startswith(DOCX_MEDIA_TYPE)
    assert "attachment;" in downloaded.headers["content-disposition"]
    assert extract_docx(downloaded.content).plain_text == ("Salut portail.\n\nCellule révisée")

    assert api_client.post("/api/auth/logout", headers=headers).status_code == 204
    _, other_csrf = _login(api_client, "yousra", "yousra password")
    forbidden = api_client.get(f"/api/threads/{ids['thread_id']}/document.docx")
    assert forbidden.status_code == 404
    assert forbidden.json()["code"] == "thread_not_found"
    assert (
        api_client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": other_csrf, "Origin": "http://testserver"},
        ).status_code
        == 204
    )
    assert list(api_client.app.state.settings.attachments_dir.glob("*.docx"))
    _, owner_csrf = _login(api_client, "charles", "charles password")
    deleted = api_client.delete(
        f"/api/threads/{ids['thread_id']}",
        headers={"X-CSRF-Token": owner_csrf, "Origin": "http://testserver"},
    )
    assert deleted.status_code == 204
    assert not list(api_client.app.state.settings.attachments_dir.glob("*.docx"))


def test_reference_docx_guides_plain_text_without_becoming_the_template(
    api_client: TestClient,
) -> None:
    provider = ReferenceProvider()
    api_client.app.state.generation_manager.provider = provider
    _, csrf = _login(api_client, "charles", "charles password")
    headers = {"X-CSRF-Token": csrf, "Origin": "http://testserver"}

    submitted = api_client.post(
        "/api/threads",
        data={
            "mode": "revision",
            "text": "La présente politique s'applique aux employés permanents.",
            "client_request_id": "reference-establish",
            "attachment_role": "reference",
        },
        files={"attachment": ("policy-reference.docx", make_docx(), DOCX_MEDIA_TYPE)},
        headers=headers,
    )
    assert submitted.status_code == 200
    ids = submitted.json()
    completed = _wait_generation(api_client, ids["generation_id"])
    assert completed["status"] == "completed"

    detail = api_client.get(f"/api/threads/{ids['thread_id']}").json()
    assert detail["docx_exportable"] is False
    assert detail["messages"][0]["attachment"]["role"] == "reference"
    trusted = provider.chat_requests[0].snapshot["provider_messages"][0]["content"]
    untrusted = provider.chat_requests[0].snapshot["provider_messages"][1]["content"]
    assert "Reference-document authority and consistency" in trusted
    assert "DOCX protocol extension" not in trusted
    assert '"active_reference_document"' in untrusted
    assert '"role":"reference"' in untrusted
    assert "policy-reference.docx" not in untrusted

    follow_up = api_client.post(
        f"/api/threads/{ids['thread_id']}/messages",
        data={
            "text": "Which reference remains active?",
            "client_request_id": "reference-follow-up",
        },
        headers=headers,
    )
    assert follow_up.status_code == 200
    assert _wait_generation(api_client, follow_up.json()["generation_id"])["status"] == "completed"
    follow_up_untrusted = provider.chat_requests[1].snapshot["provider_messages"][1]["content"]
    assert '"active_reference_document"' in follow_up_untrusted
    assert '"role":"reference"' in follow_up_untrusted


def test_docx_export_returns_conflict_without_committed_docx(api_client: TestClient) -> None:
    _, csrf = _login(api_client, "charles", "charles password")
    submitted = api_client.post(
        "/api/threads",
        data={
            "mode": "revision",
            "text": "Review this plain text.",
            "client_request_id": "plain-no-docx",
        },
        headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"},
    )
    ids = submitted.json()
    _wait_generation(api_client, ids["generation_id"])

    response = api_client.get(f"/api/threads/{ids['thread_id']}/document.docx")
    assert response.status_code == 409
    assert response.json()["code"] == "docx_export_unavailable"
    assert response.headers["cache-control"] == "no-store"


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
        assert [user.username for user in users] == ["charles", "yousra"]
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
