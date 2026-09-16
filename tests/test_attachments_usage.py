from __future__ import annotations

import io

import pytest
from fastapi import UploadFile

from oveo.attachments import AttachmentError, count_words, validate_text_upload
from oveo.provider import parse_usage
from oveo.usage import format_lifetime_cost


@pytest.mark.asyncio
async def test_text_upload_accepts_utf8_bom() -> None:
    upload = UploadFile(filename="source.TXT", file=io.BytesIO(b"\xef\xbb\xbfHello world"))
    result = await validate_text_upload(upload, max_bytes=100)
    assert result.text == "Hello world"
    assert result.word_count == 2


@pytest.mark.asyncio
async def test_text_upload_rejects_invalid_utf8_and_byte_overflow() -> None:
    invalid = UploadFile(filename="source.txt", file=io.BytesIO(b"\xff"))
    with pytest.raises(AttachmentError, match="UTF-8"):
        await validate_text_upload(invalid, max_bytes=100)
    oversized = UploadFile(filename="source.txt", file=io.BytesIO(b"1234"))
    with pytest.raises(AttachmentError) as error:
        await validate_text_upload(oversized, max_bytes=3)
    assert error.value.status_code == 413


def test_word_count_and_precise_cost_formatting() -> None:
    assert count_words(" one\n\tdeux  trois ") == 3
    rounded_down = parse_usage({"cost": "0.0000014"})
    rounded_up = parse_usage({"cost": "0.0000015"})
    assert rounded_down is not None and rounded_down.cost_microusd == 1
    assert rounded_up is not None and rounded_up.cost_microusd == 2
    assert format_lifetime_cost(0) == "$0.00"
    assert format_lifetime_cost(1) == "$0.000001"
    assert format_lifetime_cost(12_340_000) == "$12.34"
