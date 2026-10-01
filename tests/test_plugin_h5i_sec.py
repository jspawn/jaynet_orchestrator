"""h5i plugin red-team tools: browser.recon / browser.websec argv building,
privacy flag, error paths. No real h5i runs — _run_h5i is monkeypatched.
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


sec = _load("h5i_sec_under_test",
            _REPO / "plugins" / "h5i" / "tools" / "h5i" / "sec.py")

CFG = {"plugins": {"h5i": {"binary": "/fake/h5i", "timeout_s": 42}}}


def _ctx(cfg=CFG):
    return ToolContext(request_id="req-abcdef012345", config=cfg, budget=None)


class _FakeH5i:
    def __init__(self, monkeypatch, out="OK", err=None):
        self.calls = []

        async def fake(binary, argv, timeout):
            self.calls.append(argv)
            return out, err
        monkeypatch.setattr(sec, "_run_h5i", fake)


def test_both_tools_private():
    """Captures carry Authorization/cookies — both red-team tools are
    private=True so their results never leave the box by default."""
    assert sec.BrowserRecon.private is True
    assert sec.BrowserWebsec.private is True


def test_recon_core_loop_argv(monkeypatch):
    fake = _FakeH5i(monkeypatch)
    r = run(sec.BrowserRecon().execute({"action": "extract"}, _ctx()))
    assert r.status == "ok"
    assert fake.calls[0] == ["recon", "-s", "jaynet-req-abcd", "extract"]

    run(sec.BrowserRecon().execute(
        {"action": "endpoints", "state": "confirmed"}, _ctx()))
    assert fake.calls[1] == ["recon", "-s", "jaynet-req-abcd", "endpoints",
                             "--state", "confirmed"]

    run(sec.BrowserRecon().execute(
        {"action": "crawl", "max_requests": 50, "rate": 2,
         "seed": "https://t.example", "depth": 2}, _ctx()))
    assert fake.calls[2] == ["recon", "-s", "jaynet-req-abcd", "crawl",
                             "--seed", "https://t.example", "--depth", "2",
                             "--max-requests", "50", "--rate", "2"]

    run(sec.BrowserRecon().execute({"action": "triage"}, _ctx()))
    assert "--calibrate" in fake.calls[3]

    run(sec.BrowserRecon().execute(
        {"action": "show", "endpoint_id": "ep_1af62d68"}, _ctx()))
    assert fake.calls[4][-1] == "ep_1af62d68"

    r = run(sec.BrowserRecon().execute({"action": "show"}, _ctx()))
    assert r.status == "error" and "missing argument" in r.error


def test_websec_replay_diff_match_finding(monkeypatch):
    fake = _FakeH5i(monkeypatch)
    ws = sec.BrowserWebsec()
    run(ws.execute({"action": "replay", "msg_id": "req_42",
                    "set": ["query.id=456", 'json.role="admin"'],
                    "unset": ["header.x-debug"], "create": True}, _ctx()))
    assert fake.calls[0] == ["websec", "-s", "jaynet-req-abcd", "replay",
                             "req_42", "--set", "query.id=456", "--set",
                             'json.role="admin"', "--unset", "header.x-debug",
                             "--create"]

    run(ws.execute({"action": "diff", "msg_id": "res_42",
                    "other_id": "res_43"}, _ctx()))
    assert fake.calls[1][-2:] == ["res_42", "res_43"]

    run(ws.execute({"action": "show", "msg_id": "req_42", "raw": True}, _ctx()))
    assert fake.calls[2][-2:] == ["req_42", "--raw"]

    run(ws.execute({"action": "match", "msg_id": "res_42",
                    "contains": "Welcome admin"}, _ctx()))
    assert fake.calls[3] == ["websec", "-s", "jaynet-req-abcd", "match",
                             "res_42", "--contains", "Welcome admin"]

    r = run(ws.execute({"action": "match", "msg_id": "res_42"}, _ctx()))
    assert r.status == "error", "match without an assertion is a clean error"

    run(ws.execute({"action": "finding", "title": "IDOR on /orders",
                    "evidence": "req_42,res_43", "note": "repeatable",
                    "state": "confirmed"}, _ctx()))
    assert fake.calls[4] == ["websec", "-s", "jaynet-req-abcd", "finding",
                             "create", "--title", "IDOR on /orders",
                             "--evidence", "req_42,res_43", "--note",
                             "repeatable", "--state", "confirmed"]

    r = run(ws.execute({"action": "finding"}, _ctx()))
    assert r.status == "error" and "missing argument" in r.error


def test_websec_missing_ids_are_clean_errors(monkeypatch):
    _FakeH5i(monkeypatch)
    ws = sec.BrowserWebsec()
    for args in ({"action": "show"}, {"action": "replay"},
                 {"action": "diff", "msg_id": "res_1"}):
        r = run(ws.execute(args, _ctx()))
        assert r.status == "error" and "missing argument" in r.error, args


def test_missing_plugin_binary_hint(monkeypatch):
    """A not-installed recon/websec verb surfaces as an h5i usage error —
    the tool translates it into the install hint."""
    _FakeH5i(monkeypatch, out=None,
             err="error: unrecognized subcommand 'recon'")
    r = run(sec.BrowserRecon().execute({"action": "extract"}, _ctx()))
    assert r.status == "error"
    assert "plugin install recon" in r.error
