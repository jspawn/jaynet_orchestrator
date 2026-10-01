"""Static sanity checks for the admin SPA (web/static/admin.html).

The admin page is one big HTML file with inline <script> — no bundler, no
linter. A duplicate top-level function declaration silently overrides the
earlier one (JS hoisting: the LAST declaration wins), which once left the
Usage tab dead: two `loadUsage` functions existed and the wrong one ran.
"""

import re
from pathlib import Path

ADMIN_HTML = Path(__file__).resolve().parent.parent / "web" / "static" / "admin.html"


def test_no_duplicate_function_declarations():
    src = ADMIN_HTML.read_text(encoding="utf-8")
    names = re.findall(r"(?:^|\n)\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", src)
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"duplicate function declarations in admin.html: {dupes}"


def _tabmap_ids(src: str) -> set[str]:
    """The pane + sub ids TABMAP routes to — the reorg's load-bearing contract
    (audit #26: verified by hand once; this pins it for every future reorg)."""
    m = re.search(r"const TABMAP=\{(.*?)\};", src, re.S)
    assert m, "TABMAP not found in admin.html"
    return set(re.findall(r'"(pane-[\w-]+|tab-[\w-]+|sub-[\w-]+)"', m.group(1)))


def test_tabmap_targets_exist():
    src = ADMIN_HTML.read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([\w-]+)"', src))
    missing = sorted(i for i in _tabmap_ids(src) if i not in ids)
    assert not missing, f"TABMAP routes to ids with no element: {missing}"


def test_js_referenced_ids_exist():
    """Every literal $("#id")/getElementById("id") the inline script addresses
    must have a matching id in the markup — a typo here dies silently at
    runtime (null.x), and there is no bundler or linter to catch it."""
    src = ADMIN_HTML.read_text(encoding="utf-8")
    # plain-text scan: also covers ids the script generates into innerHTML
    ids = set(re.findall(r'id="([\w-]+)"', src))
    refs = set(re.findall(r'\$\("#([A-Za-z][\w-]*)"', src))
    refs |= set(re.findall(r'getElementById\("([\w-]+)"', src))
    missing = sorted(r for r in refs if r not in ids)
    assert not missing, f"script references ids with no element: {missing}"
