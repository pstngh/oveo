from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from fastapi import UploadFile

from oveo.docx import DocxBlock, DocxError, docx_uncompressed_limit, extract_docx
from oveo.workers import BoundedWorker, WorkerBusy

_MANAGED_DOCX_NAME = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.docx\Z"
)


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
    byte_count: int
    word_count: int
    sha256: str
    document_blocks: tuple[DocxBlock, ...]


def count_words(text: str) -> int:
    """Count Unicode non-whitespace runs consistently across input and canonical state."""

    return len(re.findall(r"\S+", text, flags=re.UNICODE))


def _validated_docx(name: str, content: bytes, max_bytes: int) -> ValidatedAttachment:
    extracted = extract_docx(content, max_uncompressed_bytes=docx_uncompressed_limit(max_bytes))
    return ValidatedAttachment(
        original_name=name,
        content=content,
        byte_count=len(content),
        word_count=count_words(extracted.plain_text),
        sha256=hashlib.sha256(content).hexdigest(),
        document_blocks=extracted.blocks,
    )


async def validate_attachment_upload(
    upload: UploadFile, *, max_bytes: int, worker: BoundedWorker
) -> ValidatedAttachment:
    name = Path((upload.filename or "").replace("\\", "/")).name
    suffix = Path(name).suffix.lower()
    if suffix != ".docx":
        raise AttachmentError(
            "invalid_attachment_type",
            "Only .docx source files are supported.",
        )

    # Read one byte beyond the ceiling so an exact-limit file remains valid.
    content = await upload.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise AttachmentError(
            "attachment_too_large",
            f"The source file is larger than the {max_bytes:,}-byte limit.",
            status_code=413,
        )
    try:
        # Parsing is CPU-bound; the dedicated worker keeps the event loop responsive and
        # turns work away instead of running several large parses at once.
        return await worker.run(partial(_validated_docx, name, content, max_bytes))
    except WorkerBusy as exc:
        raise AttachmentError(
            "document_worker_busy",
            "Oveo is processing another document. Try again in a moment.",
            status_code=503,
        ) from exc
    except DocxError as exc:
        raise AttachmentError(exc.code, exc.message) from exc


def is_managed_attachment_name(name: str) -> bool:
    return _MANAGED_DOCX_NAME.fullmatch(name) is not None


def persist_attachment(directory: Path, storage_name: str, content: bytes) -> Path:
    if not is_managed_attachment_name(storage_name):
        raise ValueError("unsafe attachment storage name")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / storage_name
    target.write_bytes(content)
    target.chmod(0o600)
    return target
