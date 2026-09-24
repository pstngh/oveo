from __future__ import annotations

import io
import re
import stat
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from lxml import etree  # type: ignore[import-untyped]

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_DOCX_ENTRIES = 2_048
MAX_DOCX_UNCOMPRESSED_BYTES = 50_000_000
MAX_DOCX_XML_PART_BYTES = 12_000_000
MAX_DOCX_BLOCKS = 10_000
# Tree size limits checked before the document tree is built. A small package can
# expand into millions of (empty) elements, which costs seconds of CPU and hundreds of
# MiB; legitimate documents within the word limit stay far below these values.
MAX_DOCX_XML_ELEMENTS = 300_000
MAX_DOCX_PARAGRAPHS = 20_000
MAX_COMPRESSION_RATIO = 500
MIN_DOCX_UNCOMPRESSED_BYTES = 20_000_000

_CONTENT_TYPES = "[Content_Types].xml"
_DOCUMENT_PART = "word/document.xml"
_DOCUMENT_RELS = "word/_rels/document.xml.rels"
_WORD_MAIN_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_XML = "http://www.w3.org/XML/1998/namespace"
_NS = {"w": _W, "r": _R, "pr": _PKG_REL}
_P_TAG = f"{{{_W}}}p"
_R_TAG = f"{{{_W}}}r"
_T_TAG = f"{{{_W}}}t"
_TAB_TAG = f"{{{_W}}}tab"
_BR_TAG = f"{{{_W}}}br"
_CR_TAG = f"{{{_W}}}cr"
_NO_BREAK_HYPHEN_TAG = f"{{{_W}}}noBreakHyphen"
_SOFT_HYPHEN_TAG = f"{{{_W}}}softHyphen"
_BREAK_TYPE = f"{{{_W}}}type"
# Run content that a block's text shows as a character, restored on export. Page and
# column breaks are layout rather than text, so they never appear in block text.
_INLINE_CHARACTERS = {
    _TAB_TAG: "\t",
    _BR_TAG: "\n",
    _CR_TAG: "\n",
    _NO_BREAK_HYPHEN_TAG: "\u2011",
    _SOFT_HYPHEN_TAG: "\u00ad",
}
_CHARACTER_ELEMENTS = {
    "\t": _TAB_TAG,
    "\n": _BR_TAG,
    "\u2011": _NO_BREAK_HYPHEN_TAG,
    "\u00ad": _SOFT_HYPHEN_TAG,
}
_INLINE_TAGS = (_T_TAG, *_INLINE_CHARACTERS)
_HYPERLINK_TAG = f"{{{_W}}}hyperlink"
_PPR_TAG = f"{{{_W}}}pPr"
_ALTERNATE_CONTENT_TAG = f"{{{_MC}}}AlternateContent"
_FIELD_TAGS = frozenset({f"{{{_W}}}fldChar", f"{{{_W}}}instrText", f"{{{_W}}}fldSimple"})
# Paragraphs directly in the body flow (including table cells and content controls),
# never paragraphs nested in another paragraph's text boxes or shapes.
_BODY_PARAGRAPHS = etree.XPath(".//w:body//w:p[not(ancestor::w:p)]", namespaces=_NS)
_IN_TABLE_CELL = etree.XPath("ancestor::w:tc", namespaces=_NS)
_BLOCK_ID = re.compile(r"p[0-9]{6}\Z")
_LINK_ID = re.compile(r"l[0-9]{6}\Z")
_LINK_TOKEN = re.compile(
    r"\{\{OVEO_LINK_(l[0-9]{6})\}\}(.*?)\{\{/OVEO_LINK_\1\}\}",
    flags=re.DOTALL,
)
_RESERVED_LINK_PREFIX = "{{OVEO_LINK_"
_RESERVED_LINK_CLOSE_PREFIX = "{{/OVEO_LINK_"


class DocxError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class DocxBlock:
    id: str
    kind: str
    text: str

    def to_model(self) -> dict[str, str]:
        return {"id": self.id, "kind": self.kind, "text": self.text}


@dataclass(frozen=True, slots=True)
class DocxReplacement:
    id: str
    text: str

    def to_storage(self) -> dict[str, str]:
        return {"id": self.id, "text": self.text}


