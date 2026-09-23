from __future__ import annotations

import io
import random
import time
import zipfile

import pytest
from lxml import etree  # type: ignore[import-untyped]

import oveo.docx as docx_module
from oveo.docx import (
    DocxError,
    DocxReplacement,
    docx_blocks_from_storage,
    docx_uncompressed_limit,
    extract_docx,
    render_docx,
    require_matching_blocks,
)
from tests.docx_fixtures import TEXTBOX_NAMESPACES, R, W, make_docx, textbox_document


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


def _text_values(package: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        root = etree.fromstring(archive.read("word/document.xml"))
    return [node.text or "" for node in root.iter(f"{{{W}}}t")]


@pytest.mark.parametrize("anchor_first", [True, False])
def test_text_boxes_are_neither_extracted_nor_rewritten(anchor_first: bool) -> None:
    template = make_docx(document_xml=textbox_document(anchor_first=anchor_first))
    extracted = extract_docx(template)
    assert [block.text for block in extracted.blocks] == [
        "Intro paragraph.",
        "Quarterly results improved.",
    ]

    exported = render_docx(
        template,
        (
            DocxReplacement(id="p000001", text="Paragraphe d'introduction."),
            DocxReplacement(id="p000002", text="Les résultats trimestriels ont progressé."),
        ),
    )
    values = _text_values(exported)
    # The anchoring paragraph's own text is replaced, never erased or duplicated, and
    # the text box keeps its original text in both the DrawingML and VML versions.
    assert values.count("Les résultats trimestriels ont progressé.") == 1
    assert values.count("Callout text") == 2
    assert "Quarterly results improved." not in values
    assert extract_docx(exported).plain_text == (
        "Paragraphe d'introduction.\n\nLes résultats trimestriels ont progressé."
    )


def test_hyperlinks_and_fields_inside_text_boxes_do_not_affect_the_anchor() -> None:
    box = (
        '<w:p><w:hyperlink r:id="rId5"><w:r><w:t>boxed link</w:t></w:r></w:hyperlink></w:p>'
        '<w:p><w:fldSimple w:instr="PAGE"><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>'
    )
    extracted = extract_docx(make_docx(document_xml=textbox_document(anchor_first=True, box=box)))
    assert [block.text for block in extracted.blocks] == [
        "Intro paragraph.",
        "Quarterly results improved.",
    ]


def test_paragraph_with_its_own_alternate_text_is_left_untouched() -> None:
    document = (
        f"<w:document {TEXTBOX_NAMESPACES}><w:body>"
        "<w:p><w:r><w:t>Editable.</w:t></w:r></w:p>"
        '<w:p><mc:AlternateContent><mc:Choice Requires="w14"><w:r><w:t>Symbol</w:t></w:r>'
        "</mc:Choice><mc:Fallback><w:r><w:t>Symbol</w:t></w:r></mc:Fallback>"
        "</mc:AlternateContent></w:p><w:sectPr/></w:body></w:document>"
    )
    template = make_docx(document_xml=document)
    assert [block.text for block in extract_docx(template).blocks] == ["Editable."]
    exported = render_docx(template, (DocxReplacement(id="p000001", text="Modifiable."),))
    assert _text_values(exported) == ["Modifiable.", "Symbol", "Symbol"]


def test_stored_block_maps_from_the_unsafe_extractor_fail_closed() -> None:
    template = make_docx(document_xml=textbox_document(anchor_first=True))
    current = extract_docx(template).blocks
    require_matching_blocks(current, [block.to_model() for block in current])
    # What the earlier extractor stored for this package: text-box text folded into
    # the anchor and repeated as two extra blocks.
    legacy = [
        {"id": "p000001", "kind": "paragraph", "text": "Intro paragraph."},
        {
            "id": "p000002",
            "kind": "paragraph",
            "text": "Callout textCallout textQuarterly results improved.",
        },
        {"id": "p000003", "kind": "paragraph", "text": "Callout text"},
        {"id": "p000004", "kind": "paragraph", "text": "Callout text"},
    ]
    with pytest.raises(DocxError) as caught:
        render_docx(
            template,
            [{"id": block["id"], "text": block["text"]} for block in legacy],
            stored_blocks=legacy,
        )
    assert caught.value.code == "docx_template_outdated"
    assert "Upload the document again" in caught.value.message
    with pytest.raises(DocxError, match="Upload the document again"):
        require_matching_blocks(current, [])


def test_element_and_paragraph_caps_reject_expansion_before_building_a_tree() -> None:
    # About 7 MB of XML from a package of a few hundred KB, within every byte and
    # compression-ratio limit (occasional random attributes, as in audit probe p08).
    rng = random.Random(9)  # noqa: S311 - deterministic synthetic fixture, not security
    empty = "".join(
        f'<w:p w:rsidR="{rng.getrandbits(32):08X}"/>' if index % 40 == 0 else "<w:p/>"
        for index in range(docx_module.MAX_DOCX_PARAGRAPHS * 60)
    )
    document = (
        f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{empty}'
        "<w:p><w:r><w:t>One visible sentence.</w:t></w:r></w:p><w:sectPr/></w:body></w:document>"
    )
    package = make_docx(document_xml=document)
    assert len(package) < 2_000_000
    started = time.perf_counter()
    with pytest.raises(DocxError) as caught:
        extract_docx(package, max_uncompressed_bytes=docx_uncompressed_limit(2_000_000))
    assert caught.value.code == "docx_too_complex"
    assert time.perf_counter() - started < 5  # the unbounded parse took ~23 s (p08)


def test_representative_large_document_is_processed_within_bounds() -> None:
    words = "policy employees office report hours weekly guideline approval fiscal benefits"
    vocabulary = words.split()
    paragraphs = "".join(
        f"<w:p><w:pPr><w:spacing w:after='120'/></w:pPr><w:r><w:rPr><w:sz w:val='22'/></w:rPr>"
        f"<w:t>{' '.join(vocabulary[(index + offset) % 10] for offset in range(10))}.</w:t>"
        "</w:r></w:p>"
        for index in range(2_500)
    )
    document = f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{paragraphs}</w:body></w:document>'
    package = make_docx(document_xml=document)
    limit = docx_uncompressed_limit(2_000_000)
    started = time.perf_counter()
    extracted = extract_docx(package, max_uncompressed_bytes=limit)
    rendered = render_docx(
        package,
        [{"id": block.id, "text": block.text.upper()} for block in extracted.blocks],
        max_uncompressed_bytes=limit,
        stored_blocks=[block.to_model() for block in extracted.blocks],
    )
    elapsed = time.perf_counter() - started
    assert len(extracted.blocks) == 2_500
    assert len(extracted.plain_text.split()) == 25_000
    assert extract_docx(rendered, max_uncompressed_bytes=limit).plain_text == (
        extracted.plain_text.upper()
    )
    assert elapsed < 10
