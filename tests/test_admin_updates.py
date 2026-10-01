"""Admin → Status Updates card: GET /api/admin/updates + runtime/update_check.

The network and subprocess probes are module-level seams (_fetch_json /
_run_stdout) — tests monkeypatch them, never the network. paths.HOME is
redirected so the litellmenv probe reads a fake venv/lock, never the real
install.
"""
import time

import pytest

from runtime import update_check as uc


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(uc, "CACHE_PATH", tmp_path / "update_check.json")
    monkeypatch.setattr(uc.paths, "HOME", tmp_path)
    (tmp_path / "litellmenv" / "bin").mkdir(parents=True)
    (tmp_path / "litellmenv" / "bin" / "python").write_text("")
    (tmp_path / "requirements-litellm.lock").write_text("litellm==1.102.1\n")
    monkeypatch.setattr(uc, "_fetch_json", _fake_fetch)
    monkeypatch.setattr(uc, "_run_stdout", _fake_run)


FETCHES = []


async def _fake_fetch(url):
    FETCHES.append(url)
    if "h5i" in url:
        return {"tag_name": "v0.4.7"}
    if "llama" in url:
        # a list now (releases?per_page=15): the newest B-TAG wins, an
        # asset-less/non-b tag ahead of it is skipped
        return [{"tag_name": "v0.5.0"}, {"tag_name": "b11300"}]
    if "jevify" in url:
        return {"info": {"version": "0.1.0"}}
    if "litellm" in url:
        return {"info": {"version": "1.103.1"}}
    return None


def _fake_run(cmd, timeout=15):
    exe = str(cmd[0])
    if exe == "h5i":
        return "h5i 0.4.1"
    if exe == "jevify":
        return "jevify 0.1.0"
    if "litellmenv" in exe:
        return "1.102.1"
    return "version: 0.5.0-dev (build 11280, commit abc)"   # llama --version


def test_ver_tuple_and_status():
    assert uc._ver_tuple("v0.4.7") == (0, 4, 7)
    assert uc._ver_tuple("b11282") == (11282,)
    assert uc._status("0.4.1", "v0.4.7") == "behind"
    assert uc._status("0.4.7", "v0.4.7") == "current"
    assert uc._status(None, "v0.4.7") == "missing"
    assert uc._status("0.4.1", None) == "unknown"


@pytest.mark.asyncio
async def test_check_updates_statuses(tmp_path):
    llama = tmp_path / "llama-server"
    llama.write_text("")
    FETCHES.clear()
    out = await uc.check_updates({}, llama_bins=[str(llama)], refresh=True)
    by = {c["component"]: c for c in out["components"]}
    assert by["h5i (browser plugin)"]["status"] == "behind"      # 0.4.1 < 0.4.7
    assert by["jevify (jev plugin sidecar)"]["status"] == "current"
    lit = by["litellm proxy (litellmenv)"]
    assert lit["status"] == "current" and lit["pinned"] == "1.102.1"
    ll = by["llama.cpp servers"]
    assert ll["status"] == "behind" and ll["installed"] == "b11280"
    assert ll["detail"] == {str(llama): "b11280"}


@pytest.mark.asyncio
async def test_litellm_lock_drift(monkeypatch):
    """Installed below the LOCK pin is the real drift (live ran 1.87.0 while
    the lock said 1.102.1) — behind the pin beats informational PyPI-latest."""
    def run(cmd, timeout=15):
        if "litellmenv" in str(cmd[0]):
            return "1.87.0"
        return _fake_run(cmd, timeout)
    monkeypatch.setattr(uc, "_run_stdout", run)
    out = await uc.check_updates({}, llama_bins=[], refresh=True)
    lit = next(c for c in out["components"] if c["component"].startswith("litellm"))
    assert lit["installed"] == "1.87.0" and lit["status"] == "behind"
    assert "requirements-litellm.lock" in lit["hint"]


@pytest.mark.asyncio
async def test_cache_and_refresh():
    FETCHES.clear()
    first = await uc.check_updates({}, llama_bins=[], refresh=True)
    n = len(FETCHES)
    second = await uc.check_updates({}, llama_bins=[])
    assert len(FETCHES) == n, "cached within 24h — no new probes"
    assert second["checked_at"] == first["checked_at"]
    await uc.check_updates({}, llama_bins=[], refresh=True)
    assert len(FETCHES) > n, "refresh=1 bypasses the cache"


@pytest.mark.asyncio
async def test_stale_cache_rechecks(monkeypatch):
    await uc.check_updates({}, llama_bins=[], refresh=True)
    import json
    data = json.loads(uc.CACHE_PATH.read_text())
    data["checked_at"] = time.time() - 25 * 3600
    uc.CACHE_PATH.write_text(json.dumps(data))
    FETCHES.clear()
    await uc.check_updates({}, llama_bins=[])
    assert FETCHES, "a stale cache re-probes"


@pytest.mark.asyncio
async def test_probe_failures_degrade_to_unknown(monkeypatch):
    async def no_net(url):
        return None
    def no_bin(cmd, timeout=15):
        return None
    monkeypatch.setattr(uc, "_fetch_json", no_net)
    monkeypatch.setattr(uc, "_run_stdout", no_bin)
    out = await uc.check_updates({}, llama_bins=[], refresh=True)
    statuses = {c["status"] for c in out["components"]}
    assert statuses <= {"missing", "unknown"}


@pytest.mark.asyncio
async def test_admin_updates_endpoint(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/updates?refresh=1")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        names = {c["component"] for c in body["components"]}
        assert "h5i (browser plugin)" in names
        assert "litellm proxy (litellmenv)" in names


@pytest.mark.asyncio
async def test_admin_updates_disabled(web_app, web_client):
    app = web_app()
    app.state.runtime.config["updates"] = {"enabled": False}
    async with web_client(app) as c:
        r = await c.get("/api/admin/updates")
        assert r.status_code == 200
        assert r.json() == {"enabled": False, "components": []}
