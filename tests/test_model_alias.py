"""Model alias resolution — shared by llm.call and agent.spawn.

The brain may pass either a friendly short name ('glm', 'gemini', 'qwen') or a
raw litellm.yaml alias ('glm-5.2', 'local-coder'); both resolve through
resolve_model_alias so a short name never 400s at LiteLLM.
"""
from tools.llm.cloud_models import resolve_model_alias, valid_model_names


def test_friendly_aliases_map_to_litellm_names():
    assert resolve_model_alias("glm") == "glm-5.2"
    assert resolve_model_alias("gemini") == "gemini-pro"
    assert resolve_model_alias("qwen") == "qwen-plus"


def test_raw_litellm_aliases_pass_through():
    for a in ("glm-5.2", "gemini-pro", "qwen-plus",
              "local-orchestrator", "local-coder"):
        assert resolve_model_alias(a) == a


def test_case_and_separator_tolerant():
    assert resolve_model_alias("GLM") == "glm-5.2"
    assert resolve_model_alias("Gemini-Pro") == "gemini-pro"
    assert resolve_model_alias("qwen_plus") == "qwen-plus"
    assert resolve_model_alias("LOCAL_CODER") == "local-coder"


def test_unknown_and_empty_return_none():
    for bad in ("gpt-4", "banana", "haiku", "qwen-coder", "", None):
        assert resolve_model_alias(bad) is None


def test_valid_names_includes_both_forms():
    names = valid_model_names()
    assert "glm" in names and "glm-5.2" in names        # friendly + raw
    assert "local-coder" in names
    # exactly the friendly keys + litellm aliases, no dupes
    assert len(names) == len(set(names))
