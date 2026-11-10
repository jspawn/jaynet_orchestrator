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


def test_preset_catalog_priority_drag_drop_hooks():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    js = ADMIN_JS.read_text(encoding="utf-8")
    css = (STATIC / "admin.css").read_text(encoding="utf-8")
    assert "<th>Priority</th><th>Name</th>" in html
    assert "strength routing and failover" in html
    assert "Boot model slots stay independent" in html
    assert "archived and helper presets remain reorderable" in html
    assert 'class="preset-row' in js and 'draggable="true"' in js
    assert "row.ondragstart" in js and "row.ondragover" in js
    assert "row.ondrop=e=>presetDrop" in js
    assert 'api("/api/admin/preset-order"' in js
    assert "body:JSON.stringify({names})" in js
    save = js.split("async function savePresetOrder(){", 1)[1].split("\n}", 1)[0]
    assert "await loadPresets();" in save
    assert 'filter(q=>!q.archived&&q.alias!=="local-orchestrator"&&(q.strengths||[]).includes(tag))' in js
    assert "archived, not routed" in js
    assert "not a specialist candidate" in js
    assert "last fallback" in js and 'p.alias==="local-orchestrator"' in js
    assert ".preset-drag-handle" in css and ".preset-drop-after" in css
    # Asset references need a numeric buster, not one specific release's value.
    assert re.search(r"/static/admin\.js\?v=\d+", html)
    assert re.search(r"/static/admin\.css\?v=\d+", html)


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


def test_hf_preset_suggestion_opens_visible_loaded_editor():
    """Create preset must leave Files for the editor under Models → Presets.
    Opening Files directly must also fetch the catalog editPreset reads."""
    src = ADMIN_JS.read_text(encoding="utf-8")
    block = src.split("async function hfPreset(j){", 1)[1].split("\n}", 1)[0]
    load = 'if(!_presets) await loadPresets();'
    navigate = 'goTab("models","presets");'
    edit = "editPreset(null);"
    assert load in block and navigate in block and edit in block
    assert block.index(load) < block.index(navigate) < block.index(edit)
    assert 'note($("#peNote"),msg,true);' in block
