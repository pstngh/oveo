from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from oveo.authorization import may_access_thread
from oveo.models import Thread
from tests.conftest import add_user


async def test_thread_scope_is_private_to_its_account(db: AsyncSession) -> None:
    charles = await add_user(db, "charles")
    yousra = await add_user(db, "yousra")
    outsider = await add_user(db, "outsider")
    thread = Thread(owner_id=yousra.id, mode="translate")
    db.add(thread)
    await db.flush()

    assert may_access_thread(yousra, thread)
    assert not may_access_thread(charles, thread)
    assert not may_access_thread(outsider, thread)
