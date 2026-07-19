"""The no-TTY confirmation fallback: unattended (piped/cron/script) runs must
REFUSE gated tool calls unless the operator opted in — via config
(confirmation.non_interactive: allow) or per-run auto_confirm. Interactive TTY
prompts are unaffected (they never reach the fallback)."""
import asyncio

from runtime.loop import AgentRuntime


def _rt(cfg):
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.config = cfg
    return rt


async def _emit(*a):
    pass


def _confirm(rt, auto_confirm=False):
    return asyncio.run(rt._confirm("fs.write", {}, "r", auto_confirm, _emit))


def test_no_tty_denies_by_default(monkeypatch):
    # No confirmation block at all -> safe default is deny.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _confirm(_rt({})) is False


def test_no_tty_deny_explicit(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _confirm(_rt({"confirmation": {"non_interactive": "deny"}})) is False


def test_no_tty_allow_opt_in(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _confirm(_rt({"confirmation": {"non_interactive": "allow"}})) is True


def test_no_tty_auto_confirm_bypasses(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _confirm(_rt({}), auto_confirm=True) is True


def test_confirmation_disabled_bypasses(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _confirm(_rt({"confirmation": {"enabled": False}})) is True
