---
name: xlsx
description: Read data from, or CREATE, Excel .xlsx spreadsheets. Load when an .xlsx is uploaded/referenced and you need its data, or when the user asks to create/write/generate a spreadsheet.
---
# Reading Excel (.xlsx) files

For stored values, start with **`doc.extract(path="...")`**: it returns each
sheet as text, with no polling or helper script. For custom reads, use
**`code.check(language="python")`** with `openpyxl` (the devbox ships it).

The bundled `read_xlsx.py` is a standard-library alternative via **job.start**
(full host environment):

    job.start(name="read-xlsx",
              command="python <files['read_xlsx.py']> <path-to-file.xlsx>")

It prints each sheet as a `# xl/worksheets/sheetN.xml` header followed by
tab-separated rows. Read it back with `job.logs`.

The `<files[...]>` paths are HOST paths and are not automatically mounted in
devbox. Do not paste them into `code.check` shell commands; use an inline
`openpyxl` snippet there, or run the bundled script through `job.start`.

## Notes

- This reads **stored values**, not formulas — a cell showing `=SUM(...)` returns
  its last-computed value, and an unopened/never-calculated file may store none.
- Number formatting (dates shown as serial numbers, currency, %) is *display*
  formatting; you get the raw stored value. Interpret dates/serials with care and
  say so if it matters.
- For multiple sheets, formulas, merged cells, or writing changes back, use
  `openpyxl` with `code.check` (or a venv via job.start) instead.

## Cell background/fill colors

`doc.extract` and `read_xlsx.py` cover **values**, not formatting. For a cell's
background/fill color, use **`code.check(language="python")`** with `openpyxl`
(the devbox ships it), passing the spreadsheet path from `fs.list`:

    from openpyxl import load_workbook
    ws = load_workbook(path).active          # or wb["SheetName"]
    rgb = ws.cell(row=2, column=3).fill.fgColor.rgb   # '00F478A7'-style ARGB, or None
    print(rgb)

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
2. Generate with **job.start**, then wait for completion:

       job.start(name="write-xlsx",
                 command="python <files['write_xlsx.py']> data.json report.xlsx")
       job.wait(<id>)

   Prefer the bundled script — it is tested. Without `job.start`, use a short
   `openpyxl` snippet with **code.check(language="python")** instead (the
   devbox ships openpyxl); do not substitute the host `<files[...]>` path into
   a devbox shell command.
3. **`deliver.files(["report.xlsx"])`**.

For formulas, multiple formatted sheets, charts, or styling, extend the basic
pattern (headers, column widths, freeze panes) in an inline `openpyxl` snippet
with `code.check`.
