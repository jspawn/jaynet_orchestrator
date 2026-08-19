"""Audit B15 (secret-bearing files don't depend on the launcher's umask) and
B16 (login-throttle maps are bounded)."""
import os
import stat

from web.auth import LoginThrottle, UserStore, resolve_secret


def test_session_secret_created_0600(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCH_SESSION_SECRET", raising=False)
    monkeypatch.delenv("JAYNET_SESSION_SECRET", raising=False)
    secret = resolve_secret(tmp_path)
    mode = stat.S_IMODE(os.stat(tmp_path / "session.secret").st_mode)
    assert mode == 0o600
    assert (tmp_path / "session.secret").read_text() == secret
    # Second call reads the persisted secret, doesn't rotate it.
    assert resolve_secret(tmp_path) == secret


def test_users_db_private_perms(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_ADMIN_USER", "admin")
    monkeypatch.setenv("ORCH_ADMIN_PASSWORD", "pw")
    db = tmp_path / "users.db"
    UserStore(str(db))
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path).st_mode) & 0o077 == 0   # dir 0700


def test_login_throttle_maps_bounded():
    t = LoginThrottle(max_keys=100)
    for i in range(5000):          # unique-username spray
        t.record_failure(f"user{i}")
    assert len(t._fails) <= 100
    for i in range(5000):
        for _ in range(5):         # drive names into the lock map
            t.record_failure(f"lock{i}")
    assert len(t._locked) <= 100


def test_login_throttle_sweeps_expired():
    t = LoginThrottle(window=0, lock_s=0)
    t.record_failure("stale")      # window=0 -> instantly expired
    t.record_failure("fresh")
    assert "stale" not in t._fails and "fresh" in t._fails