@dataclass(frozen=True, slots=True)
class ExtractedDocx:
    blocks: tuple[DocxBlock, ...]
    plain_text: str


@dataclass(slots=True)
class _ParsedPackage:
    archive: zipfile.ZipFile
    infos: tuple[zipfile.ZipInfo, ...]
    root: etree._Element
    blocks: tuple[DocxBlock, ...]
    editable: tuple[etree._Element, ...]


def docx_uncompressed_limit(max_upload_bytes: int) -> int:
    """Return one consistent expansion ceiling for a configured upload limit."""

    return min(
        MAX_DOCX_UNCOMPRESSED_BYTES,
        max(MIN_DOCX_UNCOMPRESSED_BYTES, max_upload_bytes * 20),
    )


def _xml_part_limit(max_uncompressed_bytes: int) -> int:
    # 10 MB for the default 2 MB upload limit; smaller uploads get a smaller ceiling.
    return min(MAX_DOCX_XML_PART_BYTES, max_uncompressed_bytes // 4)


def _xml_parser(**options: object) -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        recover=False,
        huge_tree=False,
        remove_blank_text=False,
        **options,
    )


class _TooComplex(Exception):
    pass


class _ElementCounter:
    """lxml parser target that counts elements without building a tree."""

    def __init__(self) -> None:
        self.elements = 0
        self.paragraphs = 0

    def start(self, tag: str, _attrib: object) -> None:
        self.elements += 1
        if tag == _P_TAG:
            self.paragraphs += 1
        if self.elements > MAX_DOCX_XML_ELEMENTS or self.paragraphs > MAX_DOCX_PARAGRAPHS:
            raise _TooComplex

    def end(self, _tag: str) -> None:
        return None

    def data(self, _data: str) -> None:
        return None

    def close(self) -> None:
        return None


def _require_bounded_tree(data: bytes, *, label: str) -> None:
    # Every element has at least one '<', so a byte count below the limit proves the
    # bound without parsing. Otherwise count exactly, stopping at the first excess.
    if data.count(b"<") <= MAX_DOCX_PARAGRAPHS:
        return
    try:
        etree.fromstring(data, parser=_xml_parser(target=_ElementCounter()))
    except _TooComplex as exc:
        raise DocxError(
            "docx_too_complex", "The DOCX contains too many elements to process safely."
        ) from exc
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise DocxError("invalid_docx", f"The DOCX {label} part is malformed.") from exc


def _xml(data: bytes, *, label: str, max_bytes: int = MAX_DOCX_XML_PART_BYTES) -> etree._Element:
    if len(data) > max_bytes or b"<!DOCTYPE" in data.upper():
        raise DocxError("invalid_docx", f"The DOCX {label} part is not supported.")
    _require_bounded_tree(data, label=label)
    try:
        return etree.fromstring(data, parser=_xml_parser())
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise DocxError("invalid_docx", f"The DOCX {label} part is malformed.") from exc


def _validated_infos(
    archive: zipfile.ZipFile,
    *,
    max_uncompressed_bytes: int,
) -> tuple[zipfile.ZipInfo, ...]:
    infos = tuple(archive.infolist())
    if not infos or len(infos) > MAX_DOCX_ENTRIES:
        raise DocxError("unsafe_docx_package", "The DOCX package has too many entries.")
    seen: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        path = PurePosixPath(name)
        if not name or name in seen or path.is_absolute() or ".." in path.parts or "\\" in name:
            raise DocxError("unsafe_docx_package", "The DOCX package contains unsafe paths.")
        seen.add(name)
        if info.flag_bits & 0x1:
            raise DocxError("encrypted_docx", "Encrypted DOCX files are not supported.")
        file_mode = (info.external_attr >> 16) & 0xFFFF
        if file_mode and stat.S_ISLNK(file_mode):
            raise DocxError("unsafe_docx_package", "DOCX package links are not supported.")
        total += info.file_size
        if total > max_uncompressed_bytes:
            raise DocxError(
                "docx_expansion_too_large",
                "The DOCX package expands beyond the safety limit.",
            )
        if (
            info.file_size > 0
            and info.compress_size > 0
            and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
        ):
            raise DocxError(
                "unsafe_docx_package",
                "The DOCX package has an unsafe compression ratio.",
            )
    return infos


