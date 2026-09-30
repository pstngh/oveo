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


def _field(kind: str, *, style: bool = False) -> str:
    properties = '<w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>' if style else ""
    return f'<w:r>{properties}<w:fldChar w:fldCharType="{kind}"/></w:r>'


def _field_link(display: str, *, target: str = "https://example.com/policy") -> str:
    """A hyperlink as Word writes it with field codes: code, result, and end."""

    return (
        _field("begin")
        + f'<w:r><w:instrText xml:space="preserve"> HYPERLINK "{target}" </w:instrText></w:r>'
        + _field("separate")
        + f'<w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr><w:t>{display}</w:t></w:r>'
        + _field("end", style=True)
    )


def _field_sequence(paragraph: bytes) -> list[str]:
    """The paragraph's field characters, field code, and text, in document order."""

    sequence = []
    for element in etree.fromstring(paragraph).iter(
        f"{{{W}}}fldChar", f"{{{W}}}instrText", f"{{{W}}}t", f"{{{W}}}br"
    ):
        name = etree.QName(element).localname
        if name == "fldChar":
            sequence.append(element.get(f"{{{W}}}fldCharType"))
        elif name == "br":
            sequence.append(f"<{element.get(f'{{{W}}}type')}>")
        else:
            sequence.append(element.text or "")
    return sequence


def test_field_code_hyperlinks_are_protected_like_hyperlink_elements() -> None:
    template = make_docx(
        document_xml=_body(
            '<w:p><w:r><w:t xml:space="preserve">Consultez la </w:t></w:r>'
            f"{_field_link('politique')}"
            '<w:r><w:t xml:space="preserve"> ou le </w:t></w:r>'
            '<w:hyperlink r:id="rId5"><w:r><w:t>site</w:t></w:r></w:hyperlink>'
            '<w:r><w:t xml:space="preserve"> et le </w:t></w:r>'
            '<w:fldSimple w:instr=" HYPERLINK &quot;https://example.com/guide&quot; ">'
            "<w:r><w:t>guide</w:t></w:r></w:fldSimple><w:r><w:t>.</w:t></w:r></w:p>"
        )
    )
    (block,) = extract_docx(template).blocks
    assert block.text == (
        "Consultez la {{OVEO_LINK_l000001}}politique{{/OVEO_LINK_l000001}} ou le "
        "{{OVEO_LINK_l000002}}site{{/OVEO_LINK_l000002}} et le "
        "{{OVEO_LINK_l000003}}guide{{/OVEO_LINK_l000003}}."
    )

    translated = (
        "Read the {{OVEO_LINK_l000001}}harassment policy{{/OVEO_LINK_l000001}}, the "
        "{{OVEO_LINK_l000002}}website{{/OVEO_LINK_l000002}}, and the "
        "{{OVEO_LINK_l000003}}guide for managers{{/OVEO_LINK_l000003}}."
    )
    exported = render_docx(template, [DocxReplacement(id="p000001", text=translated)])

    assert [block.text for block in extract_docx(exported).blocks] == [translated]
    (paragraph,) = _paragraph_xml(exported)
    # The field code and its target are untouched; only the result changed, and the
    # surrounding text stayed outside the field.
    assert _field_sequence(paragraph)[:6] == [
        "Read the ",
        "begin",
        ' HYPERLINK "https://example.com/policy" ',
        "separate",
        "harassment policy",
        "end",
    ]
    assert b"HYPERLINK &quot;https://example.com/guide&quot;" in paragraph
    assert b"Hyperlink" in paragraph


