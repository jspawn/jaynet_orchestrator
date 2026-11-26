"""Null-safe nested admin config updates."""
from web.routes_admin import _set_nested


def test_set_nested_replaces_null_intermediate():
    config = {"tools": None}
    _set_nested(config, "tools.mcp.servers", {"local": {"url": "http://x"}})
    assert config == {"tools": {"mcp": {"servers": {"local": {"url": "http://x"}}}}}
