"""runtime/env.py — the JAYNET_/ORCH_ dual read."""

import logging
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


def test_legacy_fallback_warns_once(monkeypatch, caplog):
    # Unique suffix: the warning is once per process per name, and other
    # tests (paths.py imports) may already have warned for the common names.
    monkeypatch.delenv("JAYNET_DEPWARN_DEMO", raising=False)
    monkeypatch.setenv("ORCH_DEPWARN_DEMO", "x")
    with caplog.at_level(logging.WARNING, logger="runtime.env"):
        assert env("ORCH_DEPWARN_DEMO") == "x"
        assert env("ORCH_DEPWARN_DEMO") == "x"   # second read stays quiet
    warns = [r.getMessage() for r in caplog.records
             if "ORCH_DEPWARN_DEMO" in r.getMessage()]
    assert len(warns) == 1
    assert "JAYNET_DEPWARN_DEMO" in warns[0]


def test_new_prefix_read_is_silent(monkeypatch, caplog):
    monkeypatch.setenv("JAYNET_DEPWARN_QUIET", "y")
    monkeypatch.setenv("ORCH_DEPWARN_QUIET", "z")
    with caplog.at_level(logging.WARNING, logger="runtime.env"):
        assert env("ORCH_DEPWARN_QUIET") == "y"
    assert not [r for r in caplog.records
                if "DEPWARN_QUIET" in r.getMessage()]


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
