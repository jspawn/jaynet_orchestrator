"""run.badge — label sanitising and the UI event it rides on."""

import asyncio

from tools.agent.badge import RunBadge


def _collect():
    events = []

    async def emit(t, d):
        events.append((t, d))

    return events, emit


def test_badge_emits_sanitised_label(ctx):
    events, emit = _collect()
    res = asyncio.run(RunBadge().execute({"label": "  j-space:   loop\nnow" },
                                         ctx(emit=emit)))
    assert res.status == "ok"
    assert events == [("badge", {"label": "j-space: loop now"})]


def test_badge_caps_at_40_and_clears(ctx):
    events, emit = _collect()
    res = asyncio.run(RunBadge().execute({"label": "x" * 80}, ctx(emit=emit)))
    assert res.status == "ok"
    assert len(res.result["label"]) == 40
    asyncio.run(RunBadge().execute({"label": ""}, ctx(emit=emit)))
    assert events[-1] == ("badge", {"label": ""})


def test_badge_without_ui_reports_not_displayed(ctx):
    res = asyncio.run(RunBadge().execute({"label": "j-space: full"}, ctx()))
    assert res.status == "ok"
    assert res.result == {"label": "j-space: full", "displayed": False}


def test_j_space_skill_discoverable():
    from pathlib import Path

    from runtime.skills import load_skill
    repo = Path(__file__).resolve().parent.parent
    payload = load_skill(str(repo / "skills"), "j-space",
                         custom_dir="/nonexistent")
    assert payload is not None
    assert payload["name"] == "j-space"
    files = payload.get("files") or {}
    assert any(p.endswith("modules/capacity.md") for p in files)
    assert any(p.endswith("scripts/workspace-ledger.md") for p in files)
