"""Working-anchor placement is config-gated and never breaks the message shape.
Default 'off' restores the plain transcript (the known-good behavior)."""
from runtime.loop import AgentRuntime

MSGS = [{"role": "system", "content": "SYS"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "name": "t", "content": "R"}]
ANCHOR = {"role": "system", "content": "ANCHOR"}


def test_off_returns_transcript_unchanged():
    out = AgentRuntime._apply_anchor(MSGS, ANCHOR, "off")
    assert out is MSGS                                  # untouched, plain transcript
    assert AgentRuntime._apply_anchor(MSGS, None, "system") is MSGS


def test_system_folds_into_position_0_only():
    out = AgentRuntime._apply_anchor(MSGS, ANCHOR, "system")
    assert len(out) == len(MSGS)                        # no new message
    assert out[0]["role"] == "system" and "SYS" in out[0]["content"] and "ANCHOR" in out[0]["content"]
    assert out[-1]["role"] == "tool"                    # last message unchanged
    assert MSGS[0]["content"] == "SYS"                  # original not mutated


def test_trailing_appends_a_system_message():
    out = AgentRuntime._apply_anchor(MSGS, ANCHOR, "trailing")
    assert len(out) == len(MSGS) + 1
    assert out[-1] == ANCHOR and out[:-1] == MSGS
