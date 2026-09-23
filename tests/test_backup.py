from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest


def _load_backup_tool() -> ModuleType:
    path = Path(__file__).parents[1] / "deploy" / "backup_tool.py"
    spec = importlib.util.spec_from_file_location("oveo_backup_tool", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through it
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
    attachment = attachments / "synthetic.docx"
    attachment.write_text("Synthetic attachment only.\n", encoding="utf-8")
    database = tmp_path / "oveo.sqlite3"
    _database(database, attachment)
    archive = tmp_path / "backup.tar.gz"

    backup_tool.create_archive(database, attachments, archive)
    attachment.write_text("changed after backup", encoding="utf-8")
    restored = tmp_path / "restored"
    backup_tool.restore_archive(archive, restored)

    assert (restored / "attachments" / "synthetic.docx").read_text(encoding="utf-8") == (
        "Synthetic attachment only.\n"
    )
    connection = sqlite3.connect(restored / "oveo.sqlite3")
    assert connection.execute("SELECT username FROM users").fetchall() == [("synthetic-user",)]
    connection.close()


def test_backup_rejects_attachment_digest_mismatch(tmp_path: Path) -> None:
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    attachment = attachments / "synthetic.docx"
    attachment.write_text("expected", encoding="utf-8")
    database = tmp_path / "oveo.sqlite3"
    _database(database, attachment)
    attachment.write_text("tampered", encoding="utf-8")

    with pytest.raises(backup_tool.BackupError, match="missing or damaged"):
        backup_tool.create_archive(database, attachments, tmp_path / "backup.tar.gz")
    assert not (tmp_path / "backup.tar.gz").exists()


def test_backup_leaves_out_unreferenced_files_instead_of_failing(tmp_path: Path) -> None:
    # L-19: an upload whose transaction never committed used to fail every backup.
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    attachment = attachments / "synthetic.docx"
    attachment.write_text("expected", encoding="utf-8")
    database = tmp_path / "oveo.sqlite3"
    _database(database, attachment)
    (attachments / "orphan.docx").write_text("must not be archived", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"

    result = backup_tool.create_archive(database, attachments, archive)

    assert result.complete and result.unreferenced == 1
    with tarfile.open(archive) as members:
        assert "attachments/orphan.docx" not in members.getnames()
    restored = tmp_path / "restored"
    backup_tool.restore_archive(archive, restored)
    assert sorted(path.name for path in (restored / "attachments").iterdir()) == ["synthetic.docx"]


def _two_attachment_database(tmp_path: Path) -> tuple[Path, Path]:
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    kept = attachments / "kept.docx"
    kept.write_text("kept", encoding="utf-8")
    lost = attachments / "lost.docx"
    lost.write_text("lost", encoding="utf-8")
    database = tmp_path / "oveo.sqlite3"
    _database(database, kept)
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO attachments VALUES (?, ?, ?)",
        ("lost.docx", 4, hashlib.sha256(b"lost").hexdigest()),
    )
    connection.commit()
    connection.close()
    return database, attachments


def test_missing_attachment_gives_a_visibly_incomplete_backup_only_when_asked(
    tmp_path: Path,
) -> None:
    database, attachments = _two_attachment_database(tmp_path)
    (attachments / "lost.docx").unlink()
    archive = tmp_path / "backup.tar.gz"

    with pytest.raises(backup_tool.BackupError, match="1 referenced attachment"):
        backup_tool.create_archive(database, attachments, archive)
    result = backup_tool.create_archive(database, attachments, archive, allow_incomplete=True)

    assert not result.complete and result.missing == ("lost.docx",)
    with tarfile.open(archive) as members:
        manifest = json.loads(members.extractfile("manifest.json").read())  # type: ignore[union-attr]
    assert manifest["complete"] is False
    assert manifest["missing_attachments"] == ["lost.docx"]
    with pytest.raises(backup_tool.BackupError, match="INCOMPLETE"):
        backup_tool.restore_archive(archive, tmp_path / "refused")
    assert not (tmp_path / "refused").exists()
    restored = tmp_path / "restored"
    backup_tool.restore_archive(archive, restored, allow_incomplete=True)
    assert sorted(path.name for path in (restored / "attachments").iterdir()) == ["kept.docx"]


def test_symlinked_attachment_is_never_followed(tmp_path: Path) -> None:
    database, attachments = _two_attachment_database(tmp_path)
    outside = tmp_path / "outside.docx"
    outside.write_text("lost", encoding="utf-8")  # same bytes as the expected file
    (attachments / "lost.docx").unlink()
    (attachments / "lost.docx").symlink_to(outside)

    with pytest.raises(backup_tool.BackupError):
        backup_tool.create_archive(database, attachments, tmp_path / "backup.tar.gz")


def test_backups_made_before_the_complete_flag_still_restore(tmp_path: Path) -> None:
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    attachment = attachments / "synthetic.docx"
    attachment.write_text("content", encoding="utf-8")
    database = tmp_path / "oveo.sqlite3"
    _database(database, attachment)
    archive = tmp_path / "backup.tar.gz"
    backup_tool.create_archive(database, attachments, archive)
    unpacked = tmp_path / "unpacked"
    with tarfile.open(archive) as members:
        members.extractall(unpacked, filter="data")
    manifest = json.loads((unpacked / "manifest.json").read_text(encoding="utf-8"))
    del manifest["complete"]
    (unpacked / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    legacy = tmp_path / "legacy.tar.gz"
    with tarfile.open(legacy, "w:gz") as output:
        for name in ("manifest.json", "oveo.sqlite3", "attachments"):
            output.add(unpacked / name, arcname=name)

    backup_tool.restore_archive(legacy, tmp_path / "restored")


def test_revision_reports_the_schema_revision_or_none(tmp_path: Path) -> None:
    database = tmp_path / "oveo.sqlite3"
    assert backup_tool.database_revision(database) is None
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE users (username TEXT)")
    connection.commit()
    assert backup_tool.database_revision(database) is None
    connection.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
    connection.execute("INSERT INTO alembic_version VALUES ('20260917_0005')")
    connection.commit()
    connection.close()
    assert backup_tool.database_revision(database) == "20260917_0005"


def test_snapshot_copies_the_whole_data_directory_for_rollback(tmp_path: Path) -> None:
    data = tmp_path / "data"
    attachments = data / "attachments"
    attachments.mkdir(parents=True)
    attachment = attachments / "synthetic.docx"
    attachment.write_text("immutable upload", encoding="utf-8")
    (attachments / "unreferenced.docx").write_text("kept as found", encoding="utf-8")
    database = data / "oveo.sqlite3"
    _database(database, attachment)
    # A committed row still only in the write-ahead log must be in the snapshot.
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO users VALUES ('only-in-wal')")
    writer.commit()
    (data / "logs").mkdir()
    (data / "logs" / "oveo-errors.log").write_text("error_id=synthetic\n", encoding="utf-8")
    (data / "deployed-image").write_text("ghcr.io/pstngh/oveo@sha256:" + "0" * 64 + "\n")
    (data / "maintenance-mode").touch()
    destination = tmp_path / "data.predeploy"

    assert backup_tool.snapshot_data_dir(data, destination) == 2
    writer.close()

    copy = sqlite3.connect(destination / "oveo.sqlite3")
    assert ("only-in-wal",) in copy.execute("SELECT username FROM users").fetchall()
    copy.close()
    linked = destination / "attachments" / "synthetic.docx"
    assert linked.stat().st_ino == attachment.stat().st_ino
    attachment.unlink()  # later deletions in the live tree do not reach the snapshot
    assert linked.read_text(encoding="utf-8") == "immutable upload"
    assert (destination / "attachments" / "unreferenced.docx").exists()
    assert (destination / "logs" / "oveo-errors.log").exists()
    assert (destination / "deployed-image").exists()
    assert not (destination / "maintenance-mode").exists()
    with pytest.raises(backup_tool.BackupError, match="must not already exist"):
        backup_tool.snapshot_data_dir(data, destination)


def test_snapshot_refuses_links_and_leaves_nothing_behind(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "attachments").mkdir(parents=True)
    (data / "attachments" / "planted.docx").symlink_to(tmp_path)
    destination = tmp_path / "data.predeploy"

    with pytest.raises(backup_tool.BackupError, match="not allowed"):
        backup_tool.snapshot_data_dir(data, destination)
    assert not destination.exists()


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
