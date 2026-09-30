from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import event, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from oveo.config import Settings
from oveo.db import prepare_sqlite_path
from oveo.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

database_url = os.environ.get("OVEO_DATABASE_URL") or Settings().database_url
prepare_sqlite_path(database_url)
config.set_main_option("sqlalchemy.url", database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    sqlite = connection.dialect.name == "sqlite"
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        # SQLite's DDL is transactional once the driver leaves transactions to SQLAlchemy
        # (see run_async_migrations): every pending migration and its revision stamp then
        # commit together, after the check below, or not at all.
        transactional_ddl=True if sqlite else None,
    )
    with context.begin_transaction():
        migration = context.get_context()
        before = migration.get_current_revision()
        context.run_migrations()
        # Every container start runs this; only one that migrated has anything to check.
        if sqlite and migration.get_current_revision() != before:
            # Foreign keys are off while migrating (see run_async_migrations), so the
            # result is checked instead: a migration must not leave a dangling reference.
            # Raising here rolls the upgrade back, so the next start fails the same way.
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"migrations left {len(violations)} foreign key violation(s)")


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    if connectable.dialect.name == "sqlite":
        # Batch migrations rebuild a table by dropping it; with foreign keys on, that drop
        # would cascade and delete every child row.
        @event.listens_for(connectable.sync_engine, "connect")
        def foreign_keys_off(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.close()
            # Python's sqlite3 opens a transaction only before data changes, so DDL would
            # commit on its own; SQLAlchemy's begin emits BEGIN instead (below).
            dbapi_connection.isolation_level = None

        @event.listens_for(connectable.sync_engine, "begin")
        def begin(sync_connection: Connection) -> None:
            sync_connection.exec_driver_sql("BEGIN")

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
