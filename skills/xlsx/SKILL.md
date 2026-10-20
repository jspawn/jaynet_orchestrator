---
name: xlsx
description: Read data from, or CREATE, Excel .xlsx spreadsheets. Load when an .xlsx is uploaded/referenced and you need its data, or when the user asks to create/write/generate a spreadsheet.
---
# Reading Excel (.xlsx) files

An `.xlsx` is a ZIP of XML; cell text lives in `xl/sharedStrings.xml` and each
sheet in `xl/worksheets/sheetN.xml`. The bundled `read_xlsx.py` is standard-library
only, so you can run it either way.

## Run it

Via **job.start** (full env, always works):

    job.start(name="read-xlsx",
              command="python <files['read_xlsx.py']> <path-to-file.xlsx>")

It prints each sheet as a `# xl/worksheets/sheetN.xml` header followed by
tab-separated rows. Read it back with `job.logs`.

If `tools.code.allowed_imports` includes `zipfile`, `xml` and `zlib`, you can run
the same logic inline with `code.run` (language=python, no job needed). By default those aren't
allowed, so prefer the job.

## Notes

- This reads **stored values**, not formulas — a cell showing `=SUM(...)` returns
  its last-computed value, and an unopened/never-calculated file may store none.
- Number formatting (dates shown as serial numbers, currency, %) is *display*
  formatting; you get the raw stored value. Interpret dates/serials with care and
  say so if it matters.
- For multiple sheets, formulas, merged cells, or writing changes back, use
  `openpyxl` in a venv (via job.start) instead — offer that if the task needs it.

## Cell background/fill colors

The stdlib reader and the notes above cover **values** — not formatting. When a
task asks for a cell's background/fill color, use `openpyxl` via job.start
(the devbox ships it; same pattern as Creating below):

    from openpyxl import load_workbook
    ws = load_workbook(path).active          # or wb["SheetName"]
    rgb = ws.cell(row=2, column=3).fill.fgColor.rgb   # '00F478A7'-style ARGB, or None

`fgColor.rgb` returns ARGB with a leading alpha byte (usually `00` or `FF`) —
strip the first two chars for the hex color; theme/indexed fills carry
`fill.fgColor.theme` / `.indexed` instead. Fallback without openpyxl: parse
`xl/styles.xml` — `cellXfs` index → `fillId` → `<fill><patternFill><fgColor rgb=…/>`.

## Creating an .xlsx

This skill bundles `write_xlsx.py`, which builds a spreadsheet from **CSV** (one
sheet) or **JSON** (one or more sheets). It needs `openpyxl` (pip, no system deps).
The first row of each sheet becomes a bold, frozen header.

JSON input forms: `{"Sheet1": [["h1","h2"],[1,2]], "Sheet2": [...]}` or
`[{"name":"Sheet1","rows":[[...]]}]`.

1. `fs.write` your data into the workspace — `data.json` (or `data.csv`).
2. Generate with **code.check**, output into the workspace:

       code.check(command="python3 <files['write_xlsx.py']> data.json report.xlsx")

   **Never write your own converter** — the bundled script is tested; run it,
   don't reimplement it. The devbox toolchain container ships openpyxl (on older installs rebuild
   the image: scripts/devbox-build.sh), so bare `python3` works there — no
   venv, no network. Only if the script answers that openpyxl is missing,
   make a venv once (needs network): `python3 -m venv /tmp/docenv;
   /tmp/docenv/bin/pip install openpyxl`, then run with `/tmp/docenv/bin/python`
   (code.run with network=true or job.start).
3. **`deliver.files(["report.xlsx"])`**.

For formulas, multiple formatted sheets, charts, or styling, write a short
`openpyxl` script (`fs.write` a `.py`, run it the same way) — `write_xlsx.py`
shows the basic pattern (headers, column widths, freeze panes) to extend.
