#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tarfile
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote

MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
SCHEMA_VERSION = 1


class BackupError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_storage_name(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and len(path.parts) == 1
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
    )


def _walk_regular_files(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                raise BackupError(f"symlink is not allowed in attachments: {path}")
        for name in files:
            path = current_path / name
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise BackupError(f"non-regular attachment is not allowed: {path}")
            yield path


def _copy_attachments(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, mode=0o700)
    if not source.exists():
        return
    if source.is_symlink() or not source.is_dir():
        raise BackupError("attachment source must be a real directory")
    for path in _walk_regular_files(source):
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(path, target, follow_symlinks=False)
        target.chmod(0o600)


def _open_readonly_database(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _check_database(path: Path) -> sqlite3.Connection:
    connection = _open_readonly_database(path)
    quick_check = connection.execute("PRAGMA quick_check").fetchone()
    if quick_check != ("ok",):
        connection.close()
        raise BackupError("SQLite quick_check failed")
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        connection.close()
        raise BackupError("SQLite foreign_key_check failed")
    return connection


def _validate_attachment_rows(database: sqlite3.Connection, attachment_root: Path) -> None:
    has_table = database.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='attachments'"
    ).fetchone()
    if has_table is None:
        raise BackupError("attachments table is missing")
    referenced: set[str] = set()
    for storage_name, byte_count, expected_sha256 in database.execute(
        "SELECT storage_name, byte_count, sha256 FROM attachments"
    ):
        if not isinstance(storage_name, str) or not _safe_storage_name(storage_name):
            raise BackupError("database contains an unsafe attachment storage name")
        path = attachment_root / storage_name
        if not path.is_file() or path.is_symlink():
            raise BackupError(f"referenced attachment is missing: {storage_name}")
        if path.stat().st_size != byte_count:
            raise BackupError(f"attachment size mismatch: {storage_name}")
        if sha256_file(path) != expected_sha256:
            raise BackupError(f"attachment digest mismatch: {storage_name}")
        referenced.add(storage_name)
    actual = {
        path.relative_to(attachment_root).as_posix()
        for path in _walk_regular_files(attachment_root)
    }
    if actual != referenced:
        raise BackupError("attachment tree contains files not referenced by the database")


def _normalized_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    info.mode = 0o700 if info.isdir() else 0o600
    return info


def create_archive(database_path: Path, attachments_path: Path, archive_path: Path) -> None:
    if not database_path.is_file() or database_path.is_symlink():
        raise BackupError("database must be a regular file")
    if archive_path.exists():
        raise BackupError("refusing to overwrite an existing plaintext archive")
    archive_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    with tempfile.TemporaryDirectory(prefix="oveo-backup-") as temporary:
        payload = Path(temporary) / "payload"
        payload.mkdir(mode=0o700)
        snapshot = payload / "oveo.sqlite3"
        copied_attachments = payload / "attachments"

        locker = sqlite3.connect(database_path, timeout=30, isolation_level=None)
        locker.execute("PRAGMA busy_timeout=30000")
        try:
            locker.execute("BEGIN IMMEDIATE")
            source = _open_readonly_database(database_path)
            target = sqlite3.connect(snapshot)
            try:
                source.backup(target, pages=256, sleep=0.05)
            finally:
                target.close()
                source.close()
            snapshot.chmod(0o600)
            _copy_attachments(attachments_path, copied_attachments)
            checked = _check_database(snapshot)
            try:
                _validate_attachment_rows(checked, copied_attachments)
            finally:
                checked.close()
        finally:
            if locker.in_transaction:
                locker.rollback()
            locker.close()

        files: dict[str, dict[str, int | str]] = {
            "oveo.sqlite3": {
                "size": snapshot.stat().st_size,
                "sha256": sha256_file(snapshot),
            }
        }
        for path in sorted(_walk_regular_files(copied_attachments)):
            name = (Path("attachments") / path.relative_to(copied_attachments)).as_posix()
            files[name] = {"size": path.stat().st_size, "sha256": sha256_file(path)}
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "files": files,
        }
        manifest_path = payload / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)

        with tarfile.open(archive_path, mode="w:gz", compresslevel=6) as archive:
            archive.add(manifest_path, arcname="manifest.json", filter=_normalized_tar_info)
            archive.add(snapshot, arcname="oveo.sqlite3", filter=_normalized_tar_info)
            archive.add(
                copied_attachments,
                arcname="attachments",
                recursive=True,
                filter=_normalized_tar_info,
            )
        archive_path.chmod(0o600)


def _validate_member(member: tarfile.TarInfo, seen: set[str]) -> None:
    name = member.name
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise BackupError("archive contains an unsafe member path")
    if name in seen:
        raise BackupError("archive contains duplicate members")
    seen.add(name)
    allowed = name in {"manifest.json", "oveo.sqlite3", "attachments"} or name.startswith(
        "attachments/"
    )
    if not allowed or not (member.isdir() or member.isreg()):
        raise BackupError("archive contains an unsupported member")


def _extract_archive(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    seen: set[str] = set()
    total_size = 0
    with tarfile.open(archive_path, mode="r:*") as archive:
        members = archive.getmembers()
        for member in members:
            _validate_member(member, seen)
            total_size += member.size
            if total_size > MAX_ARCHIVE_BYTES:
                raise BackupError("archive expands beyond the safety limit")
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = archive.extractfile(member)
            if source is None:
                raise BackupError("archive member could not be read")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            target.chmod(0o600)


def validate_restored_tree(destination: Path) -> None:
    manifest_path = destination / "manifest.json"
    database_path = destination / "oveo.sqlite3"
    attachments_path = destination / "attachments"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackupError("backup manifest is missing or invalid") from error
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise BackupError("unsupported backup schema version")
    files = manifest.get("files")
    if not isinstance(files, dict) or "oveo.sqlite3" not in files:
        raise BackupError("backup manifest has no database entry")

    actual_files = {
        path.relative_to(destination).as_posix()
        for path in _walk_regular_files(destination)
        if path != manifest_path
    }
    if actual_files != set(files):
        raise BackupError("archive membership does not match the manifest")
    for name, metadata in files.items():
        if not isinstance(name, str) or not isinstance(metadata, dict):
            raise BackupError("invalid file metadata in manifest")
        path = destination.joinpath(*PurePosixPath(name).parts)
        if path.stat().st_size != metadata.get("size"):
            raise BackupError(f"restored file size mismatch: {name}")
        if sha256_file(path) != metadata.get("sha256"):
            raise BackupError(f"restored file digest mismatch: {name}")

    checked = _check_database(database_path)
    try:
        _validate_attachment_rows(checked, attachments_path)
    finally:
        checked.close()


def restore_archive(archive_path: Path, destination: Path) -> None:
    if destination.exists():
        raise BackupError("restore destination must not already exist")
    try:
        _extract_archive(archive_path, destination)
        validate_restored_tree(destination)
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and validate Oveo backup payloads")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--database", type=Path, required=True)
    create.add_argument("--attachments", type=Path, required=True)
    create.add_argument("--archive", type=Path, required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    verify = subparsers.add_parser("verify-tree")
    verify.add_argument("--destination", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == "create":
            create_archive(arguments.database, arguments.attachments, arguments.archive)
        elif arguments.command == "restore":
            restore_archive(arguments.archive, arguments.destination)
        else:
            validate_restored_tree(arguments.destination)
    except BackupError as error:
        parser.exit(1, f"backup validation failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
