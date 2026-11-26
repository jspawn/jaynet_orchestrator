"""Per-store initialization happens once per store instance."""
import pytest

from web.auth import UserStore
from web.store import ChatStore, FlagStore, ReportStore, ScheduleRunStore


@pytest.mark.parametrize("cls,module", [
    (UserStore, "web.auth"),
    (ChatStore, "web.store"),
    (FlagStore, "web.store"),
    (ReportStore, "web.store"),
    (ScheduleRunStore, "web.store"),
])
def test_private_store_setup_runs_once_per_instance(cls, module, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(f"{module}.ensure_private_store", lambda path: calls.append(path))
    monkeypatch.setenv("JAYNET_ADMIN_PASSWORD", "pw")
    instance = cls(str(tmp_path / f"{cls.__name__}.db"))
    assert len(calls) == 1
    with instance._conn() as conn:
        conn.execute("SELECT 1").fetchone()
    assert len(calls) == 1


def test_users_get_includes_session_epoch(tmp_path, monkeypatch):
    monkeypatch.setenv("JAYNET_ADMIN_PASSWORD", "pw")
    users = UserStore(str(tmp_path / "users.db"))
    assert users.get("admin")["session_epoch"] == users.session_epoch("admin")
