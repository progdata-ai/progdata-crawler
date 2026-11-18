"""Small DOCX and XLSX files built from scratch for the convert tests.

Both formats are zip archives of XML parts. These hold just enough for a reader to
open them: one document with a heading, a paragraph and a list-like line, and one
workbook with a sheet of three rows.
"""

import io
import zipfile

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


_OOXML = "application/vnd.openxmlformats-officedocument"


def _content_types(overrides: dict[str, str]) -> str:
    rels = "application/vnd.openxmlformats-package.relationships+xml"
    parts = "".join(
        f'<Override PartName="{name}" ContentType="{_OOXML}.{kind}"/>'
        for name, kind in overrides.items()
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="{_CT}">'
        f'<Default Extension="rels" ContentType="{rels}"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{parts}</Types>"
    )


def _zip(parts: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def build_docx() -> bytes:
    body = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Baking notes</w:t></w:r></w:p>'
        '<w:p><w:r><w:t xml:space="preserve">Keep the starter </w:t></w:r>'
        "<w:r><w:rPr><w:b/></w:rPr><w:t>warm</w:t></w:r>"
        '<w:r><w:t xml:space="preserve"> and feed it daily.</w:t></w:r></w:p>'
        "<w:p><w:r><w:t>Rye ferments faster than wheat.</w:t></w:r></w:p>"
    )
    return _zip(
        {
            "[Content_Types].xml": _content_types(
                {
                    "/word/document.xml": "wordprocessingml.document.main+xml",
                    "/word/styles.xml": "wordprocessingml.styles+xml",
                }
            ),
            "_rels/.rels": (
                f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PKG}">'
                f'<Relationship Id="rId1" Type="{_R}/officeDocument" Target="word/document.xml"/>'
                "</Relationships>"
            ),
            "word/_rels/document.xml.rels": (
                f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PKG}">'
                f'<Relationship Id="rId1" Type="{_R}/styles" Target="styles.xml"/>'
                "</Relationships>"
            ),
            "word/styles.xml": (
                f'<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="{_W}">'
                '<w:style w:type="paragraph" w:styleId="Heading1">'
                '<w:name w:val="heading 1"/></w:style>'
                "</w:styles>"
            ),
            "word/document.xml": (
                f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{_W}">'
                f"<w:body>{body}</w:body></w:document>"
            ),
        }
    )


def build_xlsx() -> bytes:
    rows = [("Flour", "Grams"), ("Rye", "300"), ("Wheat", "700")]

    def cell(ref: str, value: str) -> str:
        if value.isdigit():
            return f'<c r="{ref}"><v>{value}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'

    sheet_rows = "".join(
        f'<row r="{n}">{cell(f"A{n}", a)}{cell(f"B{n}", b)}</row>'
        for n, (a, b) in enumerate(rows, start=1)
    )
    return _zip(
        {
            "[Content_Types].xml": _content_types(
                {
                    "/xl/workbook.xml": "spreadsheetml.sheet.main+xml",
                    "/xl/worksheets/sheet1.xml": "spreadsheetml.worksheet+xml",
                }
            ),
            "_rels/.rels": (
                f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PKG}">'
                f'<Relationship Id="rId1" Type="{_R}/officeDocument" Target="xl/workbook.xml"/>'
                "</Relationships>"
            ),
            "xl/_rels/workbook.xml.rels": (
                f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PKG}">'
                f'<Relationship Id="rId1" Type="{_R}/worksheet" Target="worksheets/sheet1.xml"/>'
                "</Relationships>"
            ),
            "xl/workbook.xml": (
                f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{_S}" xmlns:r="{_R}">'
                '<sheets><sheet name="Recipe" sheetId="1" r:id="rId1"/></sheets></workbook>'
            ),
            "xl/worksheets/sheet1.xml": (
                f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{_S}">'
                f"<sheetData>{sheet_rows}</sheetData></worksheet>"
            ),
        }
    )
