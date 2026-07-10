"""boot_posture: serve configured presets via model.use at startup; never fatal."""
import asyncio
import runtime.boot_posture as B
from runtime.boot_posture import apply_boot_posture

class _Res:
    def __init__(self, status, result=None, error=None): self.status=status; self.result=result; self.error=error
class _RT:
    def __init__(self, cfg): self.config=cfg

def test_serves_configured_presets(monkeypatch):
    calls=[]
    class _Fake:
        async def execute(self, args, ctx): calls.append(args["preset"]); return _Res("ok", {"status":"loaded"})
    monkeypatch.setattr(B, "ModelUse", _Fake)
    rep=asyncio.run(apply_boot_posture(_RT({"models":{"boot":["brain2"]}}), initial_delay=0))
    assert calls==["brain2"] and rep[0]["ok"] and rep[0]["status"]=="loaded"

def test_noop_when_unconfigured(monkeypatch):
    calls=[]
    class _Fake:
        async def execute(self, args, ctx): calls.append(args["preset"]); return _Res("ok",{})
    monkeypatch.setattr(B, "ModelUse", _Fake)
    assert asyncio.run(apply_boot_posture(_RT({"models":{}}), initial_delay=0))==[]
    assert calls==[]

def test_survives_model_use_failure(monkeypatch):
    class _Boom:
        async def execute(self, args, ctx): raise RuntimeError("nope")
    monkeypatch.setattr(B, "ModelUse", _Boom)
    rep=asyncio.run(apply_boot_posture(_RT({"models":{"boot":["brain2"]}}), initial_delay=0))
    assert rep[0]["ok"] is False and "nope" in rep[0]["error"]   # logged, not raised

def test_reports_tool_error(monkeypatch):
    class _Err:
        async def execute(self, args, ctx): return _Res("error", error="no room")
    monkeypatch.setattr(B, "ModelUse", _Err)
    rep=asyncio.run(apply_boot_posture(_RT({"models":{"boot":["coder"]}}), initial_delay=0))
    assert rep[0]["ok"] is False and rep[0]["error"]=="no room"