def test_text_is_added_around_field_hyperlinks_at_their_exact_boundaries() -> None:
    # The paragraph starts and ends with a field; two fields share a run, as does the
    # last field's end with a trailing page break.
    template = make_docx(
        document_xml=_body(
            "<w:p>"
            + _field_link("un").removesuffix(_field("end", style=True))
            + '<w:r><w:fldChar w:fldCharType="end"/><w:fldChar w:fldCharType="begin"/></w:r>'
            + '<w:r><w:instrText xml:space="preserve"> HYPERLINK "https://example.com/2" '
            + "</w:instrText></w:r>"
            + _field("separate")
            + "<w:r><w:t>deux</w:t></w:r>"
            + '<w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>'
            + '<w:fldChar w:fldCharType="end"/><w:br w:type="page"/></w:r></w:p>'
        )
    )
    assert [block.text for block in extract_docx(template).blocks] == [
        "{{OVEO_LINK_l000001}}un{{/OVEO_LINK_l000001}}"
        "{{OVEO_LINK_l000002}}deux{{/OVEO_LINK_l000002}}"
    ]

    rewritten = (
        "First {{OVEO_LINK_l000001}}one{{/OVEO_LINK_l000001}} then "
        "{{OVEO_LINK_l000002}}two{{/OVEO_LINK_l000002}} last."
    )
    exported = render_docx(template, [DocxReplacement(id="p000001", text=rewritten)])

    assert [block.text for block in extract_docx(exported).blocks] == [rewritten]
    (paragraph,) = _paragraph_xml(exported)
    assert _field_sequence(paragraph) == [
        "First ",
        "begin",
        ' HYPERLINK "https://example.com/policy" ',
        "separate",
        "one",
        "end",
        " then ",
        "begin",
        ' HYPERLINK "https://example.com/2" ',
        "separate",
        "two",
        "end",
        " last.",
        "<page>",
    ]
    # The split run keeps its properties, and the added text is not styled as a link.
    root = etree.fromstring(paragraph)
    for run in root.iter(f"{{{W}}}r"):
        if run.find(f"{{{W}}}br") is not None:
            assert run.find(f"{{{W}}}rPr/{{{W}}}rStyle") is not None
        texts = [node.text for node in run.iter(f"{{{W}}}t")]
        if texts in ([" then "], [" last."], ["First "]):
            assert run.find(f"{{{W}}}rPr") is None
    assert b"xmlns" not in paragraph.split(b">", 1)[1]


def test_unchanged_field_hyperlink_paragraphs_export_exactly() -> None:
    template = make_docx(
        document_xml=_body(
            f"<w:p><w:r><w:t xml:space='preserve'>Voir </w:t></w:r>{_field_link('ici')}</w:p>"
        )
    )
    blocks = extract_docx(template).blocks
    exported = render_docx(template, [DocxReplacement(id=b.id, text=b.text) for b in blocks])
    assert _paragraph_xml(exported) == _paragraph_xml(template)


_TOC_ENTRY = (
    _field("begin")
    + '<w:r><w:instrText xml:space="preserve"> HYPERLINK \\l "_Toc1" </w:instrText></w:r>'
    + _field("separate")
    + "<w:r><w:t>Introduction</w:t></w:r><w:r><w:tab/></w:r>"
    + _field("begin")
    + '<w:r><w:instrText xml:space="preserve"> PAGEREF _Toc1 \\h </w:instrText></w:r>'
    + _field("separate")
    + "<w:r><w:t>1</w:t></w:r>"
    + _field("end")
    + _field("end")
)


@pytest.mark.parametrize(
    "untouched",
    [
        # A table of contents: the TOC field spans paragraphs and each entry nests a
        # page reference inside its hyperlink.
        _field("begin")
        + '<w:r><w:instrText xml:space="preserve"> TOC \\o "1-3" \\h </w:instrText></w:r>'
        + _field("separate")
        + _TOC_ENTRY,
        _TOC_ENTRY,
        # A hyperlink field whose code or result continues in another paragraph.
        _field_link("suite").removesuffix(_field("end", style=True)),
        "<w:r><w:t>et fin</w:t></w:r>" + _field("end"),
        # A hyperlink field without a result, and a field that is not a hyperlink.
        '<w:r><w:t xml:space="preserve">Lien : </w:t></w:r>'
        + _field("begin")
        + '<w:r><w:instrText xml:space="preserve"> HYPERLINK "https://example.com" '
        + "</w:instrText></w:r>"
        + _field("end"),
        '<w:r><w:t xml:space="preserve">Page </w:t></w:r><w:fldSimple w:instr=" PAGE ">'
        "<w:r><w:t>3</w:t></w:r></w:fldSimple>",
        # Field markup inside a hyperlink element or a content control.
        '<w:hyperlink r:id="rId5"><w:r><w:t>site</w:t></w:r>'
        + _field("begin")
        + "<w:r><w:instrText> PAGE </w:instrText></w:r>"
        + _field("end")
        + "</w:hyperlink>",
        "<w:sdt><w:sdtContent>" + _field_link("contrôle") + "</w:sdtContent></w:sdt>",
    ],
)
def test_paragraphs_with_other_fields_are_left_untouched(untouched: str) -> None:
    template = make_docx(
        document_xml=_body(
            f"<w:p>{untouched}</w:p><w:p><w:r><w:t>Texte du corps.</w:t></w:r></w:p>"
        )
    )
    assert [block.text for block in extract_docx(template).blocks] == ["Texte du corps."]

    exported = render_docx(template, [DocxReplacement(id="p000001", text="Body text.")])
    before, after = _paragraph_xml(template), _paragraph_xml(exported)
    assert after[0] == before[0]
    assert b"Body text." in after[-1]


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


