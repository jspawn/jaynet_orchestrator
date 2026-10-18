"""Static sanity checks for the admin SPA (web/static/admin.html + admin.js).

The admin page's logic lives in admin.js (extracted from the once-inline
<script> block, 2026-10-14) — no bundler, no linter. A duplicate top-level
function declaration silently overrides the earlier one (JS hoisting: the
LAST declaration wins), which once left the Usage tab dead: two `loadUsage`
functions existed and the wrong one ran.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
ADMIN_HTML = STATIC / "admin.html"
ADMIN_JS = STATIC / "admin.js"


def test_no_duplicate_function_declarations():
    src = ADMIN_JS.read_text(encoding="utf-8")
    names = re.findall(r"(?:^|\n)\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", src)
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"duplicate function declarations in admin.js: {dupes}"


def _tabmap_ids(src: str) -> set[str]:
    """The pane + sub ids TABMAP routes to — the reorg's load-bearing contract
    (audit #26: verified by hand once; this pins it for every future reorg)."""
    m = re.search(r"const TABMAP=\{(.*?)\};", src, re.S)
    assert m, "TABMAP not found in admin.js"
    return set(re.findall(r'"(pane-[\w-]+|tab-[\w-]+|sub-[\w-]+)"', m.group(1)))


def test_tabmap_targets_exist():
    js = ADMIN_JS.read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([\w-]+)"', ADMIN_HTML.read_text(encoding="utf-8")))
    missing = sorted(i for i in _tabmap_ids(js) if i not in ids)
    assert not missing, f"TABMAP routes to ids with no element: {missing}"


def test_js_referenced_ids_exist():
    """Every literal $("#id")/getElementById("id") the script addresses must
    have a matching id — either in admin.html's markup or generated into
    innerHTML by admin.js itself (id="..." literals in template strings).
    A typo here dies silently at runtime (null.x), and there is no bundler
    or linter to catch it."""
    js = ADMIN_JS.read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([\w-]+)"', ADMIN_HTML.read_text(encoding="utf-8")))
    ids |= set(re.findall(r'id=\\?"([\w-]+)\\?"', js))   # innerHTML-generated
    refs = set(re.findall(r'\$\("#([A-Za-z][\w-]*)"', js))
    refs |= set(re.findall(r'getElementById\("([\w-]+)"', js))
    missing = sorted(r for r in refs if r not in ids)
    assert not missing, f"script references ids with no element: {missing}"


def _subpane(html: str, pane_id: str) -> str:
    start = html.index(f'<div class="subpane" id="{pane_id}">')
    end = html.find('<div class="subpane"', start + 1)
    return html[start:end if end != -1 else len(html)]


def test_eval_pane_organization():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    assert (html.index('data-sub="results"') < html.index('data-sub="details"')
            < html.index('data-sub="proposals"'))

    cases = _subpane(html, "tab-eval")
    assert (cases.index("<h2>Run</h2>") < cases.index("<h2>Scheduled runs</h2>")
            < cases.index("<h2>Eval cases"))

    results = _subpane(html, "sub-studio-results")
    assert (results.index("<h2>Statistics") < results.index("<h2>Overall trend</h2>")
            < results.index("<h2>Results"))

    details = _subpane(html, "sub-studio-details")
    assert (details.index("<h2>Tool telemetry") < details.index("<h2>Per case</h2>")
            < details.index("<h2>Period comparison</h2>"))
