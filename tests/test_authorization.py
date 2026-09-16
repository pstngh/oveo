from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oveo.authorization import (
    AuthorizationError,
    may_access_owner,
    may_access_thread,
    require_thread_access,
    resolve_target_owner,
)
from oveo.models import Thread
from tests.conftest import add_user


async def test_owner_may_switch_accounts_and_regular_user_cannot(db: AsyncSession) -> None:
    charles = await add_user(db, "charles", role="owner")
    yousra = await add_user(db, "yousra")

    assert may_access_owner(charles, yousra)
    assert may_access_owner(yousra, yousra)
    assert not may_access_owner(yousra, charles)
    assert await resolve_target_owner(db, actor=charles, requested_username="yousra") is yousra
    with pytest.raises(AuthorizationError):
        await resolve_target_owner(db, actor=yousra, requested_username="charles")


async def test_thread_scope_and_true_actor_are_distinct(db: AsyncSession) -> None:
    charles = await add_user(db, "charles", role="owner")
    yousra = await add_user(db, "yousra")
    outsider = await add_user(db, "outsider")
    thread = Thread(owner_id=yousra.id, mode="translate", voice_key=None)
    db.add(thread)
    await db.flush()

    assert may_access_thread(yousra, thread)
    assert may_access_thread(charles, thread)
    assert not may_access_thread(outsider, thread)
    require_thread_access(charles, thread)
    with pytest.raises(AuthorizationError):
        require_thread_access(outsider, thread)

    # The caller remains Charles even though the selected thread belongs to Yousra.
    assert charles.id != thread.owner_id