def _body(paragraphs: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{paragraphs}<w:sectPr/></w:body>'
        "</w:document>"
    )


def _paragraph_xml(document: bytes) -> list[bytes]:
    with zipfile.ZipFile(io.BytesIO(document)) as archive:
        root = etree.fromstring(archive.read("word/document.xml"))
    return [etree.tostring(paragraph) for paragraph in root.iter(f"{{{W}}}p")]


# An address with a soft line break, a form line with a tab and a tab stop in the
# paragraph properties, a bold name, and non-breaking and soft hyphens.
_INLINE_LAYOUT = _body(
    "<w:p><w:r><w:t>123 Main St</w:t><w:br/><w:t>Montreal</w:t></w:r></w:p>"
    '<w:p><w:pPr><w:tabs><w:tab w:val="left" w:pos="2880"/></w:tabs></w:pPr>'
    "<w:r><w:t>Nom :</w:t></w:r><w:r><w:tab/></w:r>"
    "<w:r><w:rPr><w:b/></w:rPr><w:t>Jean</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Jean</w:t><w:noBreakHyphen/><w:t>Pierre, bien</w:t><w:softHyphen/>"
    "<w:t>tôt</w:t></w:r></w:p>"
)


def test_line_breaks_tabs_and_hyphens_inside_a_paragraph_are_kept_as_text() -> None:
    extracted = extract_docx(make_docx(document_xml=_INLINE_LAYOUT))
    assert [block.text for block in extracted.blocks] == [
        "123 Main St\nMontreal",
        "Nom :\tJean",
        "Jean\u2011Pierre, bien\u00adtôt",
    ]


def test_unchanged_paragraphs_export_exactly_with_their_formatting() -> None:
    template = make_docx(document_xml=_INLINE_LAYOUT)
    blocks = extract_docx(template).blocks
    exported = render_docx(template, [DocxReplacement(id=b.id, text=b.text) for b in blocks])
    # Previously the break and the tab moved after the text and "Jean" lost its bold.
    assert _paragraph_xml(exported) == _paragraph_xml(template)


def test_rewritten_text_restores_breaks_tabs_and_hyphens_where_it_starts() -> None:
    template = make_docx(document_xml=_INLINE_LAYOUT)
    rewritten = ["123, rue Main\nMontréal", "Name:\tJean", "Jean\u2011Pierre, soon"]
    exported = render_docx(
        template,
        [
            DocxReplacement(id=f"p{index:06d}", text=text)
            for index, text in enumerate(rewritten, start=1)
        ],
    )
    assert [block.text for block in extract_docx(exported).blocks] == rewritten
    address, form, name = _paragraph_xml(exported)
    assert b"<w:t>123, rue Main</w:t><w:br/><w:t>Montr" in address
    assert b"<w:t>Name:</w:t><w:tab/><w:t>Jean</w:t>" in form
    assert b'w:pos="2880"' in form  # The tab stop is not text and stays.
    assert b"<w:noBreakHyphen/>" in name


def test_page_breaks_stay_where_they_are() -> None:
    template = make_docx(
        document_xml=_body(
            '<w:p><w:r><w:br w:type="page"/><w:t>Chapter two</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>Before</w:t><w:br w:type="page"/><w:t>after</w:t></w:r></w:p>'
        )
    )
    # Text on both sides of a page break cannot be rewritten without moving the break,
    # so that paragraph stays untouched.
    assert [block.text for block in extract_docx(template).blocks] == ["Chapter two"]
    exported = render_docx(template, [DocxReplacement(id="p000001", text="Chapitre deux")])
    chapter, untouched = _paragraph_xml(exported)
    assert chapter.index(b'w:type="page"') < chapter.index(b"Chapitre deux")
    assert untouched == _paragraph_xml(template)[1]


def test_stored_maps_that_glued_breaks_and_tabs_fail_closed() -> None:
    template = make_docx(document_xml=_INLINE_LAYOUT)
    # What the previous extractor stored for this package.
    legacy = [
        {"id": "p000001", "kind": "paragraph", "text": "123 Main StMontreal"},
        {"id": "p000002", "kind": "paragraph", "text": "Nom :Jean"},
        {"id": "p000003", "kind": "paragraph", "text": "JeanPierre, bientôt"},
    ]
    with pytest.raises(DocxError) as caught:
        require_matching_blocks(extract_docx(template).blocks, legacy)
    assert caught.value.code == "docx_template_outdated"


