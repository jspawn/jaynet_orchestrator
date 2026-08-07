"""The verifier's check command is model-influenced bash whose output goes back
into the transcript — its env must be scrubbed of orchestrator secrets, with the
same rule as code.run (runtime.tool_base.scrub_env)."""
import asyncio
from runtime.loop import AgentRuntime


class _Stub:
    _run_verify_command = AgentRuntime._run_verify_command


class _Ctx:
    # sandbox_prefix: [] → run bare bash, no firejail needed in tests.
    config = {"tools": {"code": {"run": {"sandbox_prefix": []}}}}


def test_verify_command_env_is_scrubbed(monkeypatch, tmp_path):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-should-not-leak")
    monkeypatch.setenv("MY_VERIFY_TOKEN", "also-secret")
    cmd = "echo K=${LITELLM_MASTER_KEY:-unset} T=${MY_VERIFY_TOKEN:-unset} P=${PATH:+set}"
    code, out = asyncio.run(_Stub()._run_verify_command(cmd, tmp_path, 30, _Ctx()))
    assert code == 0 and "K=unset T=unset P=set" in out


def test_verify_command_config_env_still_applies(tmp_path):
    # tools.code.run.default_env is layered on AFTER the scrub, like code.run.
    class C:
        config = {"tools": {"code": {"run": {"sandbox_prefix": [],
                                             "default_env": {"VERIFY_FLAG": "on"}}}}}
    code, out = asyncio.run(
        _Stub()._run_verify_command("echo F=$VERIFY_FLAG", tmp_path, 30, C()))
    assert code == 0 and "F=on" in out


def test_verify_refuses_bare_run_when_sandbox_missing(monkeypatch, tmp_path):
    # audit H1: no confirmation hook exists on the verifier path, so a missing
    # sandbox binary must fail closed — never run the check bare ungated.
    monkeypatch.setattr("shutil.which", lambda b: None)

    class C:
        config = {"tools": {"code": {"run": {}}}}   # default prefix -> firejail

    code, out = asyncio.run(_Stub()._run_verify_command("echo hi", tmp_path, 30, C()))
    assert code == 126 and "sandbox" in out and "firejail" in out
    # explicit sandbox_prefix: [] stays the operator opt-in to bare checks
    code2, out2 = asyncio.run(
        _Stub()._run_verify_command("echo hi", tmp_path, 30, _Ctx()))
    assert code2 == 0 and "hi" in out2
