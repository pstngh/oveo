from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence

from sqlalchemy import select

from oveo.auth import change_password, hash_password
from oveo.config import get_settings
from oveo.db import Database
from oveo.models import User


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        password = _read_password(from_stdin=args.password_stdin, confirm=not args.password_stdin)
        if args.command == "hash-password":
            print(hash_password(password))
        elif args.command == "reset-password":
            asyncio.run(_reset_password(args.username, password))
            print(f"Password reset and sessions revoked for {args.username}.")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
