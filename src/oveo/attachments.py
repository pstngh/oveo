from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from starlette.datastructures import UploadFile

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
    plain_text: str = ""


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
        plain_text=extracted.plain_text,
    )


def upload_limit_message(max_bytes: int) -> str:
    return f"The Word file is larger than the {max_bytes / 1_000_000:g} MB limit."


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
            upload_limit_message(max_bytes),
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


class AttachmentIntegrityError(OSError):
    """A stored attachment is not the file that was uploaded."""


def read_stored_attachment(
    directory: Path, storage_name: str, *, byte_count: int, sha256: str
) -> bytes:
    """Read a stored attachment, failing unless it is exactly the uploaded file.

    Raises `AttachmentIntegrityError` for a changed file or an unsafe name, and another
    `OSError` when the file cannot be read.
    """

    root = directory.resolve()
    path = (root / storage_name).resolve()
    if path.parent != root:
        raise AttachmentIntegrityError("unsafe attachment storage name")
    content = path.read_bytes()
    if len(content) != byte_count or hashlib.sha256(content).hexdigest() != sha256:
        raise AttachmentIntegrityError("attachment integrity mismatch")
    return content


def is_managed_attachment_name(name: str) -> bool:
    return _MANAGED_DOCX_NAME.fullmatch(name) is not None


def persist_attachment(directory: Path, storage_name: str, content: bytes) -> Path:
    if not is_managed_attachment_name(storage_name):
        raise ValueError("unsafe attachment storage name")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / storage_name
    # Private from creation (not chmod-ed after the bytes are written), and never
    # through an existing file or link: storage names are fresh UUIDs.
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target
