"""Deterministic routing nudge (AgentRuntime._routing_nudge).

The standing Route-don't-do doctrine in the gate prompt is not enough for
small brains (eval runs kept showing coding tasks done inline, never
delegated). The nudge adds a just-in-time system note right before the user
turn when the request smells like routed work — keyword-driven, no LLM call.
"""
import pytest
import yaml

from runtime.loop import AgentRuntime


def _rt(tmp_path, extra_cfg=None):
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "sys.md").write_text("SYS")
    cfg = {
        "orchestrator": {"litellm_base": "http://127.0.0.1:1",
                         "model": "local-orchestrator",
                         "system_prompt": "prompts/sys.md"},
        "trace": {"db_path": str(tmp_path / "trace.db"), "log_content": False},
        "costs": {},
        "budgets": {},
    }
    if extra_cfg:
        cfg.update(extra_cfg)
    cdir = tmp_path / "config"
    cdir.mkdir(exist_ok=True)
    (cdir / "runtime.yaml").write_text(yaml.safe_dump(cfg))
    return AgentRuntime(cdir / "runtime.yaml")


@pytest.mark.asyncio
async def test_coding_request_nudges_delegate(tmp_path):
    rt = _rt(tmp_path)
    note = await rt._routing_nudge("Please implement a retry parser and fix this code.")
    assert note is not None
    assert "code.delegate" in note
    assert "Do NOT" in note  # the no-inline-implementation clause


@pytest.mark.asyncio
async def test_plain_question_gets_no_nudge(tmp_path):
    rt = _rt(tmp_path)
    assert await rt._routing_nudge("what is the capital of France?") is None
    # Tool-loading keywords that are NOT nudge keywords must stay silent:
    # "run" loads the code namespace but must not talk the brain into
    # delegating a non-coding request.
    assert await rt._routing_nudge("run a web search for today's news") is None


@pytest.mark.asyncio
async def test_security_tag_with_preset_not_live(tmp_path):
    """A tagged preset exists but nothing is live → the note names the
    preset to bring in via model.use, rather than pretending a route exists."""
    rt = _rt(tmp_path, {
        "models": {
            "presets": {
                "dolphin": {
                    "alias": "local-dolphin",
                    "port": 1,          # nothing answers here → not live
                    "strengths": ["security"],
                },
            },
        },
    })
    note = await rt._routing_nudge("check this app for sql injection vulnerabilities")
    assert note is not None
    assert "security" in note
    assert "model.use" in note
    assert "dolphin" in note


@pytest.mark.asyncio
async def test_security_tag_without_preset_stays_silent(tmp_path):
    rt = _rt(tmp_path)  # no presets at all → no honest route to offer
    assert await rt._routing_nudge("check this for exploits") is None


@pytest.mark.asyncio
async def test_nudge_can_be_disabled(tmp_path):
    rt = _rt(tmp_path, {"tool_selection": {"routing_nudge": {"enabled": False}}})
    assert await rt._routing_nudge("implement a function") is None


@pytest.mark.asyncio
async def test_short_acronyms_need_word_boundaries(tmp_path):
    """'source code' contains the substring 'rce' — without word boundaries
    every coding question would get a spurious security nudge."""
    rt = _rt(tmp_path, {
        "models": {"presets": {"dolphin": {
            "alias": "local-dolphin", "port": 1, "strengths": ["security"]}}},
    })
    note = await rt._routing_nudge("review this source code for style")
    assert note is None or "security" not in note
    # a real acronym mention still fires
    note = await rt._routing_nudge("is this endpoint open to RCE?")
    assert note is not None and "security" in note


@pytest.mark.asyncio
async def test_intrusion_keywords_route_security(tmp_path):
    """tb-intrusion-detection style phrasing must hit the security tag."""
    rt = _rt(tmp_path, {
        "models": {"presets": {"dolphin": {
            "alias": "local-dolphin", "port": 1, "strengths": ["security"]}}},
    })
    note = await rt._routing_nudge(
        "Create an intrusion detection system for security threats in logs.")
    assert note is not None and "security" in note


@pytest.mark.asyncio
async def test_shipped_config_keywords_cover_the_fallback(tmp_path):
    """Drift guard (audit #12 D2): runtime.yaml's routing_nudge lists are a
    pure OVERRIDE of the code fallback — if they lag it, live installs
    silently lose nudges the tests demonstrate. The shipped lists must
    trigger on the fallback's canonical phrasings."""
    from pathlib import Path

    import yaml as _yaml

    shipped = _yaml.safe_load((Path(__file__).resolve().parent.parent
                               / "config" / "runtime.yaml")
                              .read_text(encoding="utf-8"))
    routing = (shipped.get("tool_selection") or {}).get("routing_nudge") or {}
    rt = _rt(tmp_path, {
        "tool_selection": {"routing_nudge": routing},
        "models": {"presets": {"dolphin": {
            "alias": "local-dolphin", "port": 1, "strengths": ["security"]}}},
    })
    # fallback-only code keyword
    note = await rt._routing_nudge("write a small shell script that pings the NAS")
    assert note is not None and "code.delegate" in note
    # fallback-only security stems
    note = await rt._routing_nudge(
        "Create an intrusion detection system for security threats.")
    assert note is not None and "security" in note
    note = await rt._routing_nudge("walk me through the incident response plan")
    assert note is not None and "security" in note