def _open_package(
    content: bytes,
    *,
    max_uncompressed_bytes: int,
) -> tuple[zipfile.ZipFile, tuple[zipfile.ZipInfo, ...]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content), mode="r")
        infos = _validated_infos(archive, max_uncompressed_bytes=max_uncompressed_bytes)
        bad = archive.testzip()
    except DocxError:
        archive.close()
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as exc:
        raise DocxError("invalid_docx", "The file is not a valid DOCX package.") from exc
    if bad is not None:
        archive.close()
        raise DocxError("invalid_docx", "The DOCX package is damaged.")
    return archive, infos


def _validate_content_types(archive: zipfile.ZipFile, names: set[str]) -> None:
    if _CONTENT_TYPES not in names or _DOCUMENT_PART not in names:
        raise DocxError("invalid_docx", "The DOCX package is missing required document parts.")
    root = _xml(archive.read(_CONTENT_TYPES), label="content-types")
    content_types = {
        str(node.get("ContentType", ""))
        for node in root.xpath("//*[local-name()='Default' or local-name()='Override']")
    }
    lowered = {value.casefold() for value in content_types}
    if any("macroenabled" in value for value in lowered) or any(
        name.casefold().endswith("vbaproject.bin") for name in names
    ):
        raise DocxError("macro_enabled_docx", "Macro-enabled Word files are not supported.")
    main_types = {
        str(node.get("ContentType", ""))
        for node in root.xpath("//*[local-name()='Override'][@PartName='/word/document.xml']")
    }
    if main_types != {_WORD_MAIN_CONTENT_TYPE}:
        raise DocxError("invalid_docx", "The package is not a standard DOCX document.")


def _hyperlink_relationships(archive: zipfile.ZipFile, names: set[str]) -> set[str]:
    if _DOCUMENT_RELS not in names:
        return set()
    root = _xml(archive.read(_DOCUMENT_RELS), label="relationships")
    ids: set[str] = set()
    for relationship in root.xpath("/pr:Relationships/pr:Relationship", namespaces=_NS):
        rel_id = relationship.get("Id")
        rel_type = relationship.get("Type", "")
        target_mode = relationship.get("TargetMode")
        if rel_type.endswith("/hyperlink"):
            if not rel_id or target_mode != "External":
                raise DocxError(
                    "unsupported_docx_hyperlink",
                    "The DOCX contains an unsupported hyperlink relationship.",
                )
            ids.add(rel_id)
    return ids


def _reject_unsupported_markup(root: etree._Element) -> None:
    if root.xpath(".//w:ins | .//w:del | .//w:moveFrom | .//w:moveTo", namespaces=_NS):
        raise DocxError(
            "tracked_changes_not_supported",
            "Accept or reject tracked changes before uploading the DOCX.",
        )
    instructions = [
        str(value)
        for value in root.xpath(".//w:instrText/text() | .//w:fldSimple/@w:instr", namespaces=_NS)
    ]
    if any(re.search(r"\bHYPERLINK\b", value, flags=re.IGNORECASE) for value in instructions):
        raise DocxError(
            "complex_hyperlink_not_supported",
            "The DOCX contains a field-code hyperlink that cannot be edited safely.",
        )


def _belongs_to(node: etree._Element, paragraph: etree._Element) -> bool:
    """True when ``paragraph`` is the nearest paragraph enclosing ``node``.

    Text boxes and shapes anchored in a paragraph contain their own paragraphs; their
    text is not part of the anchoring paragraph and must never be rewritten through it.
    """

    return next(node.iterancestors(_P_TAG), None) is paragraph


def _own_text_nodes(node: etree._Element, paragraph: etree._Element) -> list[etree._Element]:
    candidates = [node] if node.tag == _T_TAG else list(node.iter(_T_TAG))
    return [text for text in candidates if _belongs_to(text, paragraph)]


def _is_layout_break(element: etree._Element) -> bool:
    return element.tag == _BR_TAG and element.get(_BREAK_TYPE) in {"page", "column"}


def _is_inline_item(element: etree._Element, paragraph: etree._Element) -> bool:
    """True for the paragraph's own text: runs' text, tabs, line breaks, and hyphens."""

    if element.tag != _T_TAG:
        parent = element.getparent()
        # Tab stops live in paragraph properties, not in runs.
        if parent is None or parent.tag != _R_TAG or _is_layout_break(element):
            return False
    return _belongs_to(element, paragraph)


