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
