---
name: xlsx
description: Read data from Excel .xlsx spreadsheets (cells and sheets as text/TSV). Load when an .xlsx is uploaded or referenced and you need its data.
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
the same logic inline with `code.execute` (no job needed). By default those aren't
allowed, so prefer the job.

## Notes

- This reads **stored values**, not formulas — a cell showing `=SUM(...)` returns
  its last-computed value, and an unopened/never-calculated file may store none.
- Number formatting (dates shown as serial numbers, currency, %) is *display*
  formatting; you get the raw stored value. Interpret dates/serials with care and
  say so if it matters.
- For multiple sheets, formulas, merged cells, or writing changes back, use
  `openpyxl` in a venv (via job.start) instead — offer that if the task needs it.
