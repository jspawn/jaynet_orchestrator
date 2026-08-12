"""runtime/env.py — the JAYNET_/ORCH_ dual read."""

import os

from runtime.env import env, load_env_file


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


# ---- load_env_file -----------------------------------------------------------


def test_load_env_file_sets_missing(tmp_path):
    f = tmp_path / "jaynet.env"
    f.write_text("# comment\n\nJAYNET_HOME=/opt/jaynet\nQUOTED=\"/with space\"\n"
                 "SINGLE='/sq'\nBADLINE\n")
    # load_env_file writes os.environ directly — save/restore by hand,
    # monkeypatch can't track those writes and a leak poisons later tests.
    keys = ("JAYNET_HOME", "QUOTED", "SINGLE")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        assert load_env_file(f) == f
        assert os.environ["JAYNET_HOME"] == "/opt/jaynet"
        assert os.environ["QUOTED"] == "/with space"
        assert os.environ["SINGLE"] == "/sq"
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_load_env_file_real_env_wins(monkeypatch, tmp_path):
    f = tmp_path / "jaynet.env"
    f.write_text("JAYNET_DATA=/from-file\n")
    monkeypatch.setenv("JAYNET_DATA", "/from-env")
    load_env_file(f)
    assert os.environ["JAYNET_DATA"] == "/from-env"


def test_load_env_file_missing_returns_none(tmp_path):
    assert load_env_file(tmp_path / "nope.env") is None
