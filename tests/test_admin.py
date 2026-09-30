from __future__ import annotations

from pathlib import Path

import pytest

from oveo.admin import main, schema_revisions

ROOT = Path(__file__).parents[1]


def test_schema_revisions_lists_the_shipped_migrations_head_first() -> None:
    revisions = schema_revisions(ROOT / "alembic.ini")
    # Migration files are named <revision>_<slug>.py with revisions like 20260917_0005.
    shipped = sorted(path.name[:13] for path in (ROOT / "migrations" / "versions").glob("*.py"))
    assert revisions[0] == shipped[-1]
    assert sorted(revisions) == shipped


def test_schema_revisions_command_needs_no_settings_or_database(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A deployment runs this inside the candidate image with no runtime configuration.
    monkeypatch.setenv("OVEO_ENVIRONMENT", "production")
    monkeypatch.chdir(tmp_path)
    assert main(["schema-revisions", "--config", str(ROOT / "alembic.ini")]) == 0
    lines = capsys.readouterr().out.split()
    assert lines == schema_revisions(ROOT / "alembic.ini")
    assert list(tmp_path.iterdir()) == []
