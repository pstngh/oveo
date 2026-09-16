from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _prepare_sqlite_path(database_url: str) -> None:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return
    path = database_url.removeprefix(prefix)
    if not path or path == ":memory:" or path.startswith("file:"):
        return
    Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create the application engine with SQLite safety pragmas on every connection."""

    _prepare_sqlite_path(database_url)
    engine = create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        hide_parameters=True,
    )

    if database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


class Database:
    """Own the engine and short-lived session factory for one application process."""

    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        self.engine = create_engine(database_url, echo=echo)
        self.sessions = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
