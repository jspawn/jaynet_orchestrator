#!/usr/bin/env python3
"""Extract visible paragraph text from a .docx, standard library only.

A .docx is a ZIP whose main body text lives in word/document.xml as <w:t> runs
inside <w:p> paragraphs. Usage: read_docx.py <file.docx>  ->  text on stdout.
"""

import sys
import xml.etree.ElementTree as ET
import zipfile

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    paragraphs = []
    for p in root.iter(W + "p"):
        text = "".join(node.text or "" for node in p.iter(W + "t"))
        paragraphs.append(text)
    return "\n".join(paragraphs)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: read_docx.py <file.docx>", file=sys.stderr)
        sys.exit(2)
    try:
        sys.stdout.write(extract(sys.argv[1]) + "\n")
    except (zipfile.BadZipFile, KeyError) as e:
        print(f"not a readable .docx ({type(e).__name__}): {e}", file=sys.stderr)
        sys.exit(1)
