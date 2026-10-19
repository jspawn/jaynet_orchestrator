"""Bash-only control arms (docs/harness-lessons.md #1): a benchmark variant
can restrict the run to an exact tool list (`tools_only` — the
mini-SWE-agent-style control measuring what the JayNet toolset adds over a
bare bash agent) and swap the whole system prompt (`base_system` —
prompts/<name>.md, so the control doesn't see the gate prompt's routing
doctrine for tools it doesn't have). Unknown tool/prompt names fail loudly
at case load, never mid-run — the same contract as guards_off."""
import pytest
from conftest import run

from runtime import eval_runner
from runtime.eval_store import EvalStore
from runtime.paths import HOME
from tests.test_eval_harness import _case, _FakeRuntime, _judge_ok


def test_tools_only_restricts_toolset(tmp_path, monkeypatch):
    """tools_only intersects the unattended-safe set: the fake registry's
    unattended tools are fs.read+web.search; pinning ["web.search"] hands
    exactly that to runtime.run. No variant = the full unattended set."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["ok"])
    store = EvalStore(tmp_path / "eval.db")
    run(eval_runner.run_case(rt, _case(), store,
                             variant={"label": "pin",
                                      "tools_only": ["web.search"]}))
    assert rt.calls[0][1]["tools"] == ["web.search"]
    store.close()
    rt2 = _FakeRuntime(["ok"])
    store2 = EvalStore(tmp_path / "eval2.db")
    run(eval_runner.run_case(rt2, _case(), store2, variant={"label": "full"}))
    assert set(rt2.calls[0][1]["tools"]) == {"fs.read", "web.search"}
    store2.close()


def test_tools_only_empty_list_means_no_restriction(tmp_path, monkeypatch):
    """An empty list normalizes to None — never 'zero tools'."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["ok"])
    store = EvalStore(tmp_path / "eval.db")
    run(eval_runner.run_case(rt, _case(), store,
                             variant={"label": "v", "tools_only": []}))
    assert set(rt.calls[0][1]["tools"]) == {"fs.read", "web.search"}
    store.close()


def test_tools_only_unknown_fails_at_case_load(tmp_path, monkeypatch):
    """A typo'd tool name raises ValueError before the first model call —
    silently running with the full toolset would fake the control."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["ok"])
    store = EvalStore(tmp_path / "eval.db")
    with pytest.raises(ValueError, match="unknown tool"):
        run(eval_runner.run_case(rt, _case(), store,
                                 variant={"label": "v",
                                          "tools_only": ["web.seach"]}))
    assert not rt.calls
    store.close()


def test_base_system_swaps_system_prompt(tmp_path, monkeypatch):
    """prompts/eval-bash-agent.md ships the bash-only control prompt; the
    variant hands its TEXT to runtime.run as base_system (the loop's
    full-prompt replacement seam)."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["ok"])
    store = EvalStore(tmp_path / "eval.db")
    run(eval_runner.run_case(rt, _case(), store,
                             variant={"label": "bash",
                                      "base_system": "eval-bash-agent"}))
    sent = rt.calls[0][1]["base_system"]
    assert sent == (HOME / "prompts"
                    / "eval-bash-agent.md").read_text(encoding="utf-8")
    assert "code.run" in sent
    store.close()
    # default: no swap → None (the loop's normal prompt assembly)
    rt2 = _FakeRuntime(["ok"])
    store2 = EvalStore(tmp_path / "eval2.db")
    run(eval_runner.run_case(rt2, _case(), store2, variant={"label": "v"}))
    assert rt2.calls[0][1]["base_system"] is None
    store2.close()


def test_base_system_bad_name_and_missing_file_fail(tmp_path, monkeypatch):
    """Path-traversal-ish names die on the charset, unknown names on the
    missing prompts/<name>.md — both loudly, before any model call."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["ok"])
    store = EvalStore(tmp_path / "eval.db")
    with pytest.raises(ValueError, match="letters, digits"):
        run(eval_runner.run_case(rt, _case(), store,
                                 variant={"label": "v",
                                          "base_system": "../loop"}))
    with pytest.raises(ValueError, match="not found"):
        run(eval_runner.run_case(rt, _case(), store,
                                 variant={"label": "v",
                                          "base_system": "no-such-prompt"}))
    assert not rt.calls
    store.close()


def test_run_suite_validates_control_variant_up_front(tmp_path, monkeypatch):
    """The suite fails before spending on case 1 (run_suite's per-case
    crash handler must not turn a bad variant spec into a crash row)."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["ok"])
    store = EvalStore(tmp_path / "eval.db")
    with pytest.raises(ValueError, match="unknown tool"):
        run(eval_runner.run_suite(rt, [_case()], store,
                                  variant={"label": "v",
                                           "tools_only": ["web.seach"]}))
    assert not rt.calls
    store.close()
