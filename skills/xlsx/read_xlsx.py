#!/usr/bin/env python3
"""Extract cell values from an .xlsx as TSV, one block per sheet. Stdlib only.

An .xlsx is a ZIP of XML: shared text lives in xl/sharedStrings.xml, each sheet in
xl/worksheets/sheetN.xml. Usage: read_xlsx.py <file.xlsx>
"""

import re
import sys
import zipfile
import xml.etree.ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _col_index(ref: str) -> int:
    m = re.match(r"([A-Z]+)", ref or "")
    if not m:
        return 0
    idx = 0
    for ch in m.group(1):
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(t.text or "" for t in si.iter(NS + "t")) for si in root.iter(NS + "si")]


def _sheet_rows(z: zipfile.ZipFile, name: str, shared: list[str]) -> list[list[str]]:
    root = ET.fromstring(z.read(name))
    rows = []
    for row in root.iter(NS + "row"):
        cells, maxc = {}, -1
        for c in row.findall(NS + "c"):
            ci = _col_index(c.get("r", "")); maxc = max(maxc, ci)
            t, v, inl = c.get("t"), c.find(NS + "v"), c.find(NS + "is")
            if t == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(shared):
                val = shared[int(v.text)]
            elif t == "inlineStr" and inl is not None:
                val = "".join(x.text or "" for x in inl.iter(NS + "t"))
            elif v is not None:
                val = v.text or ""
            else:
                val = ""
            cells[ci] = val
        rows.append([cells.get(i, "") for i in range(maxc + 1)] if maxc >= 0 else [])
    return rows


def main(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        shared = _shared_strings(z)
        sheets = sorted(n for n in z.namelist()
                        if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        out = []
        for s in sheets:
            out.append(f"# {s}")
            for r in _sheet_rows(z, s, shared):
                out.append("\t".join(r))
        return "\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: read_xlsx.py <file.xlsx>", file=sys.stderr); sys.exit(2)
    try:
        print(main(sys.argv[1]))
    except (zipfile.BadZipFile, KeyError) as e:
        print(f"not a readable .xlsx ({type(e).__name__}): {e}", file=sys.stderr); sys.exit(1)
