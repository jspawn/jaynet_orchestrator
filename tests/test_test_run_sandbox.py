"""test.run quick-path sandbox default (audit tier-3 #14 leftover).

The quick path defaults to a firejail wrapper shaped like code.run's bash
sandbox but WITHOUT --net=none (suites may need network). A missing firejail
binary degrades to a bare run with a sandbox_warning — never a refusal —
because CI/dev boxes run without firejail. An explicit sandbox_prefix: []
stays the operator opt-out. The subprocess is faked; these tests pin the argv
the runner builds, not firejail itself.
"""

import asyncio
import shutil

from tools.test.runner import TestRun


class _FakeProc:
    """Stands in for the pytest subprocess: a passing one-line summary."""

    def __init__(self):
        self.returncode = 0
        self.pid = 1

    async def communicate(self):
        return b"1 passed in 0.01s\n", b""

    async def wait(self):
        return 0


def _run(ctx, monkeypatch, tmp_path, test_cfg, which):
    recorded = {}

    async def fake_exec(*argv, **kw):
        recorded["argv"] = list(argv)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", which)
    cfg = {"tools": {"test": {"workdir_root": str(tmp_path), **test_cfg}}}
    res = asyncio.run(TestRun().execute(
        {"test": "def test_ok(): pass"}, ctx(config=cfg)))
    return res, recorded["argv"]


def test_default_prefix_is_firejail_and_keeps_network(ctx, monkeypatch, tmp_path):
    res, argv = _run(ctx, monkeypatch, tmp_path, {},
                     lambda name: f"/usr/bin/{name}")
    assert res.status == "ok"
    assert argv[0] == "firejail"
    assert "--private-tmp" in argv
    assert "--net=none" not in argv          # tests keep network
    assert any(a.startswith("--whitelist=") for a in argv)
    assert argv[-3:-1] == ["bash", "-lc"]
    assert "sandbox_warning" not in res.result


def test_missing_firejail_degrades_with_warning(ctx, monkeypatch, tmp_path):
    res, argv = _run(ctx, monkeypatch, tmp_path, {}, lambda name: None)
    assert res.status == "ok"
    assert argv[0] == "bash"                 # bare, but not silent
    assert "firejail" in res.result["sandbox_warning"]


def test_explicit_empty_prefix_opts_out(ctx, monkeypatch, tmp_path):
    res, argv = _run(ctx, monkeypatch, tmp_path, {"sandbox_prefix": []},
                     lambda name: f"/usr/bin/{name}")
    assert res.status == "ok"
    assert argv[0] == "bash"
    assert "sandbox_warning" not in res.result


def test_custom_prefix_honored(ctx, monkeypatch, tmp_path):
    res, argv = _run(ctx, monkeypatch, tmp_path,
                     {"sandbox_prefix": ["bwrap", "--unshare-net"]},
                     lambda name: f"/usr/bin/{name}")
    assert res.status == "ok"
    assert argv[:2] == ["bwrap", "--unshare-net"]