def _own_inline_items(node: etree._Element, paragraph: etree._Element) -> list[etree._Element]:
    candidates = [node] if node.tag in _INLINE_TAGS else list(node.iter(*_INLINE_TAGS))
    return [item for item in candidates if _is_inline_item(item, paragraph)]


def _item_text(item: etree._Element) -> str:
    if item.tag == _T_TAG:
        return str(item.text or "")
    return _INLINE_CHARACTERS[item.tag]


def _text(node: etree._Element, paragraph: etree._Element) -> str:
    return "".join(_item_text(item) for item in _own_inline_items(node, paragraph))


def _has_interior_layout_break(paragraph: etree._Element) -> bool:
    """True when a page or column break sits between pieces of the paragraph's text."""

    text_seen = False
    break_after_text = False
    for element in paragraph.iter(*_INLINE_TAGS):
        if _is_layout_break(element) and _belongs_to(element, paragraph):
            break_after_text = break_after_text or text_seen
        elif _is_inline_item(element, paragraph) and _item_text(element):
            if break_after_text:
                return True
            text_seen = True
    return False


def _has_own_alternate_content(paragraph: etree._Element) -> bool:
    for text in _own_text_nodes(paragraph, paragraph):
        for ancestor in text.iterancestors():
            if ancestor is paragraph:
                break
            if ancestor.tag == _ALTERNATE_CONTENT_TAG:
                return True
    return False


def _reject_reserved_link_text(value: str) -> None:
    if _RESERVED_LINK_PREFIX in value or _RESERVED_LINK_CLOSE_PREFIX in value:
        raise DocxError(
            "reserved_docx_text",
            "The DOCX contains text reserved for safe hyperlink handling.",
        )


def _paragraph_block(
    paragraph: etree._Element,
    *,
    block_number: int,
    link_number: int,
    hyperlink_relationships: set[str],
) -> tuple[DocxBlock | None, int]:
    if any(_belongs_to(element, paragraph) for element in paragraph.iter(*_FIELD_TAGS)):
        return None, link_number
    if _has_own_alternate_content(paragraph):
        # Alternative renderings of the same text (for example a symbol with a legacy
        # fallback) cannot be rewritten consistently, so the paragraph stays untouched.
        return None, link_number
    if _has_interior_layout_break(paragraph):
        # Rewritten text is written where the paragraph's text starts, which would move
        # a page or column break that sits between pieces of it.
        return None, link_number
    direct_hyperlinks = [child for child in paragraph if child.tag == _HYPERLINK_TAG]
    own_hyperlinks = [
        link for link in paragraph.iter(_HYPERLINK_TAG) if _belongs_to(link, paragraph)
    ]
    if len(direct_hyperlinks) != len(own_hyperlinks):
        raise DocxError(
            "unsupported_docx_hyperlink",
            "The DOCX contains a nested hyperlink that cannot be edited safely.",
        )
    parts: list[str] = []
    for child in paragraph:
        if child.tag == _PPR_TAG:
            continue
        if child.tag != _HYPERLINK_TAG:
            value = _text(child, paragraph)
            _reject_reserved_link_text(value)
            parts.append(value)
            continue
        display = _text(child, paragraph)
        if not display:
            # Image-only links and other non-text hyperlinks remain untouched. Omitting
            # the paragraph prevents a rewrite from moving text around the protected link.
            return None, link_number
        _reject_reserved_link_text(display)
        rel_id = child.get(f"{{{_R}}}id")
        anchor = child.get(f"{{{_W}}}anchor")
        if rel_id is None and anchor is None:
            raise DocxError(
                "unsupported_docx_hyperlink",
                "The DOCX contains an unsupported hyperlink.",
            )
        if rel_id is not None and rel_id not in hyperlink_relationships:
            raise DocxError(
                "unsupported_docx_hyperlink",
                "The DOCX contains a broken hyperlink relationship.",
            )
        link_number += 1
        link_id = f"l{link_number:06d}"
        parts.append(f"{{{{OVEO_LINK_{link_id}}}}}{display}{{{{/OVEO_LINK_{link_id}}}}}")
    text = "".join(parts)
    if not any(node.text for node in _own_text_nodes(paragraph, paragraph)):
        # Tabs or line breaks without any text are layout, not content to edit.
        return None, link_number
    kind = "table_cell" if _IN_TABLE_CELL(paragraph) else "paragraph"
    return DocxBlock(id=f"p{block_number:06d}", kind=kind, text=text), link_number


