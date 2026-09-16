from __future__ import annotations

import hashlib
import importlib.util
import io
import sqlite3
import tarfile
from pathlib import Path
from types import ModuleType

import pytest


def _load_backup_tool() -> ModuleType:
    path = Path(__file__).parents[1] / "deploy" / "backup_tool.py"
    spec = importlib.util.spec_from_file_location("oveo_backup_tool", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup_tool = _load_backup_tool()


def _database(path: Path, attachment: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE attachments (
            storage_name TEXT NOT NULL UNIQUE,
            byte_count INTEGER NOT NULL,
            sha256 TEXT NOT NULL
        );
        CREATE TABLE users (username TEXT PRIMARY KEY);
        INSERT INTO users VALUES ('synthetic-user');
        """
    )
    content = attachment.read_bytes()
    connection.execute(
        "INSERT INTO attachments VALUES (?, ?, ?)",
        (attachment.name, len(content), hashlib.sha256(content).hexdigest()),
    )
    connection.commit()
    connection.close()


def test_backup_round_trip_uses_consistent_sqlite_snapshot(tmp_path: Path) -> None:
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    attachment = attachments / "synthetic.txt"
    attachment.write_text("Synthetic attachment only.\n", encoding="utf-8")
    database = tmp_path / "oveo.sqlite3"
    _database(database, attachment)
    archive = tmp_path / "backup.tar.gz"

    backup_tool.create_archive(database, attachments, archive)
    attachment.write_text("changed after backup", encoding="utf-8")
    restored = tmp_path / "restored"
    backup_tool.restore_archive(archive, restored)

    assert (restored / "attachments" / "synthetic.txt").read_text(encoding="utf-8") == (
        "Synthetic attachment only.\n"
    )
    connection = sqlite3.connect(restored / "oveo.sqlite3")
    assert connection.execute("SELECT username FROM users").fetchall() == [("synthetic-user",)]
    connection.close()


def test_backup_rejects_attachment_digest_mismatch(tmp_path: Path) -> None:
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    attachment = attachments / "synthetic.txt"
    attachment.write_text("expected", encoding="utf-8")
    database = tmp_path / "oveo.sqlite3"
    _database(database, attachment)
    attachment.write_text("tampered", encoding="utf-8")

    with pytest.raises(backup_tool.BackupError, match="digest mismatch"):
        backup_tool.create_archive(database, attachments, tmp_path / "backup.tar.gz")


def test_backup_rejects_unreferenced_attachment_file(tmp_path: Path) -> None:
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    attachment = attachments / "synthetic.txt"
    attachment.write_text("expected", encoding="utf-8")
    database = tmp_path / "oveo.sqlite3"
    _database(database, attachment)
    (attachments / "orphan.txt").write_text("must not be archived", encoding="utf-8")

    with pytest.raises(backup_tool.BackupError, match="not referenced"):
        backup_tool.create_archive(database, attachments, tmp_path / "backup.tar.gz")


def test_restore_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("../escape")
        payload = b"unsafe"
        member.size = len(payload)
        output.addfile(member, io.BytesIO(payload))

    with pytest.raises(backup_tool.BackupError, match="unsafe member path"):
        backup_tool.restore_archive(archive, tmp_path / "restored")
    assert not (tmp_path / "escape").exists()
