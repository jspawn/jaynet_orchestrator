#!/usr/bin/env python3
"""Extract slide text from a .pptx, one block per slide. Stdlib only.

A .pptx is a ZIP of XML; slide text lives in ppt/slides/slideN.xml as DrawingML
<a:t> runs. Usage: read_pptx.py <file.pptx>
"""

import re
import sys
import xml.etree.ElementTree as ET
import zipfile

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def main(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        slides = sorted(
            (n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
            key=lambda n: int(re.search(r"(\d+)", n).group(1)),
        )
        out = []
        for i, s in enumerate(slides, 1):
            root = ET.fromstring(z.read(s))
            texts = [t.text or "" for t in root.iter(A + "t")]
            out.append(f"# Slide {i}\n" + "\n".join(texts))
        return "\n\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: read_pptx.py <file.pptx>", file=sys.stderr); sys.exit(2)
    try:
        print(main(sys.argv[1]))
    except (zipfile.BadZipFile, KeyError) as e:
        print(f"not a readable .pptx ({type(e).__name__}): {e}", file=sys.stderr); sys.exit(1)
