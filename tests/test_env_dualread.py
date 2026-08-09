"""runtime/env.py — the JAYNET_/ORCH_ dual read."""

from runtime.env import env


def test_new_prefix_wins(monkeypatch):
    monkeypatch.setenv("JAYNET_HOME", "/new")
    monkeypatch.setenv("ORCH_HOME", "/old")
    assert env("ORCH_HOME") == "/new"


def test_legacy_prefix_fallback(monkeypatch):
    monkeypatch.delenv("JAYNET_DATA", raising=False)
    monkeypatch.setenv("ORCH_DATA", "/old-data")
    assert env("ORCH_DATA") == "/old-data"


def test_default_when_neither(monkeypatch):
    monkeypatch.delenv("JAYNET_MODELS", raising=False)
    monkeypatch.delenv("ORCH_MODELS", raising=False)
    assert env("ORCH_MODELS") is None
    assert env("ORCH_MODELS", "/d") == "/d"


def test_bare_name_accepted(monkeypatch):
    monkeypatch.setenv("JAYNET_WEB_PORT", "9999")
    assert env("WEB_PORT") == "9999"
    assert env("ORCH_WEB_PORT") == "9999"
