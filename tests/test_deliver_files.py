"""deliver.files workspace confinement: the tool hands over artifacts the agent
produced (inside the run's work_root/tmp_root) and refuses arbitrary host paths.
No network, no browser — staging happens under a tmp outputs dir."""
import asyncio

from runtime.outputs import read_manifest
from runtime.tool_base import ToolContext
from tools.deliver.files import DeliverFiles


def _ctx(tmp_path, work_root=None):
    cfg = {"web": {"outputs_dir": str(tmp_path / "outputs"), "max_output_mb": 50}}
    return ToolContext(request_id="run1", config=cfg, budget=None, owner="alice",
                       work_root=str(work_root) if work_root else None)


def _run(args, ctx):
    return asyncio.run(DeliverFiles().execute(args, ctx))


def test_deliver_inside_workspace_ok(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "report.txt").write_text("hello")
    ctx = _ctx(tmp_path, work)
    r = _run({"paths": [str(work / "report.txt")]}, ctx)
    assert r.status == "ok" and r.result["delivered"] == "report.txt"
    m = read_manifest(str(tmp_path / "outputs"), "run1")
    assert m and m["owner"] == "alice"


def test_deliver_relative_path_resolves_in_workspace(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "out.csv").write_text("a,b")
    ctx = _ctx(tmp_path, work)
    r = _run({"paths": ["out.csv"]}, ctx)
    assert r.status == "ok"


def test_deliver_outside_workspace_refused(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    ctx = _ctx(tmp_path, work)
    for p in ("/etc/passwd", str(tmp_path / "outputs"), "../../secret"):
        r = _run({"paths": [p]}, ctx)
        assert r.status == "error" and "outside your workspace" in r.error, p
    # nothing staged
    assert read_manifest(str(tmp_path / "outputs"), "run1") is None


def test_deliver_tmp_root_is_allowed(tmp_path):
    work = tmp_path / "work"
    tmpd = tmp_path / "tmp"
    work.mkdir()
    tmpd.mkdir()
    (tmpd / "chart.png").write_bytes(b"\x89PNG")
    ctx = _ctx(tmp_path, work)
    ctx.tmp_root = str(tmpd)
    r = _run({"paths": [str(tmpd / "chart.png")]}, ctx)
    assert r.status == "ok"


def test_deliver_no_workspace_cli_path_unconfined(tmp_path):
    """No work_root and no tools.fs.allowed_roots -> bare CLI path, no boundary."""
    f = tmp_path / "loose.txt"
    f.write_text("x")
    ctx = _ctx(tmp_path)
    r = _run({"paths": [str(f)]}, ctx)
    assert r.status == "ok"
