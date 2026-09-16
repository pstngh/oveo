#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import re
from pathlib import Path


class StagingError(RuntimeError):
    pass


_ARGON2ID = re.compile(
    r"^\$argon2id\$v=(?P<version>[0-9]+)\$"
    r"m=(?P<memory>[0-9]+),t=(?P<time>[0-9]+),p=(?P<parallelism>[0-9]+)\$"
    r"(?P<salt>[A-Za-z0-9+/]+)\$(?P<digest>[A-Za-z0-9+/]+)$"
)
_OPENROUTER_KEY = re.compile(r"^sk-or-v1-[A-Za-z0-9_-]{32,}$")


def _read_env(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise StagingError("runtime environment file is unreadable") from error
    values: dict[str, str] = {}
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw_value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise StagingError(f"invalid environment assignment on line {number}")
        if key in values:
            raise StagingError(f"duplicate environment key: {key}")
        value = raw_value.strip()
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise StagingError(f"unbalanced quotes for environment key: {key}")
            value = value[1:-1]
        values[key] = value
    return values


def _decode_argon_component(value: str, *, label: str) -> bytes:
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
    except ValueError as error:
        raise StagingError(f"{label} is not valid base64") from error


def _validate_argon2id(value: str, *, label: str) -> None:
    match = _ARGON2ID.fullmatch(value)
    if match is None:
        raise StagingError(f"{label} is not a complete Argon2id encoding")
    if int(match.group("version")) != 19:
        raise StagingError(f"{label} uses an unsupported Argon2 version")
    if int(match.group("memory")) < 8_192:
        raise StagingError(f"{label} uses an unsafe Argon2 memory cost")
    if int(match.group("time")) < 1 or int(match.group("parallelism")) < 1:
        raise StagingError(f"{label} uses invalid Argon2 cost parameters")
    if len(_decode_argon_component(match.group("salt"), label=label)) < 8:
        raise StagingError(f"{label} has an undersized Argon2 salt")
    if len(_decode_argon_component(match.group("digest"), label=label)) < 16:
        raise StagingError(f"{label} has an undersized Argon2 digest")


def validate_runtime(path: Path) -> None:
    values = _read_env(path)
    api_key = values.get("OVEO_OPENROUTER_API_KEY", "")
    if not _OPENROUTER_KEY.fullmatch(api_key):
        raise StagingError("OVEO_OPENROUTER_API_KEY is missing, placeholder, or malformed")
    for key in ("OVEO_CHARLES_PASSWORD_HASH", "OVEO_YOUSRA_PASSWORD_HASH"):
        value = values.get(key, "")
        _validate_argon2id(value, label=key)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate staged Oveo credentials without printing their values"
    )
    parser.add_argument("--runtime", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        validate_runtime(arguments.runtime)
    except StagingError as error:
        parser.exit(1, f"staging validation failed: {error}\n")
    print("Staged Oveo credentials have valid non-placeholder encodings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
