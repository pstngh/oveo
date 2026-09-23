# ruff: noqa: E501 - OOXML fixture attributes are intentionally kept on one line.

from __future__ import annotations

import base64
import io
import zipfile

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def make_docx(
    *,
    document_xml: str | None = None,
    main_content_type: str = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    ),
    extra_parts: dict[str, bytes] | None = None,
) -> bytes:
    if document_xml is None:
        document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}" xmlns:r="{R}">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">Hello </w:t></w:r>
      <w:hyperlink r:id="rId5" w:history="1"><w:r><w:rPr><w:u w:val="single"/></w:rPr><w:t>site</w:t></w:r></w:hyperlink>
      <w:r><w:t>.</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="5000" w:type="dxa"/></w:tblPr>
      <w:tblGrid><w:gridCol w:w="5000"/></w:tblGrid>
      <w:tr><w:tc><w:tcPr><w:tcW w:w="5000" w:type="dxa"/></w:tcPr><w:p><w:r><w:t>Cell text</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:sectPr>
      <w:headerReference w:type="default" r:id="rId1"/>
      <w:footerReference w:type="default" r:id="rId2"/>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>
    </w:sectPr>
  </w:body>
</w:document>"""

    content_types = f"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml" ContentType="{main_content_type}"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
  <Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
</Types>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document_rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
  <Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.com/original" TargetMode="External"/>
</Relationships>"""
    styles = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="{W}">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/></w:style>
  <w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/></w:style>
</w:styles>"""
    header = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:hdr xmlns:w="{W}"><w:p><w:r><w:t>Preserved header</w:t></w:r></w:p></w:hdr>"""
    footer = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:ftr xmlns:w="{W}"><w:p><w:r><w:t>Preserved footer</w:t></w:r></w:p></w:ftr>"""
    parts = {
        "[Content_Types].xml": content_types.encode(),
        "_rels/.rels": root_rels.encode(),
        "word/document.xml": document_xml.encode(),
        "word/_rels/document.xml.rels": document_rels.encode(),
        "word/styles.xml": styles.encode(),
        "word/header1.xml": header.encode(),
        "word/footer1.xml": footer.encode(),
        "word/media/image1.png": base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ),
    }
    parts.update(extra_parts or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return output.getvalue()


TEXTBOX_NAMESPACES = (
    f'xmlns:w="{W}" xmlns:r="{R}" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:v="urn:schemas-microsoft-com:vml" mc:Ignorable="wps"'
)


def textbox_run(inner: str) -> str:
    # Word's standard floating text box: DrawingML choice plus a VML fallback, each
    # holding its own paragraphs inside w:txbxContent.
    return (
        '<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wp:anchor>'
        '<a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/'
        f'wordprocessingShape"><wps:wsp><wps:txbx><w:txbxContent>{inner}</w:txbxContent>'
        "</wps:txbx></wps:wsp></a:graphicData></a:graphic></wp:anchor></w:drawing>"
        "</mc:Choice><mc:Fallback><w:pict><v:shape><v:textbox>"
        f"<w:txbxContent>{inner}</w:txbxContent></v:textbox></v:shape></w:pict>"
        "</mc:Fallback></mc:AlternateContent></w:r>"
    )


def textbox_document(*, anchor_first: bool, box: str | None = None) -> str:
    inner = box or "<w:p><w:r><w:t>Callout text</w:t></w:r></w:p>"
    body = "<w:r><w:t>Quarterly results improved.</w:t></w:r>"
    paragraph = textbox_run(inner) + body if anchor_first else body + textbox_run(inner)
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document '
        f"{TEXTBOX_NAMESPACES}><w:body>"
        "<w:p><w:r><w:t>Intro paragraph.</w:t></w:r></w:p>"
        f"<w:p>{paragraph}</w:p><w:sectPr/></w:body></w:document>"
    )
