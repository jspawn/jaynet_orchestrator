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


def test_recon_paths_needs_confirmation():
    """paths probes undisclosed routes with a model-brought wordlist — the
    one recon verb sending attacker-shaped requests (audit 2026-10-06 #11).
    Ledger reads and policy-bounded crawl/triage stay ungated."""
    t = sec.BrowserRecon()
    assert t.needs_confirmation({"action": "paths"}, None) is True
    for a in ("extract", "endpoints", "known", "crawl", "triage", "show",
              "export"):
        assert t.needs_confirmation({"action": a}, None) is False, a


def test_websec_active_verbs_need_confirmation():
    """Verbs that SEND live traffic (not read captures) ask the human first
    — same reason browser.test carries requires_confirmation."""
    t = sec.BrowserWebsec()
    for a in ("replay", "experiment", "matrix", "sequence", "socket"):
        assert t.needs_confirmation({"action": a}, None) is True, a
    assert t.needs_confirmation({"action": "grpc", "mode": "call"},
                                None) is True
    assert t.needs_confirmation({"action": "grpc", "mode": "describe"},
                                None) is False
    for a in ("requests", "show", "diff", "match", "sitemap", "finding",
              "import-nuclei"):
        assert t.needs_confirmation({"action": a}, None) is False, a


def test_websec_socket_grpc_ssrf_guard(monkeypatch):
    """socket/grpc dial model-supplied endpoints that bypass the session
    allowlist at the h5i level — same SSRF policy as browser.browse."""
    fake = _FakeH5i(monkeypatch)
    r = run(sec.BrowserWebsec().execute(
        {"action": "socket", "url": "ws://127.0.0.1:9000/x"}, _ctx()))
    assert r.status == "error" and "refuses" in r.error
    r = run(sec.BrowserWebsec().execute(
        {"action": "socket", "url": "http://example.com/x"}, _ctx()))
    assert r.status == "error" and "refused" in r.error   # not ws/wss
    r = run(sec.BrowserWebsec().execute(
        {"action": "grpc", "mode": "call", "symbol": "s/m", "data": "{}",
         "url": "169.254.169.254:443"}, _ctx()))
    assert r.status == "error" and "refuses" in r.error   # metadata endpoint
    assert fake.calls == []                               # never spawned
    # A public ws endpoint (resolvable or not) reaches the CLI.
    r = run(sec.BrowserWebsec().execute(
        {"action": "socket", "url": "wss://example.com/ws"}, _ctx()))
    assert r.status == "ok" and fake.calls


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
                             "--set", "query.id=456", "--set",
                             'json.role="admin"', "--unset", "header.x-debug",
                             "--create", "--", "req_42"]

    run(ws.execute({"action": "diff", "msg_id": "res_42",
                    "other_id": "res_43"}, _ctx()))
    assert fake.calls[1][-3:] == ["--", "res_42", "res_43"]

    run(ws.execute({"action": "show", "msg_id": "req_42", "raw": True}, _ctx()))
    assert fake.calls[2][-3:] == ["--raw", "--", "req_42"]

    run(ws.execute({"action": "match", "msg_id": "res_42",
                    "contains": "Welcome admin"}, _ctx()))
    assert fake.calls[3] == ["websec", "-s", "jaynet-req-abcd", "match",
                             "--contains", "Welcome admin", "--", "res_42"]

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


# ---- recon paths ------------------------------------------------------------

def test_recon_paths_argv(monkeypatch, tmp_path):
    fake = _FakeH5i(monkeypatch)
    ctx = ToolContext(request_id="req-abcdef012345", config=CFG, budget=None,
                      tmp_root=str(tmp_path))
    r = run(sec.BrowserRecon().execute(
        {"action": "paths", "wordlist": ["admin", "backup.zip"],
         "reuse_words": True, "under": ["/api", "/app"],
         "extensions": "php,bak", "backups": True,
         "origin": "https://t.example", "max_requests": 100, "rate": 2}, ctx))
    assert r.status == "ok"
    argv = fake.calls[0]
    assert argv[:4] == ["recon", "-s", "jaynet-req-abcd", "paths"]
    # an inline wordlist is written to a scratch file h5i can read
    wl = Path(argv[argv.index("--wordlist") + 1])
    assert wl.read_text().splitlines() == ["admin", "backup.zip"]
    assert "--reuse-words" in argv and "--backups" in argv
    assert argv[argv.index("--under") + 1] == "/api"
    assert argv[argv.index("--extensions") + 1] == "php,bak"
    assert argv[argv.index("--origin") + 1] == "https://t.example"
    assert argv[-4:] == ["--max-requests", "100", "--rate", "2"]

    run(sec.BrowserRecon().execute(
        {"action": "paths", "wordlist_file": "words.txt"}, ctx))
    argv = fake.calls[1]
    # a file path passes through (no work_root here) and the request budget
    # defaults match crawl's
    assert argv[argv.index("--wordlist") + 1] == "words.txt"
    assert argv[-4:] == ["--max-requests", "200", "--rate", "4"]

    r = run(sec.BrowserRecon().execute({"action": "paths"}, ctx))
    assert r.status == "error" and "missing argument" in r.error


