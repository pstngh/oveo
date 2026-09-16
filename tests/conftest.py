from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oveo.auth import hash_password
from oveo.db import Database
from oveo.models import Base, User


@pytest.fixture
async def database(tmp_path: object) -> AsyncIterator[Database]:
    path = tmp_path / "test.sqlite3"  # type: ignore[operator]
    database = Database(f"sqlite+aiosqlite:///{path}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def db(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.sessions() as session:
        yield session


async def add_user(
    db: AsyncSession,
    username: str,
    *,
    role: str = "user",
    password: str = "correct horse battery staple",  # noqa: S107 - synthetic test value
) -> User:
    user = User(
        username=username,
        display_name=username.title(),
        role=role,
        password_hash=hash_password(password),
    )
    db.add(user)
    await db.flush()
    return user
