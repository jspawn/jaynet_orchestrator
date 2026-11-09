"""Preset-driven jevify recipes, admin configuration and sidecar lifecycle."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import FastAPI

JEV_DIR = Path(__file__).resolve().parent.parent / "plugins" / "jev"


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, JEV_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def admin(tmp_path, monkeypatch):
    mod = _load("jevify_plugin_admin", "jevify_admin.py")
    monkeypatch.setattr(mod.paths, "DATA", tmp_path)
    monkeypatch.setattr(mod, "binary_path", lambda: "/usr/bin/jevify")
    return mod


def _row(name="brain", *, port=8090, served="brain-model", archived=0,
         remote="", binary="llama"):
    return {"name": name, "alias": name, "served_id": served, "port": port,
            "archived": archived, "remote_host": remote, "binary": binary}


def _catalog(monkeypatch, mod, rows, slots=None, types=None):
    class Store:
        def list_full(self):
            return rows, slots or {}

        def binary_type(self, preset):
            return (types or {}).get(preset.get("binary"), "llama")

    monkeypatch.setattr(mod, "PresetStore", lambda path: Store())
    monkeypatch.setattr(mod, "db_path_for", lambda config: ":memory:")


@pytest.mark.asyncio
async def test_preset_options_filter_and_report_running(admin, monkeypatch):
    rows = [_row(archived=1), _row("old", archived=1),
            _row("remote", remote="host")]
    _catalog(monkeypatch, admin, rows, {"brain": "brain"})

    async def ids(base):
        assert base == "http://127.0.0.1:8090"
        return ["brain-model"]

    monkeypatch.setattr(admin, "query_model_ids", ids)
    options = await admin.preset_options({})
    assert len(options) == 1
    assert options[0] == {
        "name": "brain", "alias": "brain", "slots": ["brain"],
        "backend": "llamacpp", "endpoint": "http://127.0.0.1:8090",
        "running": True, "served_id": "brain-model"}


@pytest.mark.asyncio
async def test_selected_preset_rejects_port_occupant_mismatch(admin, monkeypatch):
    _catalog(monkeypatch, admin, [_row(served="expected-unique")],
             types={"llama": "vllm"})
    monkeypatch.setattr(admin, "query_model_ids",
                        lambda base: _async_value(["totally-unrelated"]))
    with pytest.raises(admin.AdminError) as exc:
        await admin.selected_preset({}, "brain")
    assert exc.value.status == 409 and "different model" in exc.value.detail


def _async_value(value):
    async def result():
        return value
    return result()


@pytest.mark.asyncio
async def test_selected_preset_maps_vllm_dialect(admin, monkeypatch):
    _catalog(monkeypatch, admin, [_row()], types={"llama": "vllm"})
    monkeypatch.setattr(admin, "query_model_ids",
                        lambda base: _async_value(["brain-model"]))
    preset, served, dialect = await admin.selected_preset({}, "brain")
    assert preset["endpoint"] == "http://127.0.0.1:8090"
    assert served == "brain-model" and dialect == "vllm"


@pytest.mark.asyncio
async def test_recipe_probe_atomic_install_and_reuse(admin, monkeypatch):
    calls = []

    async def probe(argv, timeout):
        calls.append(argv)
        path = Path(argv[-1])
        data = yaml.safe_load(path.read_text())
        data["probe"] = {"rung": "top_k"}
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        return 0, b"", b""

    monkeypatch.setattr(admin, "proc_run", probe)
    preset = {**_row(), "endpoint": "http://127.0.0.1:8090"}
    final, probed = await admin.prepare_recipe(preset, "brain-model", "llamacpp")
    assert probed and final.is_file()
    data = yaml.safe_load(final.read_text())
    assert data["model"]["name"] == "brain-model"
    assert data["endpoint"] == {"base_url": "http://127.0.0.1:8090/v1",
                                "dialect": "llamacpp", "timeout_s": 60.0}
    assert data["probe"] == {"rung": "top_k"}
    assert calls[0][:2] == ["/usr/bin/jevify", "probe"]

    async def unexpected(argv, timeout):
        raise AssertionError("matching probed recipe should be reused")

    monkeypatch.setattr(admin, "proc_run", unexpected)
    reused, probed = await admin.prepare_recipe(preset, "brain-model", "llamacpp")
    assert reused == final and not probed


@pytest.mark.asyncio
async def test_failed_reprobe_preserves_previous_recipe(admin, monkeypatch):
    preset = {**_row(), "endpoint": "http://127.0.0.1:8090"}
    final = admin._recipe_path("brain", "llamacpp")
    final.parent.mkdir(parents=True)
    original = admin._new_recipe("brain-model", preset["endpoint"],
                                 "llamacpp", "brain")
    original["probe"] = {"rung": "top_k"}
    final.write_text(yaml.safe_dump(original, sort_keys=False))
    before = final.read_bytes()

    async def failed(argv, timeout):
        Path(argv[-1]).write_text("corrupted")
        return 1, b"", b"unsupported logprobs"

    monkeypatch.setattr(admin, "proc_run", failed)
    with pytest.raises(admin.AdminError) as exc:
        await admin.prepare_recipe(preset, "brain-model", "llamacpp", reprobe=True)
    assert exc.value.status == 422 and "unsupported logprobs" in exc.value.detail
    assert final.read_bytes() == before


def test_sidecar_port_avoids_foreign_and_keeps_managed(admin, monkeypatch):
    class Mgr:
        def __init__(self, names):
            self._names = names

        def names(self):
            return self._names

    monkeypatch.setattr(admin, "_port_free", lambda port: port != 8600)
    assert admin.choose_sidecar_port(Mgr([]), "http://127.0.0.1:8600") == 8601
    assert admin.choose_sidecar_port(
        Mgr(["jevify"]), "http://127.0.0.1:8600") == 8600


@pytest.mark.asyncio
async def test_restart_sidecar_quotes_recipe_and_waits(admin, monkeypatch):
    class Mgr:
        def __init__(self):
            self.calls = []

        async def stop_one(self, name):
            self.calls.append(("stop", name))

        def add(self, name, command, **kwargs):
            self.calls.append(("add", name, command))

        async def start_one(self, name):
            self.calls.append(("start", name))

        def status(self):
            return {"jevify": {"alive": True}}

    async def health(base):
        return {"ok": True}

    mgr = Mgr()
    monkeypatch.setattr(admin, "health_now", health)
    result = await admin.restart_sidecar(
        mgr, Path("/tmp/a recipe.yaml"), "http://127.0.0.1:8601")
    assert result == {"alive": True}
    assert [call[:2] for call in mgr.calls] == [
        ("stop", "jevify"), ("add", "jevify"), ("start", "jevify")]
    assert "'/tmp/a recipe.yaml'" in mgr.calls[1][2]


class _Users:
    def __init__(self):
        self.overrides = {"existing": 1}

    def get_config_overrides(self):
        return dict(self.overrides)

    def set_config_overrides(self, values):
        self.overrides = dict(values)
        return True


class _Mgr:
    def __init__(self):
        self.names_value = []

    def names(self):
        return self.names_value

    def status(self):
        return {}

    async def remove(self, name):
        self.names_value = []


def _routes_state(monkeypatch):
    routes = _load("jev_test_admin_routes", "routes.py")
    admin = sys.modules["jevify_plugin_admin"]
    from runtime import process_manager
    mgr = _Mgr()
    monkeypatch.setattr(process_manager, "CURRENT", mgr)
    state = SimpleNamespace(
        runtime=SimpleNamespace(config={"plugins": {"jev": {"enabled": True}}}),
        users=_Users(), startup_hooks=[], shutdown_hooks=[])
    app = FastAPI()
    routes.register(app, state)
    return routes, admin, state, mgr, app


@pytest.mark.asyncio
async def test_configure_persists_preset_and_restarts_only_jevify(
        admin, monkeypatch, tmp_path):
    routes, admin, state, mgr, app = _routes_state(monkeypatch)
    calls = []

    async def selected(config, name):
        return {**_row(), "endpoint": "http://127.0.0.1:8090"}, "brain-model", "llamacpp"

    async def prepare(preset, served, dialect, reprobe=False):
        return tmp_path / "brain.yaml", True

    async def restart(manager, recipe, base_url):
        calls.append((manager, recipe, base_url))
        return {"alive": True}

    monkeypatch.setattr(admin, "selected_preset", selected)
    monkeypatch.setattr(admin, "prepare_recipe", prepare)
    monkeypatch.setattr(admin, "choose_sidecar_port", lambda mgr, url: 8601)
    monkeypatch.setattr(admin, "restart_sidecar", restart)
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/admin/plugins/jev/api/configure",
            json={"preset": "brain", "reprobe": True})
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "brain-model"
    assert state.users.overrides["plugins.jev.preset"] == "brain"
    assert state.users.overrides["plugins.jev.manage_sidecar"] is True
    assert state.users.overrides["existing"] == 1
    cfg = state.runtime.config["plugins"]["jev"]
    assert cfg["base_url"] == "http://127.0.0.1:8601"
    assert cfg["model"] == "brain-model"
    assert calls == [(mgr, tmp_path / "brain.yaml", "http://127.0.0.1:8601")]


@pytest.mark.asyncio
async def test_probe_failure_does_not_persist_or_restart(admin, monkeypatch):
    _, admin, state, _, app = _routes_state(monkeypatch)

    async def selected(config, name):
        return {**_row(), "endpoint": "http://127.0.0.1:8090"}, "brain-model", "llamacpp"

    async def failed(*args, **kwargs):
        raise admin.AdminError(422, "probe failed")

    async def unexpected(*args):
        raise AssertionError("sidecar must not restart after a failed probe")

    monkeypatch.setattr(admin, "selected_preset", selected)
    monkeypatch.setattr(admin, "prepare_recipe", failed)
    monkeypatch.setattr(admin, "restart_sidecar", unexpected)
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/admin/plugins/jev/api/configure", json={"preset": "brain"})
    assert response.status_code == 422
    assert state.users.overrides == {"existing": 1}


@pytest.mark.asyncio
async def test_test_decision_uses_existing_client(admin, monkeypatch):
    _, _, state, _, app = _routes_state(monkeypatch)
    state.runtime.config["plugins"]["jev"]["base_url"] = "http://127.0.0.1:8601"
    client = SimpleNamespace(
        JevError=ValueError,
        settings=lambda config: {"timeout_s": 2},
        decide=lambda config, state, questions, timeout: {
            "health": {"noul": 0.98}})
    monkeypatch.setitem(sys.modules, "jev_plugin_client", client)
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post("/api/admin/plugins/jev/api/test", json={})
    assert response.status_code == 200
    assert response.json()["answers"]["health"]["noul"] == 0.98
