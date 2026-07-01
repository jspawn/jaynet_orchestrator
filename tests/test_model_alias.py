"""Model alias resolution — shared by llm.call and agent.spawn.

The brain often reuses llm.call's short names ('haiku', 'opus') when spawning a
sub-agent; both paths now resolve through resolve_model_alias so a short name no
longer 400s at LiteLLM. Both friendly aliases and raw litellm.yaml aliases work.
"""
from tools.llm.cloud_models import resolve_model_alias, valid_model_names


def test_friendly_aliases_map_to_litellm_names():
    assert resolve_model_alias("haiku") == "claude-haiku"
    assert resolve_model_alias("opus") == "claude-opus"
    assert resolve_model_alias("claude") == "claude-sonnet"
    assert resolve_model_alias("qwen_coder") == "qwen-coder"
    assert resolve_model_alias("gemini_flash") == "gemini-flash"


def test_raw_litellm_aliases_pass_through():
    for a in ("claude-haiku", "claude-sonnet", "claude-opus", "qwen-max",
              "qwen-plus", "qwen-flash", "qwen-coder", "gemini-pro",
              "gemini-flash", "local-orchestrator", "local-coder"):
        assert resolve_model_alias(a) == a


def test_case_and_separator_tolerant():
    assert resolve_model_alias("HAIKU") == "claude-haiku"
    assert resolve_model_alias("Claude-Haiku") == "claude-haiku"
    assert resolve_model_alias("qwen-coder") == "qwen-coder"
    assert resolve_model_alias("qwen_coder") == "qwen-coder"


def test_unknown_and_empty_return_none():
    for bad in ("gpt-4", "banana", "", None):
        assert resolve_model_alias(bad) is None


def test_valid_names_includes_both_forms():
    names = valid_model_names()
    assert "haiku" in names and "claude-haiku" in names        # friendly + raw
    assert "local-coder" in names
    # exactly the friendly keys + litellm aliases, no dupes
    assert len(names) == len(set(names))
