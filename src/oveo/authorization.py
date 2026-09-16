from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oveo.models import Thread, User


class AuthorizationError(Exception):
    pass


def may_access_owner(actor: User, owner: User) -> bool:
    return actor.id == owner.id or actor.role == "owner"


def may_access_thread(actor: User, thread: Thread) -> bool:
    return actor.id == thread.owner_id or actor.role == "owner"


def require_thread_access(actor: User, thread: Thread) -> None:
    if not may_access_thread(actor, thread):
        raise AuthorizationError


async def resolve_thread(db: AsyncSession, *, actor: User, thread_id: str) -> Thread | None:
    thread = await db.get(Thread, thread_id)
    if thread is None:
        return None
    require_thread_access(actor, thread)
    return thread


async def resolve_target_owner(
    db: AsyncSession,
    *,
    actor: User,
    requested_username: str | None,
) -> User:
    if requested_username is None or requested_username == actor.username:
        return actor
    owner = await db.scalar(select(User).where(User.username == requested_username.casefold()))
    if owner is None or not may_access_owner(actor, owner):
        raise AuthorizationError
    return owner
