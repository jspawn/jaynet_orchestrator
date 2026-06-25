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