def test_a_paragraph_with_only_layout_characters_is_not_a_block() -> None:
    template = make_docx(
        document_xml=_body(
            "<w:p><w:r><w:tab/><w:br/></w:r></w:p><w:p><w:r><w:t>Text</w:t></w:r></w:p>"
        )
    )
    assert [block.text for block in extract_docx(template).blocks] == ["Text"]


def _rezip(package: bytes, *, replace: dict[str, bytes]) -> tuple[bytes, dict[str, int]]:
    """Write ``package`` again with some members replaced; return local header offsets."""

    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(package)) as source,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            target.writestr(info.filename, replace.get(info.filename, source.read(info)))
        offsets = {info.filename: info.header_offset for info in target.infolist()}
    return output.getvalue(), offsets


def _declare(package: bytearray, offset: int, name: str, *, crc: int, size: int) -> None:
    """Overwrite a member's CRC and uncompressed size in both of its headers."""

    package[offset + 14 : offset + 18] = crc.to_bytes(4, "little")
    package[offset + 22 : offset + 26] = size.to_bytes(4, "little")
    central = package.index(b"PK\x01\x02")  # The first central directory record.
    while True:
        name_length = int.from_bytes(package[central + 28 : central + 30], "little")
        if package[central + 46 : central + 46 + name_length] == name.encode():
            break
        extra = int.from_bytes(package[central + 30 : central + 32], "little")
        comment = int.from_bytes(package[central + 32 : central + 34], "little")
        central += 46 + name_length + extra + comment
    package[central + 16 : central + 20] = crc.to_bytes(4, "little")
    package[central + 24 : central + 28] = size.to_bytes(4, "little")


def test_a_member_that_understates_its_size_is_never_inflated_past_it() -> None:
    import tracemalloc
    import zlib

    real = zipfile.ZipFile(io.BytesIO(make_docx())).read("word/document.xml")
    # The stream holds the document and 48 MiB of spaces; the headers declare only the
    # document, so every size and ratio check passes.
    rebuilt, offsets = _rezip(make_docx(), replace={"word/document.xml": real + b" " * (48 << 20)})
    package = bytearray(rebuilt)
    _declare(
        package,
        offsets["word/document.xml"],
        "word/document.xml",
        crc=zlib.crc32(real),
        size=len(real),
    )

    tracemalloc.start()
    try:
        extracted = extract_docx(bytes(package))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert extracted.plain_text == "Hello site.\n\nCell text"
    assert peak < 16 << 20


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_members_python_cannot_inflate_in_bounded_steps_are_refused(method: int) -> None:
    # Python inflates these in one step whatever size the headers declare, so the
    # package is refused before any member is read.
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(make_docx())) as source,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            target.writestr(info.filename, source.read(info))
        target.writestr("word/media/image1.bin", b"\0" * 1024, compress_type=method)

    with pytest.raises(DocxError) as caught:
        extract_docx(output.getvalue())
    assert caught.value.code == "unsafe_docx_package"


def test_a_damaged_compressed_stream_is_an_invalid_docx() -> None:
    rebuilt, offsets = _rezip(make_docx(), replace={})
    package = bytearray(rebuilt)
    offset = offsets["word/document.xml"]
    name_length = int.from_bytes(package[offset + 26 : offset + 28], "little")
    extra = int.from_bytes(package[offset + 28 : offset + 30], "little")
    package[offset + 30 + name_length + extra] ^= 0xFF  # First byte of the deflate data.

    with pytest.raises(DocxError) as caught:
        extract_docx(bytes(package))
    assert caught.value.code == "invalid_docx"


@pytest.mark.parametrize("character", ["\x00", "\x0b", "\x1f", "￾"])
def test_text_a_word_document_cannot_hold_is_refused_before_it_is_committed(
    character: str,
) -> None:
    template = make_docx()
    replacements = [
        DocxReplacement(
            id="p000001", text="Bonjour {{OVEO_LINK_l000001}}site{{/OVEO_LINK_l000001}}."
        ),
        DocxReplacement(id="p000002", text=f"Cellule{character}"),
    ]
    blocks = extract_docx(template).blocks

    with pytest.raises(DocxError) as caught:
        docx_module.plain_text_from_replacements(blocks, replacements)
    assert caught.value.code == "invalid_docx_text"


def test_a_hyperlink_cannot_lose_all_of_its_text() -> None:
    blocks = extract_docx(make_docx()).blocks
    replacements = [
        DocxReplacement(id="p000001", text="Bonjour {{OVEO_LINK_l000001}}{{/OVEO_LINK_l000001}}."),
        DocxReplacement(id="p000002", text="Cellule"),
    ]

    with pytest.raises(DocxError) as caught:
        docx_module.plain_text_from_replacements(blocks, replacements)
    assert caught.value.code == "invalid_docx_hyperlinks"


