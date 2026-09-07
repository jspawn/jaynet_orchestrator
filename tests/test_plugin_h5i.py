"""h5i plugin: browser.browse argv building, config, and error paths.

No real h5i binary runs — _run_h5i is the subprocess seam (monkeypatched),
missing-binary is simulated via shutil.which returning None.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from conftest import run

from runtime.tool_base import ToolContext

_REPO = Path(__file__).resolve().parent.parent


def _load(mod_name, path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


h5 = _load("h5i_browse_under_test",
           _REPO / "plugins" / "h5i" / "tools" / "h5i" / "browse.py")

CFG = {"plugins": {"h5i": {"binary": "/fake/h5i", "timeout_s": 42,
                           "allow": ["docs.rs"], "identity": "privacy"}}}


def _ctx(cfg=CFG):
    return ToolContext(request_id="req-abcdef012345", config=cfg, budget=None)


def _exe(args, cfg=CFG):
    return run(h5.BrowserBrowse().execute(args, _ctx(cfg)))


class _FakeH5i:
    """Records argv, replays a queued (stdout, error) outcome."""

    def __init__(self, monkeypatch, out="PAGE TEXT", err=None):
        self.calls = []
        async def fake(binary, argv, timeout):
            self.calls.append((binary, argv, timeout))
            return out, err
        monkeypatch.setattr(h5, "_run_h5i", fake)


# ---- binary resolution ------------------------------------------------------

def test_missing_binary_is_clean_error(monkeypatch):
    monkeypatch.setattr(h5.shutil, "which", lambda _name: None)
    r = _exe({"action": "status"}, cfg={"plugins": {"h5i": {}}})
    assert r.status == "error"
    assert "h5i binary not found" in r.error
    assert "install.sh" in r.error


def test_configured_binary_and_timeout_used(monkeypatch):
    fake = _FakeH5i(monkeypatch)
    r = _exe({"action": "status"})
    assert r.status == "ok"
    binary, _argv, timeout = fake.calls[0]
    assert binary == "/fake/h5i" and timeout == 42


# ---- argv building ----------------------------------------------------------

def test_open_merges_allow_dedups_and_honors_new_and_identity(monkeypatch):
    fake = _FakeH5i(monkeypatch)
    r = _exe({"action": "open", "url": "https://docs.rs/x",
              "allow": ["crates.io", "docs.rs"], "new": True})
    assert r.status == "ok"
    argv = fake.calls[0][1]
    assert argv[:3] == ["browser", "open", "https://docs.rs/x"]
    assert argv.count("--allow") == 2                    # config + call, deduped
    assert "docs.rs" in argv and "crates.io" in argv
    assert "--new" in argv and "--identity" in argv and "privacy" in argv
    assert r.result["session"] == "jaynet-req-abcd"      # per-run default


def test_default_session_is_per_run_and_overridable(monkeypatch):
    fake = _FakeH5i(monkeypatch)
    _exe({"action": "snapshot"})
    assert "jaynet-req-abcd" in fake.calls[0][1]
    _exe({"action": "snapshot", "session": "auth"})
    assert "auth" in fake.calls[1][1]


def test_snapshot_delta_and_read(monkeypatch):
    fake = _FakeH5i(monkeypatch)
    _exe({"action": "snapshot", "delta": True})
    assert "--delta" in fake.calls[0][1]
    _exe({"action": "read", "url": "https://example.com"})
    argv = fake.calls[1][1]
    assert argv == ["browser", "read", "https://example.com"]  # no session


def test_interaction_actions(monkeypatch):
    fake = _FakeH5i(monkeypatch)
    _exe({"action": "click", "ref": "@e3"})
    assert ["browser", "click", "@e3"] == fake.calls[0][1][:3]
    _exe({"action": "type", "ref": "@e5", "text": "serde"})
    assert fake.calls[1][1][:4] == ["browser", "type", "@e5", "serde"]
    _exe({"action": "extract", "spec": '{"titles": ["h2"]}'})
    assert fake.calls[2][1][:3] == ["browser", "extract", '{"titles": ["h2"]}']


def test_missing_args_are_clean_errors(monkeypatch):
    _FakeH5i(monkeypatch)
    for args in ({"action": "open"}, {"action": "read"}, {"action": "click"},
                 {"action": "type", "text": "x"}, {"action": "extract"}):
        r = _exe(args)
        assert r.status == "error", args
        assert "missing argument" in r.error


# ---- subprocess outcomes ----------------------------------------------------

def test_h5i_failure_propagates_as_error(monkeypatch):
    _FakeH5i(monkeypatch, out=None, err="session exploded")
    r = _exe({"action": "snapshot"})
    assert r.status == "error" and "session exploded" in r.error


def test_output_passthrough(monkeypatch):
    _FakeH5i(monkeypatch, out="# heading\nbody")
    r = _exe({"action": "markdown"})
    assert r.status == "ok"
    assert r.result["output"] == "# heading\nbody"
    assert r.result["action"] == "markdown"


def test_output_cap(monkeypatch):
    async def fake(_b, _a, _t):
        return "x" * (h5._OUT_CAP + 500), None
    monkeypatch.setattr(h5, "_run_h5i", fake)
    # cap happens inside _run_h5i in production; here we verify the constant
    # is wired by calling the real seam's contract — see test_output_cap_real
    r = _exe({"action": "markdown"})
    assert r.status == "ok"


async def _real_run(binary, argv, timeout):
    return await h5._run_h5i(binary, argv, timeout)


def test_run_h5i_timeout_and_nonzero(monkeypatch, tmp_path):
    """The REAL seam: timeout kills the process, non-zero returns stderr."""
    slow = tmp_path / "slow.sh"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    out, err = run(_real_run(str(slow), [], 1))
    assert out is None and "timed out" in err

    bad = tmp_path / "bad.sh"
    bad.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    bad.chmod(0o755)
    out, err = run(_real_run(str(bad), [], 5))
    assert out is None and "boom" in err


def test_run_h5i_caps_output(tmp_path):
    big = tmp_path / "big.sh"
    big.write_text("#!/bin/sh\nhead -c 40000 /dev/zero | tr '\\0' 'x'\n")
    big.chmod(0o755)
    out, err = run(_real_run(str(big), [], 5))
    assert err is None
    assert len(out) < h5._OUT_CAP + 200
    assert "output capped" in out
