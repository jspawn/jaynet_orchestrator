"""Every new tool must be discovered by the registry under its expected name."""
from runtime.registry import ToolRegistry

NEW_TOOLS = {
    "code.run", "code.patch", "code.symbols", "code.tree", "code.deps",
    "lint.run",
    "git.fetch", "git.pull", "git.push", "git.stash", "git.restore", "git.worktree",
    "trace.query", "code.delegate",
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


def test_mutating_tools_are_gated():
    reg = ToolRegistry("tools")
    reg.discover()
    for name in ("git.push", "git.pull", "git.stash", "git.restore",
                 "git.worktree", "code.patch", "code.deps"):
        assert reg.get(name).requires_confirmation, f"{name} should require confirmation"
    # Fast-loop read/run tools must NOT be gated.
    for name in ("code.run", "lint.run", "code.symbols", "code.tree",
                 "git.fetch", "trace.query"):
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
