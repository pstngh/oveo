from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi import UploadFile

from oveo.attachments import (
    AttachmentError,
    count_words,
    is_managed_attachment_name,
    persist_attachment,
    validate_attachment_upload,
)
from oveo.provider import parse_usage
from oveo.usage import format_lifetime_cost
from tests.docx_fixtures import make_docx


@pytest.mark.asyncio
async def test_docx_upload_is_extracted() -> None:
    content = make_docx()
    upload = UploadFile(filename="source.DOCX", file=io.BytesIO(content))
    result = await validate_attachment_upload(upload, max_bytes=len(content))
    assert result.word_count == 4
    assert [block.text for block in result.document_blocks] == [
        "Hello {{OVEO_LINK_l000001}}site{{/OVEO_LINK_l000001}}.",
        "Cell text",
    ]


@pytest.mark.asyncio
async def test_upload_rejects_text_files_and_byte_overflow() -> None:
    text_upload = UploadFile(filename="source.txt", file=io.BytesIO(b"legacy"))
    with pytest.raises(AttachmentError, match=r"Only \.docx") as invalid_type:
        await validate_attachment_upload(text_upload, max_bytes=100)
    assert invalid_type.value.code == "invalid_attachment_type"

    oversized = UploadFile(filename="source.docx", file=io.BytesIO(b"1234"))
    with pytest.raises(AttachmentError) as error:
        await validate_attachment_upload(oversized, max_bytes=3)
    assert error.value.status_code == 413


def test_attachment_storage_name_cannot_escape_its_directory(tmp_path: Path) -> None:
    attachment_id = "00000000-0000-0000-0000-000000000000"
    safe_name = f"{attachment_id}.docx"
    directory = tmp_path / "attachments"

    assert is_managed_attachment_name(safe_name)
    assert persist_attachment(directory, safe_name, b"safe") == directory / safe_name
    for unsafe_name in (
        f"../{safe_name}",
        f"nested/{safe_name}",
        f"{attachment_id}.DOCX",
        "not-a-uuid.docx",
    ):
        assert not is_managed_attachment_name(unsafe_name)
        with pytest.raises(ValueError, match="unsafe attachment storage name"):
            persist_attachment(directory, unsafe_name, b"unsafe")

    assert not (tmp_path / safe_name).exists()


def test_word_count_and_precise_cost_formatting() -> None:
    assert count_words(" one\n\tdeux  trois ") == 3
    rounded_down = parse_usage({"cost": "0.0000014"})
    rounded_up = parse_usage({"cost": "0.0000015"})
    assert rounded_down is not None and rounded_down.cost_microusd == 1
    assert rounded_up is not None and rounded_up.cost_microusd == 2
    assert format_lifetime_cost(0) == "$0.00"
    assert format_lifetime_cost(1) == "$0.000001"
    assert format_lifetime_cost(12_340_000) == "$12.34"