# ---- websec multi-send orchestration ----------------------------------------

def test_websec_experiment_and_sequence(monkeypatch, tmp_path):
    fake = _FakeH5i(monkeypatch)
    ws = sec.BrowserWebsec()
    ctx = ToolContext(request_id="req-abcdef012345", config=CFG, budget=None,
                      tmp_root=str(tmp_path), work_root=str(tmp_path))
    plan = '{"request": "req_42", "positions": [], "strategy": "product"}'
    run(ws.execute({"action": "experiment", "plan": plan}, ctx))
    argv = fake.calls[0]
    assert argv[:4] == ["websec", "-s", "jaynet-req-abcd", "experiment"]
    assert Path(argv[4]).read_text().strip() == plan

    run(ws.execute({"action": "sequence", "plan": "seq.json",
                    "vars": ["user=bob"], "keep_going": True}, ctx))
    argv = fake.calls[1]
    # a plan that isn't inline content is a workspace path, resolved against
    # the work_root (h5i's cwd is the orchestrator's)
    assert argv == ["websec", "-s", "jaynet-req-abcd", "sequence",
                    "--var", "user=bob", "--keep-going",
                    str(tmp_path / "seq.json")]

    r = run(ws.execute({"action": "experiment"}, ctx))
    assert r.status == "error" and "missing argument" in r.error


def test_websec_matrix(monkeypatch):
    fake = _FakeH5i(monkeypatch)
    ws = sec.BrowserWebsec()
    run(ws.execute({"action": "matrix", "msg_id": "req_42",
                    "identities": ["anon", "userB", "admin"],
                    "set": ["query.id=7"], "rate": 2,
                    "keep_credentials": True}, _ctx()))
    assert fake.calls[0] == ["websec", "-s", "jaynet-req-abcd", "matrix",
                             "--as", "anon,userB,admin",
                             "--set", "query.id=7", "--rate", "2",
                             "--keep-credentials", "--", "req_42"]
    for args in ({"action": "matrix", "msg_id": "req_42"},
                 {"action": "matrix", "identities": ["anon"]}):
        r = run(ws.execute(args, _ctx()))
        assert r.status == "error" and "missing argument" in r.error, args


def test_websec_socket(monkeypatch):
    fake = _FakeH5i(monkeypatch)
    ws = sec.BrowserWebsec()
    run(ws.execute({"action": "socket", "url": "wss://t.example/ws",
                    "send": ["hello", "ping"], "wait_ms": 500}, _ctx()))
    assert fake.calls[0] == ["websec", "-s", "jaynet-req-abcd", "socket",
                             "--send", "hello", "--send", "ping",
                             "--wait-ms", "500", "--", "wss://t.example/ws"]
    r = run(ws.execute({"action": "socket"}, _ctx()))
    assert r.status == "error" and "missing argument" in r.error


def test_websec_dom(monkeypatch):
    fake = _FakeH5i(monkeypatch)
    ws = sec.BrowserWebsec()
    run(ws.execute({"action": "dom", "mode": "scan",
                    "urls": ["https://t.example/"], "settle": 300}, _ctx()))
    assert fake.calls[0] == ["websec", "-s", "jaynet-req-abcd", "dom", "scan",
                             "--settle", "300", "--", "https://t.example/"]
    run(ws.execute({"action": "dom", "mode": "node", "box": "eng-1",
                    "command": ["node", "server.js"], "timeout_s": 10}, _ctx()))
    assert fake.calls[1] == ["websec", "-s", "jaynet-req-abcd", "dom", "node",
                             "--box", "eng-1", "--timeout", "10",
                             "--", "node", "server.js"]
    for args in ({"action": "dom"},
                 {"action": "dom", "mode": "node", "box": "eng-1"},
                 {"action": "dom", "mode": "bogus"}):
        r = run(ws.execute(args, _ctx()))
        assert r.status == "error", args


