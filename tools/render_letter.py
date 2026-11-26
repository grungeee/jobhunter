#!/usr/bin/env python3
"""Render a letter to .docx with the standard library only.

There is no Word, LibreOffice or python-docx on the hosts this runs on, so the
file is written directly as the minimum OOXML Word accepts: content types,
package relationships, styles and one document part. Word, Pages and Google
Docs all open it; nothing here depends on a renderer being installed.

    python3 -B tools/render_letter.py <letter.md> <out.docx> ["Header line"]
"""

import os
import sys
import zipfile
from xml.sax.saxutils import escape

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""

STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:sz w:val="22"/>
</w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="264" w:lineRule="auto"/></w:pPr></w:pPrDefault>
</w:docDefaults>
</w:styles>
"""

DOC_HEAD = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>
"""

DOC_TAIL = """<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>
<w:pgMar w:top="1417" w:right="1417" w:bottom="1417" w:left="1417"
 w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>
</w:body></w:document>
"""


def paragraphs(markdown):
    """Split a letter into (text, bold) paragraphs, one per markdown block."""
    blocks, current = [], []
    for raw in (markdown or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            if current:
                blocks.append(" ".join(current).strip())
                current = []
            continue
        if line.startswith("# "):
            if current:
                blocks.append(" ".join(current).strip())
                current = []
            blocks.append(("#", line[2:].strip()))
            continue
        current.append(line.strip())
    if current:
        blocks.append(" ".join(current).strip())
    out = []
    for block in blocks:
        if isinstance(block, tuple):
            out.append((block[1], True))
        elif block:
            out.append((block, False))
    return out


def _run(text, bold=False):
    props = '<w:rPr><w:b/><w:sz w:val="24"/></w:rPr>' if bold else '<w:rPr><w:sz w:val="22"/></w:rPr>'
    return ('<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>'
            % (props, escape(text)))


def document_xml(markdown, header=""):
    body = [DOC_HEAD]
    if header:
        body.append("<w:p>%s</w:p>" % _run(header, bold=True))
    for text, bold in paragraphs(markdown):
        body.append("<w:p>%s</w:p>" % _run(text, bold=bold))
    body.append(DOC_TAIL)
    return "".join(body)


def write_docx(path, markdown, header=""):
    """Write the letter as a .docx, atomically, and return the path."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    staging = path + ".tmp"
    with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("word/_rels/document.xml.rels", DOC_RELS)
        archive.writestr("word/styles.xml", STYLES)
        archive.writestr("word/document.xml", document_xml(markdown, header))
    os.replace(staging, path)
    return path


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    source, target = argv[0], argv[1]
    header = argv[2] if len(argv) > 2 else ""
    with open(source, encoding="utf-8") as handle:
        markdown = handle.read()
    write_docx(target, markdown, header)
    print("wrote %s" % target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
