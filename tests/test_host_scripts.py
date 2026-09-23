"""Run the host scripts against temporary stand-ins for every host path.

In production the scripts run as root on the Debian host. Here each one runs as the
root of an unprivileged user namespace (`unshare --map-auto --map-root-user`) with GNU
coreutils, every absolute host path rewritten into a temporary directory, and docker,
curl, age, logger and sleep replaced by stubs. Nothing outside the temporary directory
is read or changed. The tests are skipped where such namespaces are unavailable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from argon2 import PasswordHasher

ROOT = Path(__file__).parents[1]
PREVIOUS = "ghcr.io/pstngh/oveo@sha256:" + "1" * 64
CANDIDATE = "ghcr.io/pstngh/oveo@sha256:" + "2" * 64
HOST_PATHS = (
    "/usr/local/lib/oveo",
    "/usr/local/sbin",
    "/etc/systemd/system",
    "/etc/oveo",
    "/opt/oveo",
    "/var/lib/oveo",
    "/var/backups/oveo",
    "/run/lock",
    "/var/tmp",  # noqa: S108 - a host path to rewrite into the stand-in tree
)
_HOST_PATH = re.compile("|".join(re.escape(path) for path in HOST_PATHS))
UNSHARE = shutil.which("unshare")
_UNREWRITTEN = re.compile(r"(?<![\w.-])/(?:etc|var|opt|run|usr/local)/")
# The Debian host runs GNU coreutils; some development systems ship other
# implementations and keep GNU's under a "gnu" prefix.
_COREUTILS = (
    "basename cat chmod chown cp date dirname env head id install ln mkdir mktemp mv rm "
    "sort stat tail touch"
).split()

DOCKER_STUB = r"""#!/usr/bin/env python3
import json, os, sqlite3, sys
from pathlib import Path

state = Path(os.environ["STUB_STATE"])
data_dir = Path(os.environ["STUB_DATA_DIR"])
config = json.loads((state / "images.json").read_text())
args = sys.argv[1:]


def log(line):
    with (state / "docker.log").open("a") as handle:
        handle.write(line + "\n")


def image_from_env_file():
    if "--env-file" in args:
        path = Path(args[args.index("--env-file") + 1])
        if path.exists():
            for line in path.read_text().splitlines():
                if line.startswith("OVEO_IMAGE="):
                    return line.split("=", 1)[1]
    return os.environ.get("OVEO_IMAGE", "")


def database_revision():
    database = data_dir / "oveo.sqlite3"
    if not database.exists():
        return None
    connection = sqlite3.connect(database)
    row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    connection.close()
    return row[0]


if args[:2] in (["manifest", "inspect"], ["image", "rm"], ["image", "ls"]) or args[0] == "pull":
    sys.exit(0)
if args[:2] == ["image", "inspect"]:
    print(args[2])
    sys.exit(0)
if args[0] == "run":
    image = next(arg for arg in args if arg.startswith("ghcr.io/"))
    log("run " + " ".join(args[args.index(image) + 1:]))
    revisions = config["revisions"].get(image)
    if revisions is None:
        sys.exit(2)
    print("\n".join(revisions))
    sys.exit(0)
if args[0] == "compose":
    command = next(arg for arg in args[1:] if arg in {"config", "up", "stop", "ps"})
    if command == "up":
        image = image_from_env_file()
        marker = "yes" if (data_dir / "maintenance-mode").exists() else "no"
        known = config["revisions"].get(image) or []
        healthy = image not in config.get("broken", [])
        current = database_revision()
        if current is not None:
            if current not in known:
                healthy = False  # the entrypoint's "alembic upgrade head" fails
            elif current != known[0]:
                connection = sqlite3.connect(data_dir / "oveo.sqlite3")
                connection.execute("UPDATE alembic_version SET version_num = ?", (known[0],))
                connection.commit()
                connection.close()
                log(f"migrated {current} -> {known[0]}")
        (state / "running").write_text(image)
        (state / "healthy").write_text("yes" if healthy else "no")
        log(f"up {image[-6:]} marker={marker}")
    elif command == "stop":
        (state / "running").write_text("")
        (state / "healthy").write_text("no")
        log("stop")
    else:
        log(command)
    sys.exit(0)
