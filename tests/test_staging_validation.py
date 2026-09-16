from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from argon2 import PasswordHasher


def _load_validator() -> ModuleType:
    path = Path(__file__).parents[1] / "deploy" / "validate_staging.py"
    spec = importlib.util.spec_from_file_location("oveo_staging_validator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


def _runtime(path: Path, *, api_key: str, charles_hash: str, yousra_hash: str) -> None:
    path.write_text(
        "\n".join(
            (
                f"OVEO_OPENROUTER_API_KEY={api_key}",
                f"OVEO_CHARLES_PASSWORD_HASH='{charles_hash}'",
                f"OVEO_YOUSRA_PASSWORD_HASH='{yousra_hash}'",
                "",
            )
        ),
        encoding="utf-8",
    )


def test_staging_validator_accepts_complete_non_placeholder_credentials(tmp_path: Path) -> None:
    password_hash = PasswordHasher().hash("synthetic password")
    runtime = tmp_path / "runtime.env"
    _runtime(
        runtime,
        api_key=f"sk-or-v1-{'a' * 64}",
        charles_hash=password_hash,
        yousra_hash=password_hash,
    )

    validator.validate_runtime(runtime)


@pytest.mark.parametrize(
    ("api_key", "password_hash"),
    (
        ("replace-with-staged-secret", "$argon2id$replace-with-staged-hash"),
        (f"sk-or-v1-{'a' * 64}", "$argon2id$replace-with-staged-hash"),
    ),
)
def test_staging_validator_rejects_examples(
    tmp_path: Path, api_key: str, password_hash: str
) -> None:
    runtime = tmp_path / "runtime.env"
    _runtime(
        runtime,
        api_key=api_key,
        charles_hash=password_hash,
        yousra_hash=password_hash,
    )

    with pytest.raises(validator.StagingError):
        validator.validate_runtime(runtime)
