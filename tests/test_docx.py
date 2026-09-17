from __future__ import annotations

import io
import zipfile

import pytest

import oveo.docx as docx_module
from oveo.docx import (
    DocxError,
    DocxReplacement,
    docx_blocks_from_storage,
    docx_uncompressed_limit,
    extract_docx,
    render_docx,
)
from tests.docx_fixtures import R, W, make_docx


def test_extracts_paragraphs_table_cells_and_protected_hyperlinks() -> None:
    extracted = extract_docx(make_docx())

    assert [(block.id, block.kind) for block in extracted.blocks] == [
        ("p000001", "paragraph"),
        ("p000002", "table_cell"),
    ]
    assert extracted.blocks[0].text == ("Hello {{OVEO_LINK_l000001}}site{{/OVEO_LINK_l000001}}.")
    assert extracted.blocks[1].text == "Cell text"
    assert extracted.plain_text == "Hello site.\n\nCell text"


def test_round_trip_patches_only_text_and_preserves_hyperlink_target_and_parts() -> None:
    template = make_docx()
    replacements = (
        DocxReplacement(
            id="p000001",
            text="Bonjour {{OVEO_LINK_l000001}}portail{{/OVEO_LINK_l000001}}!",
        ),
        DocxReplacement(id="p000002", text="Texte de cellule"),
    )

    exported = render_docx(template, replacements)
    reopened = extract_docx(exported)
    assert reopened.plain_text == "Bonjour portail!\n\nTexte de cellule"

    with (
        zipfile.ZipFile(io.BytesIO(template)) as before,
        zipfile.ZipFile(io.BytesIO(exported)) as after,
    ):
        for untouched in (
            "word/_rels/document.xml.rels",
            "word/styles.xml",
            "word/header1.xml",
            "word/footer1.xml",
            "word/media/image1.png",
        ):
            assert after.read(untouched) == before.read(untouched)
        assert b"https://example.com/original" in after.read("word/_rels/document.xml.rels")
        document = after.read("word/document.xml")
        assert b"Heading1" in document
        assert b"TableGrid" in document


def test_render_opens_and_parses_the_package_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    original = docx_module._open_package

    def counted_open(
        content: bytes, *, max_uncompressed_bytes: int
    ) -> tuple[zipfile.ZipFile, tuple[zipfile.ZipInfo, ...]]:
        nonlocal calls
        calls += 1
        return original(content, max_uncompressed_bytes=max_uncompressed_bytes)

    monkeypatch.setattr(docx_module, "_open_package", counted_open)
    render_docx(
        make_docx(),
        (
            DocxReplacement(
                id="p000001",
                text="Bonjour {{OVEO_LINK_l000001}}portail{{/OVEO_LINK_l000001}}!",
            ),
            DocxReplacement(id="p000002", text="Cellule"),
        ),
    )

    assert calls == 1


@pytest.mark.parametrize(
    "text",
    (
        "Bonjour portail!",
        "{{OVEO_LINK_l000002}}portail{{/OVEO_LINK_l000002}}",
        "{{OVEO_LINK_l000001}}un{{/OVEO_LINK_l000001}} "
        "{{OVEO_LINK_l000001}}deux{{/OVEO_LINK_l000001}}",
        "{{OVEO_LINK_l000001}}portail",
    ),
)
def test_missing_unknown_duplicate_or_malformed_hyperlink_tokens_fail_safely(text: str) -> None:
    with pytest.raises(DocxError):
        render_docx(
            make_docx(),
            (
                DocxReplacement(id="p000001", text=text),
                DocxReplacement(id="p000002", text="Cell"),
            ),
        )


def test_rejects_malformed_oversized_macro_and_tracked_change_packages() -> None:
    with pytest.raises(DocxError, match="valid DOCX"):
        extract_docx(b"not a zip")
    with pytest.raises(DocxError, match="safety limit"):
        extract_docx(make_docx(), max_uncompressed_bytes=100)
    macro_type = "application/vnd.ms-word.document.macroEnabled.main+xml"
    with pytest.raises(DocxError, match="Macro-enabled"):
        extract_docx(make_docx(main_content_type=macro_type))
    encrypted = bytearray(make_docx())
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        position = 0
        while (position := encrypted.find(signature, position)) != -1:
            flags = int.from_bytes(encrypted[position + flag_offset : position + flag_offset + 2])
            encrypted[position + flag_offset : position + flag_offset + 2] = (flags | 1).to_bytes(
                2, "little"
            )
            position += len(signature)
    with pytest.raises(DocxError, match="Encrypted"):
        extract_docx(bytes(encrypted))
    tracked = f"""<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body><w:p>
<w:ins w:id="1"><w:r><w:t>Tracked</w:t></w:r></w:ins>
</w:p></w:body></w:document>"""
    with pytest.raises(DocxError, match="tracked changes"):
        extract_docx(make_docx(document_xml=tracked))


def test_rejects_complex_field_hyperlinks() -> None:
    field_link = f"""<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body><w:p>
<w:r><w:instrText> HYPERLINK &quot;https://example.com&quot; </w:instrText></w:r>
</w:p></w:body></w:document>"""
    with pytest.raises(DocxError, match="field-code hyperlink"):
        extract_docx(make_docx(document_xml=field_link))


def test_rejects_reserved_tokens_inside_hyperlink_display_text() -> None:
    smuggled = (
        f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body><w:p>'
        '<w:hyperlink r:id="rId5"><w:r><w:t>'
        "{{OVEO_LINK_l000009}}injected{{/OVEO_LINK_l000009}}"
        "</w:t></w:r></w:hyperlink></w:p></w:body></w:document>"
    )

    with pytest.raises(DocxError, match="reserved for safe hyperlink handling"):
        extract_docx(make_docx(document_xml=smuggled))


def test_validates_stored_blocks_and_uses_one_expansion_limit() -> None:
    extracted = extract_docx(make_docx())
    stored = [block.to_model() for block in extracted.blocks]
    assert docx_blocks_from_storage(stored) == extracted.blocks

    stored[0]["text"] = (
        "{{OVEO_LINK_l000001}}{{OVEO_LINK_l000009}}injected"
        "{{/OVEO_LINK_l000009}}{{/OVEO_LINK_l000001}}"
    )
    with pytest.raises(DocxError, match="malformed"):
        docx_blocks_from_storage(stored)

    assert docx_uncompressed_limit(1_000_000) == 20_000_000
    assert docx_uncompressed_limit(2_000_000) == 40_000_000
    assert docx_uncompressed_limit(10_000_000) == 50_000_000
