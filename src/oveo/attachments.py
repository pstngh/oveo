from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile


class AttachmentError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ValidatedAttachment:
    original_name: str
    content: bytes
    text: str
    byte_count: int
    word_count: int
    sha256: str


def count_words(text: str) -> int:
    """Count Unicode non-whitespace runs consistently across input and canonical state."""

    return len(re.findall(r"\S+", text, flags=re.UNICODE))


async def validate_text_upload(upload: UploadFile, *, max_bytes: int) -> ValidatedAttachment:
    name = Path(upload.filename or "").name
    if not name.lower().endswith(".txt"):
        raise AttachmentError("invalid_attachment_type", "Only .txt source files are supported.")

    # Read one byte beyond the ceiling so an exact-limit file remains valid.
    content = await upload.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise AttachmentError(
            "attachment_too_large",
            f"The text file is larger than the {max_bytes:,}-byte limit.",
            status_code=413,
        )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AttachmentError(
            "invalid_text_encoding",
            "The text file must use UTF-8 encoding.",
        ) from exc
    return ValidatedAttachment(
        original_name=name,
        content=content,
        text=text,
        byte_count=len(content),
        word_count=count_words(text),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def persist_attachment(directory: Path, storage_name: str, content: bytes) -> Path:
    if not re.fullmatch(r"[0-9a-f-]{36}\.txt", storage_name):
        raise ValueError("unsafe attachment storage name")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / storage_name
    target.write_bytes(content)
    target.chmod(0o600)
    return target