def test_websec_grpc(monkeypatch, tmp_path):
    fake = _FakeH5i(monkeypatch)
    ws = sec.BrowserWebsec()
    ctx = ToolContext(request_id="req-abcdef012345", config=CFG, budget=None,
                      work_root=str(tmp_path))
    run(ws.execute({"action": "grpc", "mode": "describe",
                    "symbol": "pkg.Service", "url": "http://host:50051",
                    "reflect": True, "insecure": True}, ctx))
    assert fake.calls[0] == ["websec", "-s", "jaynet-req-abcd", "grpc",
                             "describe", "--url", "http://host:50051",
                             "--reflect", "--insecure", "--", "pkg.Service"]
    run(ws.execute({"action": "grpc", "mode": "call",
                    "symbol": "pkg.Service/Get", "data": '{"id": 1}',
                    "url": "http://host:50051",
                    "metadata": ["authorization:Bearer x"],
                    "server_streaming": True}, ctx))
    assert fake.calls[1] == ["websec", "-s", "jaynet-req-abcd", "grpc",
                             "call", "--data", '{"id": 1}',
                             "--url", "http://host:50051",
                             "--metadata", "authorization:Bearer x",
                             "--server-streaming", "--", "pkg.Service/Get"]
    # describe with no symbol lists every service — valid; proto resolves
    # against the work_root
    run(ws.execute({"action": "grpc", "mode": "describe",
                    "proto": ["svc.proto"]}, ctx))
    assert fake.calls[2] == ["websec", "-s", "jaynet-req-abcd", "grpc",
                             "describe", "--proto",
                             str(tmp_path / "svc.proto")]
    r = run(ws.execute({"action": "grpc", "mode": "call",
                        "symbol": "pkg.Service/Get"}, ctx))
    assert r.status == "error" and "missing argument" in r.error


def test_websec_import_nuclei(monkeypatch, tmp_path):
    fake = _FakeH5i(monkeypatch)
    ws = sec.BrowserWebsec()
    ctx = ToolContext(request_id="req-abcdef012345", config=CFG, budget=None,
                      work_root=str(tmp_path))
    run(ws.execute({"action": "import-nuclei", "file": "cve.yaml"}, ctx))
    assert fake.calls[0] == ["websec", "-s", "jaynet-req-abcd",
                             "import-nuclei", "--", str(tmp_path / "cve.yaml")]
    r = run(ws.execute({"action": "import-nuclei"}, ctx))
    assert r.status == "error" and "missing argument" in r.error


# ---- browser.test (h5i-test) -------------------------------------------------

def test_browser_test_flags_and_argv(monkeypatch, tmp_path):
    """browser.test actively attacks the target — confirmation required,
    results private like the other red-team tools."""
    assert sec.BrowserTest.requires_confirmation is True
    assert sec.BrowserTest.private is True
    fake = _FakeH5i(monkeypatch)
    t = sec.BrowserTest()
    ctx = ToolContext(request_id="req-abcdef012345", config=CFG, budget=None,
                      work_root=str(tmp_path))
    r = run(t.execute({"target": "https://t.example"}, ctx))
    assert r.status == "ok"
    # the CLI default (.h5i-tests/tests) is pointed at the workspace, since
    # h5i's cwd is the orchestrator's
    assert fake.calls[0] == ["test", "--target", "https://t.example",
                             "--", str(tmp_path / ".h5i-tests/tests")]
    run(t.execute({"target": "https://t.example", "path": "tests/x.yaml",
                   "openapi": "api.yaml", "min_coverage": 60,
                   "json": True}, ctx))
    assert fake.calls[1] == ["test", "--target", "https://t.example",
                             "--openapi", str(tmp_path / "api.yaml"),
                             "--min-coverage", "60", "--json",
                             "--", str(tmp_path / "tests/x.yaml")]
    r = run(t.execute({}, ctx))
    assert r.status == "error" and "target" in r.error


def test_browser_test_no_work_root_uses_cli_default(monkeypatch):
    fake = _FakeH5i(monkeypatch)
    run(sec.BrowserTest().execute({"target": "https://t.example"}, _ctx()))
    assert fake.calls[0] == ["test", "--target", "https://t.example"]


def test_browser_test_missing_plugin_hint(monkeypatch):
    _FakeH5i(monkeypatch, out=None,
             err="error: unrecognized subcommand 'test'")
    r = run(sec.BrowserTest().execute({"target": "https://t.example"}, _ctx()))
    assert r.status == "error"
    assert "plugin install test" in r.error
