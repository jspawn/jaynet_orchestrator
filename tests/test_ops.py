"""ops.run guardrails: allowlist, no-shell metachar rejection, loopback-only, exec."""
import asyncio
from tools.ops.run import OpsRun, _validate
from runtime.tool_base import ToolContext

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