def test_text_after_the_last_link_stays_before_a_trailing_page_break() -> None:
    template = make_docx(
        document_xml=_body(
            '<w:p><w:r><w:t xml:space="preserve">Details at </w:t></w:r>'
            '<w:hyperlink r:id="rId5"><w:r><w:t>site</w:t></w:r></w:hyperlink>'
            '<w:r><w:br w:type="page"/></w:r></w:p>'
        )
    )
    exported = render_docx(
        template,
        [
            DocxReplacement(
                id="p000001", text="Détails sur {{OVEO_LINK_l000001}}site{{/OVEO_LINK_l000001}}."
            )
        ],
    )

    (paragraph,) = _paragraph_xml(exported)
    assert paragraph.index(b">.</w:t>") < paragraph.index(b'w:type="page"')


@pytest.mark.parametrize(
    ("declared", "codec"),
    [
        # `<` is not the byte 0x3C in UTF-7 or EBCDIC, which hides elements from the
        # byte checks that bound the tree before it is built.
        ("UTF-7", "utf-7"),
        ("IBM037", "cp037"),
        ("ISO-8859-1", "latin-1"),
    ],
)
def test_only_utf8_and_utf16_parts_are_accepted(declared: str, codec: str) -> None:
    source = zipfile.ZipFile(io.BytesIO(make_docx())).read("word/document.xml").decode()
    document = source.replace('encoding="UTF-8"', f'encoding="{declared}"').encode(codec)
    package, _ = _rezip(make_docx(), replace={"word/document.xml": document})

    with pytest.raises(DocxError) as caught:
        extract_docx(package)
    assert caught.value.code == "invalid_docx"


def test_a_utf16_document_is_still_accepted() -> None:
    source = zipfile.ZipFile(io.BytesIO(make_docx())).read("word/document.xml").decode()
    utf16 = source.replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16")
    package, _ = _rezip(make_docx(), replace={"word/document.xml": utf16})

    assert extract_docx(package).plain_text == "Hello site.\n\nCell text"


def test_a_utf16_doctype_is_refused() -> None:
    document = (
        '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE d [<!ENTITY x "y">]>'
        f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>Hi &x; there</w:t></w:r></w:p>'
        "</w:body></w:document>"
    ).encode("utf-16")
    package, _ = _rezip(make_docx(), replace={"word/document.xml": document})

    with pytest.raises(DocxError) as caught:
        extract_docx(package)
    assert caught.value.code == "invalid_docx"


def test_comments_count_toward_the_element_cap() -> None:
    # Varied, so the package stays under the compression-ratio limit.
    comments = "".join(f"<!--{n:x}-->" for n in range(docx_module.MAX_DOCX_XML_ELEMENTS + 1))
    package = make_docx(document_xml=_body(f"<w:p><w:r><w:t>Text</w:t></w:r></w:p>{comments}"))

    with pytest.raises(DocxError) as caught:
        extract_docx(package)
    assert caught.value.code == "docx_too_complex"


def test_nested_bodies_are_refused() -> None:
    nested = _body("<w:body><w:p><w:r><w:t>Inner</w:t></w:r></w:p></w:body>")

    with pytest.raises(DocxError) as caught:
        extract_docx(make_docx(document_xml=nested))
    assert caught.value.code == "invalid_docx"


def test_many_field_instructions_are_checked_in_linear_time() -> None:
    # An XPath union of two large node sets is merged quadratically (seconds here).
    fields = '<w:r><w:instrText>PAGE</w:instrText></w:r><w:fldSimple w:instr="DATE"/>' * 40_000
    package = make_docx(
        document_xml=_body(f"<w:p><w:r><w:t>Text</w:t></w:r></w:p><w:p>{fields}</w:p>")
    )

    started = time.perf_counter()
    assert extract_docx(package).plain_text == "Text"
    assert time.perf_counter() - started < 2


def test_legacy_encoded_non_ascii_part_names_are_refused() -> None:
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(make_docx())) as source,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            target.writestr(info, source.read(info))
        target.writestr("word/media/placeholder.png", b"png")
    # The same length in bytes, now non-ASCII, without the UTF-8 name flag.
    package = output.getvalue().replace(b"placeholder", b"caf\xc3\xa9holder")

    with pytest.raises(DocxError) as caught:
        extract_docx(package)
    assert caught.value.code == "unsafe_docx_package"
