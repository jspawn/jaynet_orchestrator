"""Mediated sub-LLM calls (runtime/subcall.py + the code.execute seam).

Policy tests drive SubcallServer._serve directly with a fake runtime; one
end-to-end test runs a REAL unix-socket server and a REAL snippet subprocess
(code.execute, sandbox forced off) to prove the whole path: grant env vars,
injected llm_query preamble, socket roundtrip, budget billing, usage report.
"""
import asyncio
import sys

import tools.code.execute as EX
from runtime.budget import Budget
from runtime.subcall import SubcallServer
from runtime.tool_base import ToolContext
from tools.code.execute import CodeExecute


class _FakeRuntime:
    cost_table = {"local-brain": {"input": 1.0, "output": 2.0}}

    def __init__(self, text="PONG"):
        self.calls = []
        self._text = text

    async def _model_turn(self, messages, tools, model=None, think=True,
                          sampling=None):
        self.calls.append({"messages": messages, "tools": tools,
                           "model": model, "think": think, "sampling": sampling})
        return {"message": {"role": "assistant", "content": self._text},
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def _cfg(**sc):
    return {"tools": {"code": {"subcalls": sc}},
            "orchestrator": {"local_concurrency": {"local-brain": 2}}}


def _server(monkeypatch, tmp_path, rt=None, *, tainted=False, cfg=None,
            default_model="local-brain"):
    """A SubcallServer with recorded events; socket dir redirected to tmp."""
    monkeypatch.setattr("runtime.subcall.SANDBOX_DIR", tmp_path)
    events = []

    async def emit(t, i, d):
        events.append((t, d))

    async def emit_cost(model, delta):
        events.append(("cost", {"model": model, "delta": delta}))

    s = SubcallServer(rt or _FakeRuntime(), run_id="run-1234", config=cfg or _cfg(),
                      default_model=default_model,
                      tainted=lambda: tainted,
                      budget=Budget(max_iterations=0, max_wall_clock_s=0,
                                    max_cost_usd=0, max_total_tokens=0),
                      emit=emit, emit_cost=emit_cost)
    return s, events


def _serve(s, grant, **req):
    req.setdefault("token", grant["token"])
    req.setdefault("prompt", "ping")
    return asyncio.run(s._serve(req))


# ------------------------------------------------------------------ policy ---

def test_unknown_token_refused(monkeypatch, tmp_path):
    s, _ = _server(monkeypatch, tmp_path)
    r = _serve(s, {"token": "nope"})
    assert r["status"] == "error" and "token" in r["error"]


def test_call_cap_enforced(monkeypatch, tmp_path):
    s, _ = _server(monkeypatch, tmp_path, cfg=_cfg(max_calls=2))
    grant = s.mint_grant()
    assert _serve(s, grant)["status"] == "ok"
    assert _serve(s, grant)["status"] == "ok"
    r = _serve(s, grant)
    assert r["status"] == "error" and "cap reached" in r["error"]
    assert grant["used"] == 2


def test_success_bills_budget_and_traces(monkeypatch, tmp_path):
    rt = _FakeRuntime(text="<think>hidden</think>the answer")
    s, events = _server(monkeypatch, tmp_path, rt=rt)
    grant = s.mint_grant()
    r = _serve(s, grant, prompt="slice 1 of N", system="be terse",
               max_tokens=99999)
    assert r["status"] == "ok"
    assert r["text"] == "the answer"               # think stripped
    assert r["calls_remaining"] == grant["max_calls"] - 1
    call = rt.calls[0]
    assert call["model"] == "local-brain"          # default = run's model
    assert call["think"] is False and call["tools"] == []
    assert call["sampling"] == {"max_tokens": 4096}  # capped at max_output_tokens
    assert call["messages"][0] == {"role": "system", "content": "be terse"}
    assert s.budget.tokens_prompt == 10 and s.budget.tokens_completion == 5
    assert s.budget.cost_usd > 0                   # cost_table applied
    types = [t for t, _ in events]
    assert "subcall" in types and "progress" in types and "cost" in types


def test_nondefault_cloud_model_refused(monkeypatch, tmp_path):
    rt = _FakeRuntime()
    s, _ = _server(monkeypatch, tmp_path, rt=rt)
    grant = s.mint_grant()
    r = _serve(s, grant, model="kimi-k3")
    assert r["status"] == "error" and "not allowed" in r["error"]
    assert rt.calls == []                          # refused before any call
    assert grant["used"] == 0                      # and before billing


def test_tainted_run_is_local_only(monkeypatch, tmp_path):
    # Cloud default brain + taint -> hard privacy refusal, no confirm path.
    s, _ = _server(monkeypatch, tmp_path, tainted=True, default_model="kimi-k3")
    grant = s.mint_grant()
    r = _serve(s, grant)
    assert r["status"] == "error" and "privacy" in r["error"]
    # ...but a local alias is fine even when tainted.
    r = _serve(s, grant, model="local-brain")
    assert r["status"] == "ok"


def test_prompt_validation(monkeypatch, tmp_path):
    s, _ = _server(monkeypatch, tmp_path, cfg=_cfg(max_prompt_chars=10))
    grant = s.mint_grant()
    assert "non-empty" in _serve(s, grant, prompt="  ")["error"]
    assert "too large" in _serve(s, grant, prompt="x" * 11)["error"]


def test_exhausted_budget_refuses(monkeypatch, tmp_path):
    s, _ = _server(monkeypatch, tmp_path)
    s.budget.max_total_tokens = 1
    s.budget.tokens_completion = 5                 # already over the ceiling
    grant = s.mint_grant()
    r = _serve(s, grant)
    assert r["status"] == "error" and "budget exhausted" in r["error"]
    assert grant["used"] == 0


# ------------------------------------------------------- code.execute seam ---

def test_build_cmd_whitelists_subcall_socket(monkeypatch, tmp_path):
    monkeypatch.setattr(EX.shutil, "which", lambda name: "/usr/bin/firejail")
    cmd = CodeExecute()._build_cmd("s.py", "firejail", tmp_path, None,
                                   "python", 1024, subcall_sock="/x/y.sock")
    assert "--read-write=/x/y.sock" in cmd
    assert "--net=none" in cmd                     # unix socket, network stays off
    cmd = CodeExecute()._build_cmd("s.py", "firejail", tmp_path, None,
                                   "python", 1024)
    assert not any("y.sock" in c for c in cmd)


def test_end_to_end_snippet_llm_query(monkeypatch, tmp_path):
    """Real server + real snippet subprocess: the full RLM roundtrip."""
    monkeypatch.setattr(EX.shutil, "which", lambda name: None)   # no firejail
    monkeypatch.setattr("runtime.subcall.SANDBOX_DIR", tmp_path)
    rt = _FakeRuntime(text="chunk-answer")

    async def main():
        events = []

        async def emit(t, i, d):
            events.append(t)

        async def emit_cost(model, delta):
            pass

        server = SubcallServer(
            rt, run_id="run-e2e", config=_cfg(), default_model="local-brain",
            tainted=lambda: False,
            budget=Budget(max_iterations=0, max_wall_clock_s=0,
                          max_cost_usd=0, max_total_tokens=0),
            emit=emit, emit_cost=emit_cost)
        await server.start()

        async def grant_fn(_limits):
            return server.mint_grant()

        ctx = ToolContext(
            request_id="t", budget=None,
            config={"tools": {"code": {"workdir": str(tmp_path / "work"),
                                       "python": sys.executable}}})
        ctx.subcall_grant = grant_fn
        try:
            return await CodeExecute().execute({"code": (
                "answers = llm_query_batched(['slice A', 'slice B'], workers=2)\n"
                "print('|'.join(answers))\n"
                "print(llm_query('slice C'))"
            )}, ctx), server, events
        finally:
            await server.close()

    r, server, events = asyncio.run(main())
    assert r.status == "ok", r.error
    assert "chunk-answer|chunk-answer" in r.result["stdout"]
    assert r.result["stdout"].count("chunk-answer") == 3
    assert r.result["subcalls"] == {"used": 3, "max": 64}
    assert len(rt.calls) == 3
    assert events.count("subcall") == 3
    # socket file cleaned up on close
    assert not list(tmp_path.glob("subcall-*.sock"))


def test_no_grant_no_helpers(monkeypatch, tmp_path):
    """Without the seam the snippet runs plain — no env vars, no preamble."""
    monkeypatch.setattr(EX.shutil, "which", lambda name: None)
    calls = []

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"ok\n", b""

    async def fake_exec(*cmd, **kw):
        calls.append({"cmd": list(cmd), "env": kw.get("env") or {},
                      "script": open(cmd[-1]).read()})
        return _Proc()

    monkeypatch.setattr(EX.asyncio, "create_subprocess_exec", fake_exec)
    ctx = ToolContext(request_id="t", budget=None,
                      config={"tools": {"code": {"workdir": str(tmp_path)}}})
    r = asyncio.run(CodeExecute().execute({"code": "print('ok')"}, ctx))
    assert r.status == "ok"
    assert "subcalls" not in r.result
    assert "ORCH_SUBCALL_SOCK" not in calls[0]["env"]
    assert "llm_query" not in calls[0]["script"]
