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

## Creating an .xlsx

This skill bundles `write_xlsx.py`, which builds a spreadsheet from **CSV** (one
sheet) or **JSON** (one or more sheets). It needs `openpyxl` (pip, no system deps).
The first row of each sheet becomes a bold, frozen header.

JSON input forms: `{"Sheet1": [["h1","h2"],[1,2]], "Sheet2": [...]}` or
`[{"name":"Sheet1","rows":[[...]]}]`.

1. `fs.write` your data into the workspace — `data.json` (or `data.csv`).
2. Generate via **job.start**, output into the workspace:

       job.start(name="make-xlsx", command=
         "bash -lc 'test -d /tmp/docenv || python -m venv /tmp/docenv; "
         "/tmp/docenv/bin/pip -q install openpyxl && "
         "/tmp/docenv/bin/python <files['write_xlsx.py']> data.json report.xlsx'")

3. `job.wait(<id>)`, then **`deliver.files(["report.xlsx"])`**.

For formulas, multiple formatted sheets, charts, or styling, write a short
`openpyxl` script (`fs.write` a `.py`, run it the same way) — `write_xlsx.py`
shows the basic pattern (headers, column widths, freeze panes) to extend.