def _parse_package(content: bytes, *, max_uncompressed_bytes: int) -> _ParsedPackage:
    archive, infos = _open_package(
        content,
        max_uncompressed_bytes=max_uncompressed_bytes,
    )
    try:
        names = {info.filename for info in infos}
        _validate_content_types(archive, names)
        hyperlink_relationships = _hyperlink_relationships(archive, names)
        root = _xml(
            archive.read(_DOCUMENT_PART),
            label="document",
            max_bytes=_xml_part_limit(max_uncompressed_bytes),
        )
        _reject_unsupported_markup(root)
        blocks: list[DocxBlock] = []
        editable: list[etree._Element] = []
        link_number = 0
        for paragraph in _BODY_PARAGRAPHS(root):
            block, link_number = _paragraph_block(
                paragraph,
                block_number=len(blocks) + 1,
                link_number=link_number,
                hyperlink_relationships=hyperlink_relationships,
            )
            if block is None:
                continue
            blocks.append(block)
            editable.append(paragraph)
            if len(blocks) > MAX_DOCX_BLOCKS:
                raise DocxError(
                    "docx_too_complex",
                    "The DOCX contains too many editable text blocks.",
                )
        if not blocks:
            raise DocxError("empty_docx", "The DOCX has no editable paragraph text.")
        return _ParsedPackage(
            archive=archive,
            infos=infos,
            root=root,
            blocks=tuple(blocks),
            editable=tuple(editable),
        )
    except BaseException:
        archive.close()
        raise


def extract_docx(
    content: bytes,
    *,
    max_uncompressed_bytes: int = MAX_DOCX_UNCOMPRESSED_BYTES,
) -> ExtractedDocx:
    parsed = _parse_package(content, max_uncompressed_bytes=max_uncompressed_bytes)
    try:
        replacements = tuple(
            DocxReplacement(id=block.id, text=block.text) for block in parsed.blocks
        )
        return ExtractedDocx(
            blocks=parsed.blocks,
            plain_text=_plain_text_from_validated(replacements),
        )
    finally:
        parsed.archive.close()


