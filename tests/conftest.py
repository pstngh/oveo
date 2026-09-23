from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oveo.auth import hash_password
from oveo.config import Settings
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
    password: str = "correct horse battery staple",  # noqa: S107 - synthetic test value
) -> User:
    user = User(
        username=username,
        display_name=username.title(),
        password_hash=hash_password(password),
    )
    db.add(user)
    await db.flush()
    return user


@pytest.fixture
async def manager_database(tmp_path: Path) -> AsyncIterator[tuple[Database, Settings, User]]:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'manager.sqlite3'}",
        data_dir=tmp_path,
        attachments_dir=tmp_path / "attachments",
        frontend_dir=tmp_path / "frontend",
        secure_cookies=False,
        provider_retry_attempts=1,
    )
    database = Database(settings.database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with database.sessions() as db:
        user = User(
            username="charles",
            display_name="Charles",
            password_hash=hash_password("test password"),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    try:
        yield database, settings, user
    finally:
        await database.dispose()
