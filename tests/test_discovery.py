"""Every new tool must be discovered by the registry under its expected name."""
from runtime.registry import ToolRegistry

NEW_TOOLS = {
    "code.run", "code.patch", "code.symbols", "code.tree", "code.deps",
    "lint.run",
    "git.fetch", "git.pull", "git.push", "git.stash", "git.restore", "git.worktree",
    "trace.query", "specialist.delegate",
    "research.start", "research.next", "research.seen", "research.add", "research.note", "research.report",
    "browser.screenshot", "browser.pdf",
}


def test_new_tools_discovered():
    reg = ToolRegistry("tools")
    reg.discover()
    names = {t.name for t in reg.all()}
    missing = NEW_TOOLS - names
    assert not missing, f"not discovered: {missing}"


def test_new_tools_have_descriptions_and_schema():
    reg = ToolRegistry("tools")
    reg.discover()
    for name in NEW_TOOLS:
        t = reg.get(name)
        assert t.description and len(t.description) > 20, f"{name} weak description"
        assert t.parameters.get("type") == "object", f"{name} bad schema"


def test_code_delegate_legacy_alias():
    """code.delegate is the hidden legacy alias of specialist.delegate:
    registered under the old name, hidden from schemas, same behavior class."""
    from tools.specialist.delegate import SpecialistDelegate
    reg = ToolRegistry("tools")
    reg.discover()
    canonical = reg.get("specialist.delegate")
    alias = reg.get("code.delegate")
    assert canonical is not None and type(canonical) is SpecialistDelegate
    assert alias is not None, "legacy alias code.delegate not registered"
    assert alias.hidden, "legacy alias must stay hidden from tool schemas"
    assert isinstance(alias, SpecialistDelegate)
    assert alias.parameters == canonical.parameters


def test_selector_substitutes_hidden_alias_for_canonical():
    """A hidden legacy alias in the selection resolves to its canonical twin
    BEFORE the tool cap — the model-facing vocabulary is the canonical name
    (live: the cap kept code.delegate, cut specialist.delegate, and a prompt
    saying 'use specialist.delegate' had no such tool — the brain
    agent.spawn'd onto the right model through the wrong lane instead)."""
    from runtime.selector import ToolSelector
    reg = ToolRegistry("tools")
    reg.discover()
    sel = ToolSelector(reg, {})
    out = sel.select("anything", requested=["code.delegate"])
    assert out == ["specialist.delegate"]
    # both requested → the canonical once, no duplicate schema
    out2 = sel.select("anything",
                      requested=["code.delegate", "specialist.delegate"])
    assert out2 == ["specialist.delegate"]


def test_mutating_tools_are_gated():
    reg = ToolRegistry("tools")
    reg.discover()
    for name in ("git.push", "git.pull", "git.stash", "git.restore",
                 "git.worktree", "code.patch", "code.deps", "git.fetch"):
        assert reg.get(name).requires_confirmation, f"{name} should require confirmation"
    # Fast-loop read/run tools must NOT be gated. (trace.query's gate is
    # conditional — needs_confirmation on all_owners=true, audit B12 — so its
    # static flag stays off; git.fetch moved into the gated set in B10: it is
    # network egress to the configured remote, like pull/push.)
    for name in ("code.run", "lint.run", "code.symbols", "code.tree",
                 "trace.query"):
        assert not reg.get(name).requires_confirmation, f"{name} should not be gated"


def test_category_aliases_resolve_against_real_registry():
    """Every namespace named in CATEGORY_ALIASES must expand to ≥1 real tool —
    this is the vocabulary the gate prompt and tools.load advertise."""
    from runtime.selector import CATEGORY_ALIASES
    reg = ToolRegistry("tools")
    reg.discover()
    namespaces = {t.name.split(".", 1)[0] for t in reg.all()} | {t.name for t in reg.all()}
    for cat, targets in CATEGORY_ALIASES.items():
        assert targets, f"{cat}: empty alias"
        for t in targets:
            assert t in namespaces, f"{cat}: '{t}' matches no tool namespace"
