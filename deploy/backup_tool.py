#!/usr/bin/env python3
"""Create, check and restore Oveo backups, and take pre-deployment snapshots.

Runs on the host as root with only the Python standard library.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote

MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
SCHEMA_VERSION = 1
# `create --allow-incomplete` exits with this status after writing an archive that is
# missing referenced attachments.
EXIT_INCOMPLETE = 3
# Besides the database and attachments, a pre-deployment snapshot keeps these.
_SNAPSHOT_FILES = ("deployed-image",)
_SNAPSHOT_DIRECTORIES = ("logs",)


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupResult:
    """What a backup contains: `missing` lists referenced attachments left out."""

    missing: tuple[str, ...]
    unreferenced: int

    @property
    def complete(self) -> bool:
        return not self.missing


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


def _regular_files(root: Path) -> set[str]:
    """Relative names of every regular file below `root`; links and devices are refused."""

    if not root.exists():
        return set()
    found: set[str] = set()
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            if (current_path / name).is_symlink():
                raise BackupError(f"symlink is not allowed in attachments: {current_path / name}")
        for name in files:
            path = current_path / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise BackupError(f"non-regular attachment is not allowed: {path}")
            found.add(path.relative_to(root).as_posix())
    return found


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


def _referenced_attachments(database: sqlite3.Connection) -> list[tuple[str, int, str]]:
    has_table = database.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='attachments'"
    ).fetchone()
    if has_table is None:
        raise BackupError("attachments table is missing")
    rows: list[tuple[str, int, str]] = []
    for storage_name, byte_count, expected_sha256 in database.execute(
        "SELECT storage_name, byte_count, sha256 FROM attachments"
    ):
        if not isinstance(storage_name, str) or not _safe_storage_name(storage_name):
            raise BackupError("database contains an unsafe attachment storage name")
        rows.append((storage_name, byte_count, expected_sha256))
    return rows


def _intact(path: Path, byte_count: int, expected_sha256: str) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(mode)
        and path.stat().st_size == byte_count
        and sha256_file(path) == expected_sha256
    )


def _copy_referenced(
    references: list[tuple[str, int, str]], source: Path, destination: Path
) -> tuple[str, ...]:
    """Copy each referenced attachment that can be verified; return the ones that cannot."""

    destination.mkdir(mode=0o700)
    if source.exists() and (source.is_symlink() or not source.is_dir()):
        raise BackupError("attachment source must be a real directory")
    missing: list[str] = []
    for storage_name, byte_count, expected_sha256 in references:
        original = source / storage_name
        target = destination / storage_name
        try:
            if not stat.S_ISREG(original.lstat().st_mode):
                missing.append(storage_name)
                continue
            shutil.copyfile(original, target, follow_symlinks=False)
        except FileNotFoundError:
            missing.append(storage_name)
            continue
        target.chmod(0o600)
        # Checked on the copy, so the archive holds exactly the bytes that were verified.
        if not _intact(target, byte_count, expected_sha256):
            target.unlink()
            missing.append(storage_name)
    return tuple(sorted(missing))


def _normalized_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    info.mode = 0o700 if info.isdir() else 0o600
    return info


def create_archive(
    database_path: Path,
    attachments_path: Path,
    archive_path: Path,
    *,
    allow_incomplete: bool = False,
) -> BackupResult:
    """Archive a consistent database snapshot and the attachments its rows reference.

    Files no row references (for example an upload whose transaction never committed)
    are left out instead of failing the backup. A referenced attachment that is missing
    or damaged fails the backup, unless `allow_incomplete` asks for an archive that
    says in its manifest which attachments it lacks.
    """

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

        # Holding the write lock keeps the database and the attachment directory
        # consistent with each other while both are copied.
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
            checked = _check_database(snapshot)
            try:
                references = _referenced_attachments(checked)
            finally:
                checked.close()
            missing = _copy_referenced(references, attachments_path, copied_attachments)
            referenced_names = {storage_name for storage_name, _, _ in references}
            unreferenced = len(_regular_files(attachments_path) - referenced_names)
        finally:
            if locker.in_transaction:
                locker.rollback()
            locker.close()

        if missing and not allow_incomplete:
            raise BackupError(f"{len(missing)} referenced attachment(s) are missing or damaged")

        files: dict[str, dict[str, int | str]] = {
            "oveo.sqlite3": {
                "size": snapshot.stat().st_size,
                "sha256": sha256_file(snapshot),
            }
        }
        for name in sorted(_regular_files(copied_attachments)):
            path = copied_attachments / name
            files[f"attachments/{name}"] = {
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        manifest: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "complete": not missing,
            "files": files,
        }
        if missing:
            manifest["missing_attachments"] = list(missing)
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
    return BackupResult(missing=missing, unreferenced=unreferenced)


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


def validate_restored_tree(destination: Path, *, allow_incomplete: bool = False) -> None:
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
    # Backups made before incomplete archives existed have no flag and were complete.
    complete = manifest.get("complete", True)
    missing = manifest.get("missing_attachments", [])
    if complete is not True:
        if (
            not isinstance(missing, list)
            or not missing
            or not all(isinstance(name, str) and _safe_storage_name(name) for name in missing)
        ):
            raise BackupError("incomplete backup manifest does not list its missing attachments")
        if not allow_incomplete:
            raise BackupError(
                f"this backup is INCOMPLETE: {len(missing)} attachment(s) are missing; "
                "restore it only deliberately, with --allow-incomplete"
            )
    elif missing:
        raise BackupError("complete backup manifest lists missing attachments")

    if _regular_files(destination) - {"manifest.json"} != set(files):
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
        references = _referenced_attachments(checked)
    finally:
        checked.close()
    expected_missing = set(missing) if complete is not True else set()
    present: set[str] = set()
    for storage_name, byte_count, expected_sha256 in references:
        if storage_name in expected_missing:
            continue
        if not _intact(attachments_path / storage_name, byte_count, expected_sha256):
            raise BackupError(f"referenced attachment is missing or damaged: {storage_name}")
        present.add(storage_name)
    if _regular_files(attachments_path) != present:
        raise BackupError("attachment tree contains files not referenced by the database")
    if not expected_missing <= {storage_name for storage_name, _, _ in references}:
        raise BackupError("manifest lists missing attachments the database does not reference")


def restore_archive(
    archive_path: Path, destination: Path, *, allow_incomplete: bool = False
) -> None:
    if destination.exists():
        raise BackupError("restore destination must not already exist")
    try:
        _extract_archive(archive_path, destination)
        validate_restored_tree(destination, allow_incomplete=allow_incomplete)
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        raise


def database_revision(database_path: Path) -> str | None:
    """The Alembic revision a database is at, or None when there is no database yet."""

    if not database_path.exists():
        return None
    if not database_path.is_file() or database_path.is_symlink():
        raise BackupError("database must be a regular file")
    connection = _open_readonly_database(database_path)
    try:
        has_table = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        rows = (
            connection.execute("SELECT version_num FROM alembic_version").fetchall()
            if has_table is not None
            else []
        )
    finally:
        connection.close()
    if has_table is None:
        return None
    if len(rows) != 1 or not isinstance(rows[0][0], str):
        raise BackupError("the database does not record exactly one schema revision")
    return str(rows[0][0])


def _same_owner(path: Path, reference: os.stat_result) -> None:
    os.chown(path, reference.st_uid, reference.st_gid, follow_symlinks=False)


def _link_or_copy(source: Path, target: Path) -> None:
    try:
        # Attachment files are written once and never modified, so a hard link is an
        # exact, instant copy that later deletions in the live tree do not affect.
        os.link(source, target, follow_symlinks=False)
    except OSError as error:
        if error.errno not in {errno.EXDEV, errno.EPERM, errno.EMLINK}:
            raise
        shutil.copy2(source, target, follow_symlinks=False)
        _same_owner(target, source.lstat())


def snapshot_data_dir(data_dir: Path, destination: Path) -> int:
    """Copy a stopped deployment's data directory so it can be put back whole.

    The database goes through SQLite's backup API, attachments are hard-linked,
    `logs/` and the deployed-image record are copied, and the maintenance marker is
    left out. Returns the number of attachment files.
    """

    if destination.exists():
        raise BackupError("snapshot destination must not already exist")
    if data_dir.is_symlink() or not data_dir.is_dir():
        raise BackupError("data directory must be a real directory")
    destination.mkdir(mode=0o700)
    try:
        _same_owner(destination, data_dir.stat())
        database = data_dir / "oveo.sqlite3"
        if database.exists():
            if database.is_symlink() or not database.is_file():
                raise BackupError("database must be a regular file")
            copied = destination / "oveo.sqlite3"
            locker = sqlite3.connect(database, timeout=30, isolation_level=None)
            try:
                locker.execute("PRAGMA busy_timeout=30000")
                locker.execute("BEGIN IMMEDIATE")
                source = _open_readonly_database(database)
                target = sqlite3.connect(copied)
                try:
                    source.backup(target)
                finally:
                    target.close()
                    source.close()
            finally:
                if locker.in_transaction:
                    locker.rollback()
                locker.close()
            copied.chmod(0o600)
            _same_owner(copied, database.stat())
            _check_database(copied).close()

        attachments = data_dir / "attachments"
        linked = 0
        if attachments.exists():
            names = _regular_files(attachments)
            if any("/" in name for name in names):
                raise BackupError("attachments must not contain subdirectories")
            target_dir = destination / "attachments"
            target_dir.mkdir(mode=0o700)
            _same_owner(target_dir, attachments.stat())
            for name in sorted(names):
                _link_or_copy(attachments / name, target_dir / name)
                linked += 1
            if _regular_files(target_dir) != names:
                raise BackupError("attachment snapshot does not match the data directory")

        for directory_name in _SNAPSHOT_DIRECTORIES:
            directory = data_dir / directory_name
            if not directory.exists():
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise BackupError(f"{directory_name} must be a real directory")
            target_dir = destination / directory_name
            target_dir.mkdir(mode=0o700)
            _same_owner(target_dir, directory.stat())
            for name in sorted(_regular_files(directory)):
                if "/" in name:
                    continue
                shutil.copy2(directory / name, target_dir / name, follow_symlinks=False)
                _same_owner(target_dir / name, (directory / name).lstat())
        for file_name in _SNAPSHOT_FILES:
            record = data_dir / file_name
            if record.is_file() and not record.is_symlink():
                shutil.copy2(record, destination / file_name, follow_symlinks=False)
                _same_owner(destination / file_name, record.lstat())
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return linked


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and validate Oveo backup payloads")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--database", type=Path, required=True)
    create.add_argument("--attachments", type=Path, required=True)
    create.add_argument("--archive", type=Path, required=True)
    create.add_argument(
        "--allow-incomplete",
        action="store_true",
        help=f"write an archive marked incomplete (exit {EXIT_INCOMPLETE}) when attachments "
        "are missing",
    )
    restore = subparsers.add_parser("restore")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--allow-incomplete", action="store_true")
    verify = subparsers.add_parser("verify-tree")
    verify.add_argument("--destination", type=Path, required=True)
    verify.add_argument("--allow-incomplete", action="store_true")
    revision = subparsers.add_parser("revision", help="print the database schema revision")
    revision.add_argument("--database", type=Path, required=True)
    snapshot = subparsers.add_parser("snapshot", help="copy a stopped data directory")
    snapshot.add_argument("--data-dir", type=Path, required=True)
    snapshot.add_argument("--destination", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == "create":
            result = create_archive(
                arguments.database,
                arguments.attachments,
                arguments.archive,
                allow_incomplete=arguments.allow_incomplete,
            )
            if result.unreferenced:
                print(
                    f"{result.unreferenced} unreferenced attachment file(s) were not archived",
                    file=sys.stderr,
                )
            if not result.complete:
                print(
                    f"INCOMPLETE backup: {len(result.missing)} referenced attachment(s) "
                    "are missing or damaged",
                    file=sys.stderr,
                )
                return EXIT_INCOMPLETE
        elif arguments.command == "restore":
            restore_archive(
                arguments.archive,
                arguments.destination,
                allow_incomplete=arguments.allow_incomplete,
            )
        elif arguments.command == "verify-tree":
            validate_restored_tree(
                arguments.destination, allow_incomplete=arguments.allow_incomplete
            )
        elif arguments.command == "revision":
            print(database_revision(arguments.database) or "none")
        else:
            count = snapshot_data_dir(arguments.data_dir, arguments.destination)
            print(f"Snapshot holds the database and {count} attachment file(s).")
    except BackupError as error:
        parser.exit(1, f"backup validation failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
