"""fs.find: locate files by name (glob or substring) before acting on them."""
import asyncio
from tools.fs.ops import FsFind
from runtime.tool_base import ToolContext


def _ws(tmp):
    (tmp/"4FMT"/"2026").mkdir(parents=True); (tmp/"5FMO"/"2026").mkdir(parents=True)
    (tmp/"4FMT"/"2026"/"Student_Overview.md").write_text("a")
    (tmp/"4FMT"/"2026"/"Sales_Pitch.md").write_text("b")
    (tmp/"5FMO"/"2026"/"Student_Overview.md").write_text("c")
    return tmp

def _run(args, root):
    return asyncio.run(FsFind().execute(args, ToolContext(request_id="t", config={"tools":{"fs":{"allowed_roots":[str(root)]}}}, budget=None, work_root=str(root))))

def test_glob_match(tmp_path):
    r=_run({"query":"Student_Overview*"}, _ws(tmp_path))
    assert r.status=="ok" and sorted(r.result["matches"])==["4FMT/2026/Student_Overview.md","5FMO/2026/Student_Overview.md"]

def test_substring_match(tmp_path):
    r=_run({"query":"pitch"}, _ws(tmp_path))   # case-insensitive substring
    assert r.result["matches"]==["4FMT/2026/Sales_Pitch.md"]

def test_scoped_path(tmp_path):
    r=_run({"query":"*.md","path":"5FMO"}, _ws(tmp_path))
    assert r.result["matches"]==["2026/Student_Overview.md"]