log("unexpected " + " ".join(args))
sys.exit(1)
"""

CURL_STUB = """#!/bin/sh
[ "$(cat "$STUB_STATE/healthy" 2>/dev/null)" = yes ]
"""

AGE_STUB = """#!/bin/sh
# age --recipient R --output OUT IN, or age --decrypt --identity F --output OUT IN
output=
previous=
for argument do
  [ "$previous" = --output ] && output=$argument
  previous=$argument
done
cp -- "$previous" "$output"
"""

LOGGER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >>"$STUB_STATE/logger.log"
"""


def _namespaces_work(probe: Path) -> bool:
    if UNSHARE is None:
        return False
    probe.write_text("probe", encoding="utf-8")
    try:
        result = subprocess.run(  # noqa: S603 - fixed arguments, synthetic probe file
            [
                UNSHARE,
                "--map-auto",
                "--map-root-user",
                "sh",
                "-c",
                'chown 10001:10001 "$0" && chown 0:0 "$0" && id -u',
                str(probe),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "0"


@dataclass
class Outcome:
    status: int
    stdout: str
    stderr: str
    shared_modes: dict[str, str]
    owners: dict[str, str]
    docker: list[str]


class Host:
    """A temporary stand-in for the production host's Oveo paths."""

    def __init__(self, base: Path) -> None:
        self.root = base / "host"
        self.state = self.root / "stub-state"
        self.bin = self.state / "bin"
        self.data = self.root / "var/lib/oveo"
        self.backups = self.root / "var/backups/oveo"
        for directory in ("etc/oveo", "opt/oveo", "usr/local/lib/oveo", "usr/local/sbin"):
            (self.root / directory).mkdir(parents=True)
        (self.root / "var/lib").mkdir(parents=True)
        (self.root / "var/backups").mkdir(parents=True)
        for shared in ("run/lock", "var/tmp"):
            (self.root / shared).mkdir(parents=True)
            (self.root / shared).chmod(0o1777)
        self.bin.mkdir(parents=True)
        self._install_stubs()
        for name in ("oveo-deploy", "oveo-backup", "oveo-restore", "oveo-backup-alert"):
            self._install(ROOT / "deploy" / f"{name}.sh", self.root / "usr/local/sbin" / name)
        for name in ("backup_tool.py", "validate_staging.py"):
            self._install(ROOT / "deploy" / name, self.root / "usr/local/lib/oveo" / name)
        (self.root / "opt/oveo/compose.yml").write_text("name: oveo\n", encoding="utf-8")
        self.configure(revisions={PREVIOUS: ["rev1"], CANDIDATE: ["rev1"]})

    def _install(self, source: Path, target: Path) -> None:
        text = _HOST_PATH.sub(lambda match: f"{self.root}{match.group(0)}", source.read_text())
        # Every host path must point into the stand-in tree.
        assert not _UNREWRITTEN.search(text.replace(str(self.root), "STANDIN")), source.name
        target.write_text(text, encoding="utf-8")
        target.chmod(0o755)

    def _install_stubs(self) -> None:
        for name, body in (
            ("docker", DOCKER_STUB),
            ("curl", CURL_STUB),
            ("age", AGE_STUB),
            ("logger", LOGGER_STUB),
            ("sleep", "#!/bin/sh\nexit 0\n"),
        ):
            (self.bin / name).write_text(body, encoding="utf-8")
            (self.bin / name).chmod(0o755)
        if Path("/usr/bin/gnuinstall").exists():
            for name in _COREUTILS:
                gnu = Path(f"/usr/bin/gnu{name}")
                if gnu.exists():
                    (self.bin / name).symlink_to(gnu)

    def configure(self, *, revisions: dict[str, list[str]], broken: tuple[str, ...] = ()) -> None:
        payload = {"revisions": revisions, "broken": list(broken)}
        (self.state / "images.json").write_text(json.dumps(payload), encoding="utf-8")

    def deployment_files(self, *, previous: str | None = PREVIOUS) -> None:
        password_hash = PasswordHasher().hash("synthetic password")
        runtime = self.root / "etc/oveo/runtime.env"
        runtime.write_text(
            f"OVEO_OPENROUTER_API_KEY=sk-or-v1-{'0' * 64}\n"
            f"OVEO_CHARLES_PASSWORD_HASH='{password_hash}'\n"
            f"OVEO_YOUSRA_PASSWORD_HASH='{password_hash}'\n",
            encoding="utf-8",
        )
        runtime.chmod(0o600)
        if previous is not None:
            deploy_env = self.root / "etc/oveo/deploy.env"
            deploy_env.write_text(f"OVEO_IMAGE={previous}\n", encoding="utf-8")
            deploy_env.chmod(0o600)
            (self.state / "running").write_text(previous)
            (self.state / "healthy").write_text("yes")

    def backup_files(self) -> None:
        identity = self.root / "etc/oveo/backup.agekey"
        identity.write_text("AGE-SECRET-KEY-SYNTHETIC\n", encoding="utf-8")
        identity.chmod(0o400)
        config = self.root / "etc/oveo/backup.env"
        config.write_text(
            f"AGE_RECIPIENT=age1synthetic0recipient\nAGE_IDENTITY_FILE={identity}\n",
            encoding="utf-8",
        )
        config.chmod(0o600)

    def data_directory(self, *, revision: str, attachments: dict[str, bytes]) -> None:
        (self.data / "attachments").mkdir(parents=True)
        (self.data / "logs").mkdir()
        (self.data / "logs" / "oveo-errors.log").write_text("error_id=synthetic\n")
        (self.data / "deployed-image").write_text(PREVIOUS + "\n")
        connection = sqlite3.connect(self.data / "oveo.sqlite3")
        connection.executescript(
            """
            CREATE TABLE alembic_version (version_num TEXT NOT NULL);
            CREATE TABLE attachments (
                storage_name TEXT NOT NULL UNIQUE,
                byte_count INTEGER NOT NULL,
                sha256 TEXT NOT NULL
            );
            CREATE TABLE users (username TEXT PRIMARY KEY);
            INSERT INTO users VALUES ('synthetic-user');
            """
        )
        connection.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        for name, content in attachments.items():
            connection.execute(
                "INSERT INTO attachments VALUES (?, ?, ?)",
                (name, len(content), hashlib.sha256(content).hexdigest()),
            )
            (self.data / "attachments" / name).write_bytes(content)
        connection.commit()
        connection.close()

    def run(self, script: str, *arguments: str) -> Outcome:
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("OVEO_")
        }
        environment.update(
            PATH=f"{self.bin}:{environment.get('PATH', '/usr/bin:/bin')}",
            STUB_STATE=str(self.state),
            STUB_DATA_DIR=str(self.data),
        )
        shared = " ".join(f'"{self.root / path}"' for path in ("run/lock", "var/tmp"))
        observe = f"""
set +e
"$@" >"$STUB_STATE/stdout" 2>"$STUB_STATE/stderr"
echo $? >"$STUB_STATE/status"
stat -c '%a %n' {shared} >"$STUB_STATE/shared-modes"
for path in "{self.root}"/var/lib/*; do stat -c '%u:%g %n' "$path"; done >"$STUB_STATE/owners"
chown -R 0:0 "{self.root}"
find "{self.root}" -type d -exec chmod u+rwx {{}} +
"""
        assert UNSHARE is not None
        subprocess.run(  # noqa: S603 - our own scripts, run inside the stand-in tree
            [
                UNSHARE,
                "--map-auto",
                "--map-root-user",
                "sh",
                "-c",
                observe,
                "sh",
                str(self.root / "usr/local/sbin" / script),
                *arguments,
            ],
            env=environment,
            timeout=300,
            check=True,
        )
        read = lambda name: (self.state / name).read_text(encoding="utf-8")  # noqa: E731
        docker_log = self.state / "docker.log"
        outcome = Outcome(
            status=int(read("status")),
            stdout=read("stdout"),
            stderr=read("stderr"),
            shared_modes={
                Path(line.split()[1]).name: line.split()[0]
                for line in read("shared-modes").splitlines()
            },
            owners={
                Path(line.split()[1]).name: line.split()[0] for line in read("owners").splitlines()
            },
            docker=docker_log.read_text().splitlines() if docker_log.exists() else [],
        )
        docker_log.unlink(missing_ok=True)
        return outcome

    def revision(self, directory: Path) -> str:
        connection = sqlite3.connect(directory / "oveo.sqlite3")
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        connection.close()
        return str(row[0])

    def siblings(self, prefix: str) -> list[Path]:
        return sorted(
            path for path in (self.root / "var/lib").iterdir() if path.name.startswith(prefix)
        )


@pytest.fixture
def host(tmp_path: Path) -> Host:
    if not _namespaces_work(tmp_path / "probe"):
        pytest.skip("unprivileged user namespaces with subordinate IDs are unavailable")
    return Host(tmp_path)


def _deployed(host: Host) -> str:
    return (host.root / "etc/oveo/deploy.env").read_text(encoding="utf-8").strip()


# --- M-13: shared directories keep their modes --------------------------------------------


def test_scripts_never_change_the_shared_lock_and_scratch_directories(host: Host) -> None:
    host.deployment_files()
    host.backup_files()
    host.data_directory(revision="rev1", attachments={"a.docx": b"synthetic"})

    deploy = host.run("oveo-deploy", CANDIDATE)
    backup = host.run("oveo-backup")
    archive = next(host.backups.glob("oveo-*.tar.gz.age"))
    restore = host.run("oveo-restore", str(archive), "--destination", str(host.root / "check"))

    for outcome in (deploy, backup, restore):
        assert outcome.status == 0, outcome.stderr
        assert outcome.shared_modes == {"lock": "1777", "tmp": "1777"}
    assert (host.root / "run/lock/oveo-maintenance.lock").exists()


# --- M-3: deployments with and without a migration ---------------------------------------


def test_deploy_without_a_migration_keeps_the_fast_path(host: Host) -> None:
    host.deployment_files()
    host.data_directory(revision="rev1", attachments={"a.docx": b"synthetic"})

    outcome = host.run("oveo-deploy", CANDIDATE)

    assert outcome.status == 0, outcome.stderr
    assert _deployed(host) == f"OVEO_IMAGE={CANDIDATE}"
    assert "stop" not in outcome.docker
    assert outcome.docker[-2:] == [f"up {CANDIDATE[-6:]} marker=no", "ps"]
    assert host.siblings("oveo.") == []
    assert not (host.data / "maintenance-mode").exists()
    assert (host.data / "deployed-image").read_text().strip() == CANDIDATE
    assert outcome.owners["oveo"] == "10001:10001"


def test_failed_deploy_without_a_migration_restores_the_previous_image(host: Host) -> None:
    host.deployment_files()
    host.data_directory(revision="rev1", attachments={"a.docx": b"synthetic"})
    host.configure(revisions={PREVIOUS: ["rev1"], CANDIDATE: ["rev1"]}, broken=(CANDIDATE,))

    outcome = host.run("oveo-deploy", CANDIDATE)

    assert outcome.status == 1
    assert "preceding image was restored" in outcome.stderr
    assert _deployed(host) == f"OVEO_IMAGE={PREVIOUS}"
    assert (host.state / "running").read_text() == PREVIOUS
    assert (host.data / "deployed-image").read_text().strip() == PREVIOUS


def test_migrating_deploy_stops_snapshots_and_gates_the_candidate(host: Host) -> None:
    host.deployment_files()
    host.data_directory(revision="rev1", attachments={"a.docx": b"synthetic"})
    host.configure(revisions={PREVIOUS: ["rev1"], CANDIDATE: ["rev2", "rev1"]})

    outcome = host.run("oveo-deploy", CANDIDATE)

    assert outcome.status == 0, outcome.stderr
    assert "unavailable until the candidate passes its checks" in outcome.stderr
    # Stopped before the snapshot, then migrated and started behind the write gate.
    stop = outcome.docker.index("stop")
    migrated = outcome.docker.index("migrated rev1 -> rev2")
    assert stop < migrated < outcome.docker.index(f"up {CANDIDATE[-6:]} marker=yes")
    assert not (host.data / "maintenance-mode").exists()
    assert host.revision(host.data) == "rev2"
    [snapshot] = host.siblings("oveo.predeploy.")
    assert host.revision(snapshot) == "rev1"
    assert (snapshot / "attachments" / "a.docx").read_bytes() == b"synthetic"
    assert (snapshot / "logs" / "oveo-errors.log").exists()
    assert not (snapshot / "maintenance-mode").exists()
    assert outcome.owners[snapshot.name] == "10001:10001"
    assert f"Pre-deployment data kept at {snapshot}" in outcome.stdout


def test_failed_migrating_deploy_puts_back_the_whole_data_directory(host: Host) -> None:
    host.deployment_files()
    host.data_directory(revision="rev1", attachments={"a.docx": b"synthetic"})
    host.configure(revisions={PREVIOUS: ["rev1"], CANDIDATE: ["rev2", "rev1"]}, broken=(CANDIDATE,))

    outcome = host.run("oveo-deploy", CANDIDATE)

    assert outcome.status == 1
    [failed] = host.siblings("oveo.failed-deploy.")
    assert f"the candidate's data is kept at {failed}" in outcome.stderr
    # The previous image runs on the pre-migration data; the migrated copy is kept aside.
    assert host.revision(host.data) == "rev1"
    assert host.revision(failed) == "rev2"
    assert (failed / "maintenance-mode").exists()
    assert (host.data / "attachments" / "a.docx").read_bytes() == b"synthetic"
    assert not (host.data / "maintenance-mode").exists()
    assert host.siblings("oveo.predeploy.") == []
    assert _deployed(host) == f"OVEO_IMAGE={PREVIOUS}"
    assert (host.state / "running").read_text() == PREVIOUS
    assert (host.state / "healthy").read_text() == "yes"
    assert outcome.docker[-1] == f"up {PREVIOUS[-6:]} marker=no"


def test_candidate_that_cannot_list_migrations_takes_the_safe_path(host: Host) -> None:
    host.deployment_files()
    host.data_directory(revision="rev1", attachments={})
    host.configure(revisions={PREVIOUS: ["rev1"]})  # an image older than schema-revisions

    outcome = host.run("oveo-deploy", CANDIDATE)

    # Unknown to the stub, the candidate "crashes"; the snapshot makes that harmless.
    assert "cannot list its migrations" in outcome.stderr
    assert outcome.status == 1
    assert host.revision(host.data) == "rev1"
    assert len(host.siblings("oveo.failed-deploy.")) == 1


def test_deploy_refuses_a_database_newer_than_the_candidate(host: Host) -> None:
    host.deployment_files()
    host.data_directory(revision="rev3", attachments={})
    host.configure(revisions={PREVIOUS: ["rev3", "rev2", "rev1"], CANDIDATE: ["rev2", "rev1"]})

    outcome = host.run("oveo-deploy", CANDIDATE)

    assert outcome.status == 1
    assert "which the candidate does not know" in outcome.stderr
    assert not any(line.startswith(("up", "stop")) for line in outcome.docker)
    assert _deployed(host) == f"OVEO_IMAGE={PREVIOUS}"


def test_deploy_stops_for_a_person_after_an_interrupted_deployment(host: Host) -> None:
    host.deployment_files()
    host.data_directory(revision="rev1", attachments={})
    (host.data / "maintenance-mode").touch()

    outcome = host.run("oveo-deploy", CANDIDATE)

    assert outcome.status == 1
    assert "an earlier deployment did not finish" in outcome.stderr
    assert outcome.docker == []
    assert (host.data / "maintenance-mode").exists()


# --- L-19: backups, alerts and restores ------------------------------------------------


def test_backup_leaves_out_unreferenced_files_and_clears_the_failure_record(host: Host) -> None:
    host.backup_files()
    host.data_directory(revision="rev1", attachments={"a.docx": b"synthetic"})
    (host.data / "attachments" / "never-committed.docx").write_bytes(b"orphan")
    host.backups.mkdir(parents=True)
    (host.backups / "BACKUP-FAILED").write_text("earlier failure\n")

    outcome = host.run("oveo-backup")

    assert outcome.status == 0, outcome.stderr
    assert "1 unreferenced attachment file(s) were not archived" in outcome.stderr
    assert len(list(host.backups.glob("oveo-*.tar.gz.age"))) == 1
    assert not (host.backups / "BACKUP-FAILED").exists()


def test_incomplete_backup_is_kept_apart_and_never_rotates_complete_ones(host: Host) -> None:
    host.backup_files()
    host.data_directory(revision="rev1", attachments={"a.docx": b"synthetic"})
    (host.data / "attachments" / "a.docx").unlink()
    host.backups.mkdir(parents=True)
    complete = [host.backups / f"oveo-2026010{day}T000000Z.tar.gz.age" for day in range(1, 8)]
    for path in complete:
        path.write_bytes(b"synthetic complete backup")

    outcome = host.run("oveo-backup")

    assert outcome.status == 1
    assert "INCOMPLETE backup kept at" in outcome.stderr
    assert sorted(host.backups.glob("oveo-*.tar.gz.age")) == complete
    [incomplete] = (host.backups / "incomplete").glob("oveo-INCOMPLETE-*.tar.gz.age")
    refused = host.run("oveo-restore", str(incomplete), "--destination", str(host.root / "r1"))
    assert refused.status == 1
    assert "INCOMPLETE" in refused.stderr
    assert not (host.root / "r1").exists()
    allowed = host.run(
        "oveo-restore",
        str(incomplete),
        "--destination",
        str(host.root / "r2"),
        "--allow-incomplete",
    )
    assert allowed.status == 0, allowed.stderr
    assert host.revision(host.root / "r2") == "rev1"


def test_backup_failure_alert_stays_on_the_host(host: Host) -> None:
    host.backups.mkdir(parents=True)

    outcome = host.run("oveo-backup-alert")

    assert outcome.status == 0, outcome.stderr
    record = (host.backups / "BACKUP-FAILED").read_text(encoding="utf-8")
    assert record.startswith("The Oveo backup failed at ")
    assert stat.S_IMODE((host.backups / "BACKUP-FAILED").stat().st_mode) == 0o600
    assert "-p daemon.crit -t oveo-backup" in (host.state / "logger.log").read_text()


def test_live_restore_gates_writes_and_keeps_host_logs(host: Host) -> None:
    host.deployment_files()
    host.backup_files()
    host.data_directory(revision="rev1", attachments={"a.docx": b"synthetic"})
    assert host.run("oveo-backup").status == 0
    archive = next(host.backups.glob("oveo-*.tar.gz.age"))
    (host.data / "logs" / "oveo-errors.log").write_text("error_id=after-backup\n")
    (host.data / "attachments" / "later.docx").write_bytes(b"written after the backup")

    outcome = host.run("oveo-restore", str(archive), "--live", "--confirm", "RESTORE")

    assert outcome.status == 0, outcome.stderr
    assert outcome.docker == ["stop", f"up {PREVIOUS[-6:]} marker=yes"]
    assert not (host.data / "maintenance-mode").exists()
    assert (host.data / "logs" / "oveo-errors.log").read_text() == "error_id=after-backup\n"
    assert (host.data / "deployed-image").read_text().strip() == PREVIOUS
    assert not (host.data / "attachments" / "later.docx").exists()
    [previous] = host.siblings("oveo.pre-restore.")
    assert (previous / "attachments" / "later.docx").exists()
    assert outcome.owners["oveo"] == "10001:10001"
