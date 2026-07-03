"""Privacy is single-sourced on each tool's own `private` flag; the config
`private_tool_namespaces` is an optional additive override (default empty)."""
from runtime.registry import ToolRegistry


def _priv(reg):
    return {t.name for t in reg.all() if getattr(t, "private", False)}


def test_known_sensitive_tools_are_private_by_flag_alone():
    r = ToolRegistry("tools"); r.discover()   # class-attr only; no config loop applied
    priv = _priv(r)
    # the namespaces the OLD config listed must still be private without the loop
    for name in ["fs.read", "fs.write", "rag.search", "test.run", "agent.spawn"]:
        assert name in priv, name
    # and the flag covers more than the old config list did (e.g. trace, git, memory)
    for name in ["trace.query", "git.commit", "memory.append", "kg.upsert_entity"]:
        assert name in priv, name


def test_non_sensitive_meta_tools_are_not_private():
    r = ToolRegistry("tools"); r.discover()
    priv = _priv(r)
    for name in ["note.set", "context.pin", "ask.user", "web.search"]:
        assert name not in priv, name


def test_optional_override_can_force_a_namespace_private():
    # simulate the loop's additive override with a non-empty list
    r = ToolRegistry("tools"); r.discover()
    extra = {"web"}
    for t in r.all():
        if t.name.split(".", 1)[0] in extra:
            t.private = True
    assert "web.search" in _priv(r)   # override adds on top of the flags
