"""Model-turn body construction: chat_template_kwargs is llama.cpp-only.

Anthropic (and other cloud providers) reject that param — "Extra inputs are not
permitted" — so it must be sent to local models only. These lock the gating.
"""
from runtime.loop import _is_local_model, _turn_body

MSGS = [{"role": "user", "content": "hi"}]
TOOLS = [{"type": "function", "function": {"name": "x"}}]


def test_is_local_model():
    assert _is_local_model("local-orchestrator")
    assert _is_local_model("local-coder")
    assert not _is_local_model("claude-haiku")
    assert not _is_local_model("qwen-max")
    assert not _is_local_model("gemini-pro")
    assert not _is_local_model(None) and not _is_local_model("")


def test_local_gets_chat_template_kwargs():
    b = _turn_body("local-orchestrator", MSGS, TOOLS, None, think=False, stream=False)
    assert b["chat_template_kwargs"] == {"enable_thinking": False}
    assert b["model"] == "local-orchestrator" and b["tool_choice"] == "auto"


def test_cloud_omits_chat_template_kwargs():
    # the exact case from the incident: a sub-agent on claude-haiku
    b = _turn_body("claude-haiku", MSGS, TOOLS, None, think=True, stream=False)
    assert "chat_template_kwargs" not in b
    for m in ("qwen-max", "gemini-pro", "claude-sonnet"):
        assert "chat_template_kwargs" not in _turn_body(m, MSGS, TOOLS, None, True, False)


def test_streaming_adds_stream_keys_but_still_gates_template():
    local = _turn_body("local-orchestrator", MSGS, TOOLS, None, True, stream=True)
    assert local["stream"] is True and local["stream_options"] == {"include_usage": True}
    assert "chat_template_kwargs" in local
    cloud = _turn_body("claude-haiku", MSGS, TOOLS, None, True, stream=True)
    assert cloud["stream"] is True and "chat_template_kwargs" not in cloud


def test_sampler_params_merge():
    b = _turn_body("local-orchestrator", MSGS, TOOLS, {"temperature": 0.5}, True, False)
    assert b["temperature"] == 0.5
