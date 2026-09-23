from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Text, cast, func, select, update

from oveo.auth import change_password, hash_password
from oveo.config import get_settings
from oveo.db import Database
from oveo.models import Generation, User

_TERMINAL_STATUSES = ("completed", "failed", "stopped")
# Small batches keep each write transaction short enough to run beside the live app.
_CLEAR_BATCH = 200


def _read_password(*, from_stdin: bool, confirm: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Password: ")
        if confirm and password != getpass.getpass("Confirm password: "):
            raise ValueError("passwords do not match")
    if not password:
        raise ValueError("password must not be empty")
    return password


async def _reset_password(username: str, password: str) -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    try:
        async with database.sessions() as db:
            user = await db.scalar(select(User).where(User.username == username.casefold()))
            if user is None:
                raise ValueError("account not found")
            await change_password(db, user=user, new_password=password)
            await db.commit()
    finally:
        await database.dispose()


def schema_revisions(config_path: Path) -> list[str]:
    """Every migration revision this release ships, the head first.

    Reads only the migration scripts, never a database or the runtime settings, so a
    deployment can ask a candidate image before starting it.
    """

    script = ScriptDirectory.from_config(Config(str(config_path)))
    heads = script.get_heads()
    if len(heads) != 1:
        raise ValueError("the migrations must have exactly one head")
    revisions = [revision.revision for revision in script.walk_revisions()]
    if not revisions or revisions[0] != heads[0]:
        raise ValueError("the migration history could not be ordered")
    return revisions


async def clear_terminal_snapshots(database: Database, *, apply: bool) -> tuple[int, int]:
    """Count, and with `apply` clear, request context kept by finished generations.

    Only rows that are completed, failed or stopped are touched, and only their frozen
    request context; nothing reads it after a generation finishes (a retry builds a
    fresh one). Returns the number of rows and the stored size in bytes.
    """

    stored = cast(Generation.request_snapshot, Text)
    holding = (Generation.status.in_(_TERMINAL_STATUSES), stored != "{}")
    async with database.sessions() as db:
        count, size = (
            await db.execute(
                select(func.count(), func.coalesce(func.sum(func.length(stored)), 0)).where(
                    *holding
                )
            )
        ).one()
    if not apply:
        return int(count), int(size)
    cleared = 0
    while True:
        async with database.sessions() as db:
            batch = select(Generation.id).where(*holding).limit(_CLEAR_BATCH)
            result = await db.execute(
                update(Generation)
                .where(Generation.id.in_(batch), *holding)
                .values(request_snapshot={})
                .execution_options(synchronize_session=False)
            )
            await db.commit()
        changed = int(getattr(result, "rowcount", 0) or 0)
        if changed == 0:
            return cleared, int(size)
        cleared += changed


async def _clear_terminal_snapshots(*, apply: bool) -> tuple[int, int]:
    database = Database(get_settings().database_url)
    try:
        return await clear_terminal_snapshots(database, apply=apply)
    finally:
        await database.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oveo-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    hash_command = commands.add_parser("hash-password", help="create an Argon2id password hash")
    hash_command.add_argument("--password-stdin", action="store_true")
    reset_command = commands.add_parser(
        "reset-password", help="replace one account password and revoke its sessions"
    )
    reset_command.add_argument("username", choices=("charles", "yousra"))
    reset_command.add_argument("--password-stdin", action="store_true")
    revisions_command = commands.add_parser(
        "schema-revisions", help="print the migration revisions this release ships, head first"
    )
    revisions_command.add_argument("--config", type=Path, default=Path("alembic.ini"))
    clear_command = commands.add_parser(
        "clear-terminal-snapshots",
        help="report (or with --apply clear) request context kept by finished generations",
    )
    clear_command.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "hash-password":
            password = _read_password(
                from_stdin=args.password_stdin, confirm=not args.password_stdin
            )
            print(hash_password(password))
        elif args.command == "reset-password":
            password = _read_password(
                from_stdin=args.password_stdin, confirm=not args.password_stdin
            )
            asyncio.run(_reset_password(args.username, password))
            print(f"Password reset and sessions revoked for {args.username}.")
        elif args.command == "schema-revisions":
            print("\n".join(schema_revisions(args.config)))
        else:
            count, size = asyncio.run(_clear_terminal_snapshots(apply=args.apply))
            if args.apply:
                print(
                    f"Cleared the stored request context of {count} finished generation(s) "
                    f"({size} bytes). Messages, documents, attachments and usage records are "
                    "unchanged. The database file keeps its size until an optional VACUUM "
                    "(see OPERATIONS.md)."
                )
            else:
                print(
                    f"{count} finished generation(s) keep {size} bytes of stored request "
                    "context. Nothing was changed; run again with --apply to clear it."
                )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
