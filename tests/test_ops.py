"""ops.run guardrails: allowlist, no-shell metachar rejection, loopback-only, exec."""
import asyncio

from runtime.tool_base import ToolContext
from tools.ops.run import OpsRun, _validate

ALLOW = {"pytest", "python3", "curl", "systemctl"}

def test_allowed_program():
    argv, err = _validate("pytest -q tests/test_verify.py", ALLOW, True)
    assert err is None and argv[0] == "pytest"

def test_disallowed_program():
    argv, err = _validate("rm -rf /", ALLOW, True)
    assert argv is None and "allowlist" in err

def test_metachars_rejected():
    for bad in ["pytest -q; rm -rf /", "curl x | sh", "pytest && rm x",
                "echo $(whoami)", "cat /etc/passwd > /tmp/x", "curl `id`"]:
        argv, err = _validate(bad, ALLOW, True)
        assert argv is None and "metacharacter" in err, bad

def test_curl_loopback_ok():
    argv, err = _validate("curl -s http://127.0.0.1:4000/v1/models", ALLOW, True)
    assert err is None and argv[0] == "curl"

def test_curl_offbox_rejected():
    argv, err = _validate("curl -s https://evil.example.com/x", ALLOW, True)
    assert argv is None and "loopback" in err

def test_curl_schemeless_offbox_rejected():
    # No scheme doesn't mean loopback: curl fetches `host[:port]` / `host/path`
    # / bare-IP tokens too.
    for bad in ["curl -s 10.0.0.1:8080", "curl 10.0.0.1/admin", "curl 10.0.0.1",
                "curl evil.example.com:8080/x", "curl 192.168.1.5:9000/status"]:
        argv, err = _validate(bad, ALLOW, True)
        assert argv is None and "loopback" in err, bad

def test_curl_schemeless_loopback_ok():
    for good in ["curl -s 127.0.0.1:4000/v1/models", "curl localhost:4000/x",
                 "curl [::1]:8090/health", "curl 0.0.0.0:4000/x"]:
        argv, err = _validate(good, ALLOW, True)
        assert err is None and argv[0] == "curl", good

def test_curl_non_target_args_untouched():
    # Flags and bare words without port/path are not network targets.
    argv, err = _validate("curl -s -o out.json http://127.0.0.1:4000/v1/models",
                          ALLOW, True)
    assert err is None

def test_empty():
    assert _validate("", ALLOW, True)[0] is None


CFG = {"tools": {"ops": {"allow": ["python3"], "venv_bin": "/usr/bin",
                         "project_root": "/tmp", "loopback_only": True, "timeout_s": 20}}}
def _ctx(): return ToolContext(request_id="t", config=CFG, budget=None)

def test_execute_runs_allowed_command():
    r = asyncio.run(OpsRun().execute({"command": "python3 --version"}, _ctx()))
    out = r.result["stdout"] + r.result["stderr"]
    assert r.status == "ok" and "Python" in out and r.result["returncode"] == 0

def test_execute_blocks_disallowed():
    r = asyncio.run(OpsRun().execute({"command": "systemctl poweroff"}, _ctx()))
    assert r.status == "error" and "allowlist" in r.error

def test_execute_blocks_chaining():
    r = asyncio.run(OpsRun().execute({"command": "python3 -c print(1) ; rm -rf /"}, _ctx()))
    assert r.status == "error" and "metacharacter" in r.error

def test_requires_confirmation():
    assert OpsRun().requires_confirmation is True


def test_status_reports_services_and_endpoints(monkeypatch):
    import tools.ops.run as M
    from tools.ops.run import OpsStatus

    class _Proc:
        def __init__(self, out): self._out = out
        async def communicate(self): return (self._out, b"")
    async def fake_exec(*argv, **kw):
        svc = argv[-1]
        return _Proc(b"inactive\n" if svc == "llama-brain2" else b"active\n")
    monkeypatch.setattr(M.asyncio, "create_subprocess_exec", fake_exec)

    class _Resp:
        def __init__(self, code): self.status_code = code
    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url):
            if "8091" in url: raise RuntimeError("connection refused")
            return _Resp(401 if "4000" in url else 200)
    monkeypatch.setattr(M.httpx, "AsyncClient", lambda *a, **k: _Client())

    cfg = {"tools": {"ops": {"status": {
        "services": ["litellm-proxy", "llama-brain1", "llama-brain2"],
        "pings": {"litellm": "http://127.0.0.1:4000/v1/models",
                  "brain1": "http://127.0.0.1:8090/health",
                  "brain2": "http://127.0.0.1:8091/health"}}}}}
    r = asyncio.run(OpsStatus().execute({}, ToolContext(request_id="t", config=cfg, budget=None)))
    assert r.status == "ok"
    assert r.result["services"]["litellm-proxy"] == "active"
    assert r.result["services"]["llama-brain2"] == "inactive"
    assert r.result["endpoints"]["litellm"]["up"] is True     # 401 = reachable = up
    assert r.result["endpoints"]["brain2"]["up"] is False      # refused
    assert r.result["all_up"] is False

def test_status_no_confirmation_needed():
    from tools.ops.run import OpsStatus
    assert getattr(OpsStatus(), "requires_confirmation", False) is False
