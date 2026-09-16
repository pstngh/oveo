from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from oveo.models import Session, User


class AuthenticationError(Exception):
    """Base exception that API code should map to a content-free client error."""


class InvalidCredentials(AuthenticationError):
    pass


class LoginThrottled(AuthenticationError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("login temporarily unavailable")
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class IssuedSession:
    session: Session
    token: str
    csrf_token: str


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    session: Session
    user: User


_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65_536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("oveo-dummy-password-never-valid")


def normalize_username(username: str) -> str:
    return username.strip().casefold()


def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def authenticate_user(
    db: AsyncSession,
    *,
    username: str,
    password: str,
    max_attempts: int,
    window_seconds: int,
    lock_seconds: int,
    now: datetime | None = None,
) -> User:
    """Verify credentials and maintain the account's bounded persistent throttle state."""

    current_time = now or datetime.now(UTC)
    normalized = normalize_username(username)
    user = await db.scalar(select(User).where(User.username == normalized))
    if user is None:
        verify_password(_DUMMY_PASSWORD_HASH, password)
        raise InvalidCredentials

    if user.login_locked_until is not None:
        locked_until = _aware(user.login_locked_until)
        if current_time < locked_until:
            remaining = max(1, int((locked_until - current_time).total_seconds()))
            raise LoginThrottled(remaining)

    if not verify_password(user.password_hash, password):
        window_started = (
            _aware(user.login_window_started_at)
            if user.login_window_started_at is not None
            else None
        )
        window_expired = window_started is None or current_time - window_started >= timedelta(
            seconds=window_seconds
        )
        if window_expired:
            user.login_window_started_at = current_time
            user.failed_login_count = 1
        else:
            user.failed_login_count += 1

        if user.failed_login_count >= max_attempts:
            user.login_locked_until = current_time + timedelta(seconds=lock_seconds)
        await db.flush()
        raise InvalidCredentials

    user.failed_login_count = 0
    user.login_window_started_at = None
    user.login_locked_until = None
    await db.flush()
    return user


async def create_session(
    db: AsyncSession,
    *,
    user: User,
    session_days: int,
    now: datetime | None = None,
) -> IssuedSession:
    current_time = now or datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    session = Session(
        token_hash=hash_token(token),
        csrf_token_hash=hash_token(csrf_token),
        user=user,
        credential_version=user.credential_version,
        expires_at=current_time + timedelta(days=session_days),
    )
    db.add(session)
    await db.flush()
    return IssuedSession(session=session, token=token, csrf_token=csrf_token)


async def get_session_principal(
    db: AsyncSession,
    token: str | None,
    *,
    now: datetime | None = None,
) -> SessionPrincipal | None:
    if not token:
        return None
    current_time = now or datetime.now(UTC)
    session = await db.scalar(select(Session).where(Session.token_hash == hash_token(token)))
    if session is None:
        return None
    if current_time >= _aware(session.expires_at):
        await db.delete(session)
        await db.flush()
        return None
    if session.credential_version != session.user.credential_version:
        await db.delete(session)
        await db.flush()
        return None
    return SessionPrincipal(session=session, user=session.user)


def validate_csrf(session: Session, cookie_token: str | None, header_token: str | None) -> bool:
    if not cookie_token or not header_token:
        return False
    if not hmac.compare_digest(cookie_token, header_token):
        return False
    return hmac.compare_digest(session.csrf_token_hash, hash_token(header_token))


async def revoke_session(db: AsyncSession, token: str | None) -> None:
    if token:
        await db.execute(delete(Session).where(Session.token_hash == hash_token(token)))


async def revoke_user_sessions(db: AsyncSession, user_id: str) -> None:
    await db.execute(delete(Session).where(Session.user_id == user_id))


async def change_password(
    db: AsyncSession,
    *,
    user: User,
    new_password: str,
) -> None:
    user.password_hash = hash_password(new_password)
    user.credential_version += 1
    await revoke_user_sessions(db, user.id)
    await db.flush()
