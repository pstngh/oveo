from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from oveo.authorization import may_access_thread
from oveo.models import Thread
from tests.conftest import add_user


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

    # The caller remains Charles even though the selected thread belongs to Yousra.
    assert charles.id != thread.owner_id
