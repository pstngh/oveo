from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oveo.auth import (
    InvalidCredentials,
    LoginThrottled,
    authenticate_user,
    change_password,
    create_session,
    get_session_principal,
    hash_password,
    validate_csrf,
    verify_password,
)
from oveo.models import Session
from tests.conftest import add_user


def test_argon2id_password_helpers() -> None:
    encoded = hash_password("a private password")
    assert encoded.startswith("$argon2id$")
    assert verify_password(encoded, "a private password")
    assert not verify_password(encoded, "wrong")
    assert not verify_password("not-an-argon-hash", "anything")


async def test_session_is_absolute_csrf_bound_and_credential_versioned(
    db: AsyncSession,
) -> None:
    now = datetime(2026, 9, 16, 12, tzinfo=UTC)
    user = await add_user(db, "charles", role="owner")
    issued = await create_session(db, user=user, session_days=30, now=now)
    await db.commit()

    principal = await get_session_principal(db, issued.token, now=now + timedelta(days=29))
    assert principal is not None
    assert principal.user.id == user.id
    assert principal.session.expires_at.replace(tzinfo=UTC) == now + timedelta(days=30)
    assert validate_csrf(issued.session, issued.csrf_token, issued.csrf_token)
    assert not validate_csrf(issued.session, issued.csrf_token, "different")
    assert not validate_csrf(issued.session, None, issued.csrf_token)

    await change_password(
        db,
        user=user,
        new_password="a replacement password",  # noqa: S106 - synthetic test value
    )
    await db.commit()
    assert await db.scalar(select(func.count()).select_from(Session)) == 0
    assert await get_session_principal(db, issued.token, now=now + timedelta(days=29)) is None


async def test_expired_session_is_rejected_and_removed(db: AsyncSession) -> None:
    now = datetime(2026, 9, 16, 12, tzinfo=UTC)
    user = await add_user(db, "yousra")
    issued = await create_session(db, user=user, session_days=30, now=now)
    await db.commit()

    principal = await get_session_principal(db, issued.token, now=now + timedelta(days=30))
    assert principal is None
    await db.commit()
    assert await db.scalar(select(func.count()).select_from(Session)) == 0


async def test_login_throttle_locks_then_resets_after_success(db: AsyncSession) -> None:
    now = datetime(2026, 9, 16, 12, tzinfo=UTC)
    user = await add_user(
        db,
        "yousra",
        password="right password",  # noqa: S106 - synthetic test value
    )

    for offset in range(3):
        with pytest.raises(InvalidCredentials):
            await authenticate_user(
                db,
                username=" YOUSRA ",
                password="wrong",  # noqa: S106 - synthetic test value
                max_attempts=3,
                window_seconds=60,
                lock_seconds=120,
                now=now + timedelta(seconds=offset),
            )
    assert user.failed_login_count == 3
    assert user.login_locked_until is not None

    with pytest.raises(LoginThrottled) as error:
        await authenticate_user(
            db,
            username="yousra",
            password="right password",  # noqa: S106 - synthetic test value
            max_attempts=3,
            window_seconds=60,
            lock_seconds=120,
            now=now + timedelta(seconds=30),
        )
    assert error.value.retry_after_seconds > 0

    authenticated = await authenticate_user(
        db,
        username="yousra",
        password="right password",  # noqa: S106 - synthetic test value
        max_attempts=3,
        window_seconds=60,
        lock_seconds=120,
        now=now + timedelta(seconds=123),
    )
    assert authenticated.id == user.id
    assert authenticated.failed_login_count == 0
    assert authenticated.login_locked_until is None


async def test_unknown_account_gets_generic_failure(db: AsyncSession) -> None:
    with pytest.raises(InvalidCredentials):
        await authenticate_user(
            db,
            username="nobody",
            password="wrong",  # noqa: S106 - synthetic test value
            max_attempts=3,
            window_seconds=60,
            lock_seconds=60,
        )