def _link_parts(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    plain_parts: list[str] = []
    links: list[tuple[str, str]] = []
    position = 0
    for match in _LINK_TOKEN.finditer(text):
        plain = text[position : match.start()]
        if _RESERVED_LINK_PREFIX in plain or _RESERVED_LINK_CLOSE_PREFIX in plain:
            raise DocxError("invalid_docx_blocks", "A DOCX hyperlink token is malformed.")
        display = match.group(2)
        if _RESERVED_LINK_PREFIX in display or _RESERVED_LINK_CLOSE_PREFIX in display:
            raise DocxError("invalid_docx_blocks", "A DOCX hyperlink token is malformed.")
        plain_parts.append(plain)
        links.append((match.group(1), display))
        position = match.end()
    tail = text[position:]
    if _RESERVED_LINK_PREFIX in tail or _RESERVED_LINK_CLOSE_PREFIX in tail:
        raise DocxError("invalid_docx_blocks", "A DOCX hyperlink token is malformed.")
    plain_parts.append(tail)
    return plain_parts, links


def validate_replacements(
    template_blocks: Sequence[DocxBlock],
    replacements: Sequence[DocxReplacement | Mapping[str, object]],
) -> tuple[DocxReplacement, ...]:
    if len(replacements) != len(template_blocks):
        raise DocxError("invalid_docx_blocks", "Every DOCX block must be returned exactly once.")
    validated: list[DocxReplacement] = []
    for expected, candidate in zip(template_blocks, replacements, strict=True):
        if isinstance(candidate, DocxReplacement):
            replacement = candidate
        else:
            if set(candidate) != {"id", "text"}:
                raise DocxError("invalid_docx_blocks", "A DOCX block replacement is invalid.")
            block_id = candidate.get("id")
            text = candidate.get("text")
            if not isinstance(block_id, str) or not isinstance(text, str):
                raise DocxError("invalid_docx_blocks", "A DOCX block replacement is invalid.")
            replacement = DocxReplacement(id=block_id, text=text)
        if _BLOCK_ID.fullmatch(replacement.id) is None or replacement.id != expected.id:
            raise DocxError(
                "invalid_docx_blocks",
                "DOCX block identifiers are missing or reordered.",
            )
        _, expected_links = _link_parts(expected.text)
        _, actual_links = _link_parts(replacement.text)
        expected_ids = [link_id for link_id, _ in expected_links]
        actual_ids = [link_id for link_id, _ in actual_links]
        if (
            any(_LINK_ID.fullmatch(link_id) is None for link_id in actual_ids)
            or actual_ids != expected_ids
            or len(set(actual_ids)) != len(actual_ids)
        ):
            raise DocxError(
                "invalid_docx_hyperlinks",
                "Every protected DOCX hyperlink must appear exactly once and in order.",
            )
        validated.append(replacement)
    return tuple(validated)


def docx_blocks_from_storage(value: object) -> tuple[DocxBlock, ...]:
    """Validate and restore the immutable block map stored with an attachment."""

    if not isinstance(value, list) or not value or len(value) > MAX_DOCX_BLOCKS:
        raise DocxError("invalid_docx_blocks", "The stored DOCX block map is invalid.")
    blocks: list[DocxBlock] = []
    next_link_number = 1
    for block_number, candidate in enumerate(value, start=1):
        if not isinstance(candidate, dict) or set(candidate) != {"id", "kind", "text"}:
            raise DocxError("invalid_docx_blocks", "The stored DOCX block map is invalid.")
        block_id = candidate.get("id")
        kind = candidate.get("kind")
        text = candidate.get("text")
        if (
            not isinstance(block_id, str)
            or block_id != f"p{block_number:06d}"
            or not isinstance(kind, str)
            or kind not in {"paragraph", "table_cell"}
            or not isinstance(text, str)
            or not text
        ):
            raise DocxError("invalid_docx_blocks", "The stored DOCX block map is invalid.")
        _, links = _link_parts(text)
        for link_id, _display in links:
            if link_id != f"l{next_link_number:06d}":
                raise DocxError("invalid_docx_blocks", "The stored DOCX block map is invalid.")
            next_link_number += 1
        blocks.append(DocxBlock(id=block_id, kind=kind, text=text))
    return tuple(blocks)


def plain_text_from_replacements(
    template_blocks: Sequence[DocxBlock],
    replacements: Sequence[DocxReplacement | Mapping[str, object]],
) -> str:
    validated = validate_replacements(template_blocks, replacements)
    return _plain_text_from_validated(validated)


def _plain_text_from_validated(replacements: Sequence[DocxReplacement]) -> str:
    paragraphs: list[str] = []
    for replacement in replacements:
        plain_parts, links = _link_parts(replacement.text)
        value = plain_parts[0]
        for index, (_, display) in enumerate(links, start=1):
            value += display + plain_parts[index]
        paragraphs.append(value)
    return "\n\n".join(paragraphs)


def _set_text(node: etree._Element, value: str) -> None:
    node.text = value
    xml_space = f"{{{_XML}}}space"
    if value[:1].isspace() or value[-1:].isspace():
        node.set(xml_space, "preserve")
    else:
        node.attrib.pop(xml_space, None)


def _inline_elements(value: str) -> list[etree._Element]:
    """Run content for ``value``: text nodes, with tabs, line breaks and hyphens restored."""

    elements: list[etree._Element] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            text = etree.Element(_T_TAG)
            _set_text(text, "".join(pending))
            elements.append(text)
            pending.clear()

    for character in value:
        tag = _CHARACTER_ELEMENTS.get(character)
        if tag is None:
            pending.append(character)
        else:
            flush()
            elements.append(etree.Element(tag))
    flush()
    return elements


def _rewrite_items(items: Sequence[etree._Element], value: str) -> None:
    """Replace a stretch of paragraph text where it starts; unchanged text is left alone.

    Leaving unchanged text untouched keeps its runs, so its mixed formatting survives.
    Changed text takes the formatting of the run where the stretch starts.
    """

    if "".join(_item_text(item) for item in items) == value:
        return
    first = items[0]
    run = first.getparent()
    position = run.index(first)
    for item in items:
        item.getparent().remove(item)
    for offset, element in enumerate(_inline_elements(value)):
        run.insert(position + offset, element)


def _insert_plain_run(
    paragraph: etree._Element,
    value: str,
    *,
    before: etree._Element | None,
) -> None:
    if not value:
        return
    run = etree.Element(_R_TAG)
    run.extend(_inline_elements(value))
    if before is None:
        paragraph.append(run)
    else:
        paragraph.insert(paragraph.index(before), run)


def _patch_paragraph(paragraph: etree._Element, replacement: DocxReplacement) -> None:
    plain_parts, replacement_links = _link_parts(replacement.text)
    hyperlinks = [child for child in paragraph if child.tag == _HYPERLINK_TAG]
    if len(hyperlinks) != len(replacement_links):
        raise DocxError("invalid_docx_blocks", "The DOCX template no longer matches its blocks.")
    segments: list[list[etree._Element]] = [[]]
    for child in paragraph:
        if child.tag == _PPR_TAG:
            continue
        if child.tag == _HYPERLINK_TAG:
            segments.append([])
        else:
            # Only the paragraph's own text: text boxes anchored here keep theirs.
            segments[-1].extend(_own_inline_items(child, paragraph))
    if len(segments) != len(plain_parts):
        raise DocxError("invalid_docx_blocks", "The DOCX template no longer matches its blocks.")
    for index, (items, value) in enumerate(zip(segments, plain_parts, strict=True)):
        if items:
            _rewrite_items(items, value)
        else:
            before = hyperlinks[index] if index < len(hyperlinks) else None
            _insert_plain_run(paragraph, value, before=before)
    for hyperlink, (_, display) in zip(hyperlinks, replacement_links, strict=True):
        items = _own_inline_items(hyperlink, paragraph)
        if not items:
            raise DocxError("invalid_docx_blocks", "The DOCX hyperlink has no editable text.")
        _rewrite_items(items, display)


_OUTDATED_TEMPLATE_MESSAGE = (
    "This Word document was imported by an earlier Oveo version that could not edit its "
    "layout safely. Upload the document again to continue editing it."
)


def require_matching_blocks(blocks: Sequence[DocxBlock], stored: object) -> None:
    """Fail closed when a template no longer yields the block map stored at upload.

    Earlier extraction read text-box paragraphs as extra blocks. Applying such a map to
    today's parse would move text between paragraphs, so the document must be uploaded
    again instead.
    """

    try:
        matches = docx_blocks_from_storage(stored) == tuple(blocks)
    except DocxError:
        matches = False
    if not matches:
        raise DocxError("docx_template_outdated", _OUTDATED_TEMPLATE_MESSAGE)


def render_docx(
    template: bytes,
    replacements: Sequence[DocxReplacement | Mapping[str, object]],
    *,
    max_uncompressed_bytes: int = MAX_DOCX_UNCOMPRESSED_BYTES,
    stored_blocks: object | None = None,
) -> bytes:
    parsed = _parse_package(template, max_uncompressed_bytes=max_uncompressed_bytes)
    try:
        if stored_blocks is not None:
            require_matching_blocks(parsed.blocks, stored_blocks)
        validated = validate_replacements(parsed.blocks, replacements)
        for paragraph, replacement in zip(parsed.editable, validated, strict=True):
            _patch_paragraph(paragraph, replacement)
        patched_document = etree.tostring(
            parsed.root,
            encoding="UTF-8",
            xml_declaration=True,
            standalone=True,
        )
        output = io.BytesIO()
        with zipfile.ZipFile(output, mode="w") as destination:
            for info in parsed.infos:
                data = (
                    patched_document
                    if info.filename == _DOCUMENT_PART
                    else parsed.archive.read(info)
                )
                destination.writestr(info, data)
        return output.getvalue()
    finally:
        parsed.archive.close()


__all__ = [
    "DOCX_MEDIA_TYPE",
    "DocxBlock",
    "DocxError",
    "DocxReplacement",
    "ExtractedDocx",
    "docx_blocks_from_storage",
    "docx_uncompressed_limit",
    "extract_docx",
    "plain_text_from_replacements",
    "render_docx",
    "require_matching_blocks",
    "validate_replacements",
]
