"""Admin models-dir tree + binary --help viewer.

models/tree: flat file listing of JAYNET_MODELS plus a map of which presets
reference which files ($ORCH_MODELS/absolute forms, remotes skipped).
binaries/{name}/help: runs the binary's --help with a per-path cache; the
subprocess seam is faked — no real llama-server ever runs.
"""
import stat
import subprocess

import pytest

import runtime.paths
import web.routes_admin as routes_admin


@pytest.fixture
def models_dir(tmp_path, monkeypatch):
    d = tmp_path / "models"
    (d / "Qwen").mkdir(parents=True)
    (d / "proj").mkdir()
    (d / "Qwen" / "a.gguf").write_bytes(b"gguf")
    (d / "proj" / "b.gguf").write_bytes(b"mmproj")
    (d / "chat.jinja").write_text("{{ template }}")
    monkeypatch.setattr(runtime.paths, "MODELS_DIR", d)
    return d


async def _mk_preset(c, name, conf, **fields):
    body = {"name": name, "conf": conf, **fields}
    r = await c.post("/api/admin/presets", json=body)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_models_tree_lists_entries(web_app, web_client, models_dir):
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/models/tree")
    assert r.status_code == 200
    j = r.json()
    assert j["models_dir"] == str(models_dir)
    paths = {e["path"]: e for e in j["entries"]}
    assert paths["Qwen"]["type"] == "dir"
    assert paths["Qwen/a.gguf"]["type"] == "file"
    assert paths["Qwen/a.gguf"]["size"] == 4
    assert "chat.jinja" in paths
    # none of OUR files are referenced (seed presets point at their own paths)
    assert "Qwen/a.gguf" not in j["assigned"]
    assert "proj/b.gguf" not in j["assigned"]


@pytest.mark.asyncio
async def test_models_tree_missing_dir_is_empty(web_app, web_client,
                                                tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.paths, "MODELS_DIR", tmp_path / "nope")
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/models/tree")
    assert r.status_code == 200
    assert r.json()["entries"] == []


@pytest.mark.asyncio
async def test_models_tree_assigned(web_app, web_client, models_dir, tmp_path):
    app = web_app()
    outside = tmp_path / "elsewhere.gguf"
    outside.write_bytes(b"x")
    async with web_client(app) as c:
        # $ORCH_MODELS form + absolute MMPROJ
        await _mk_preset(c, "p1",
                         "MODEL_PATH=$ORCH_MODELS/Qwen/a.gguf\n"
                         f"MMPROJ={models_dir}/proj/b.gguf\n")
        # $JAYNET_MODELS form, same file as p1
        await _mk_preset(c, "p2",
                         "MODEL_PATH=${JAYNET_MODELS}/Qwen/a.gguf\n"
                         "TOOLS_TEMPLATE=$ORCH_MODELS/chat.jinja\n")
        # remote preset: never owns local weights — skipped
        await _mk_preset(c, "p3",
                         "MODEL_PATH=$ORCH_MODELS/Qwen/a.gguf\n",
                         remote_host="192.168.1.50", port=8080)
        # outside the models dir — ignored
        await _mk_preset(c, "p4", f"MODEL_PATH={outside}\n")
        r = await c.get("/api/admin/models/tree")
    assert r.status_code == 200
    assigned = r.json()["assigned"]
    assert assigned["Qwen/a.gguf"] == ["p1", "p2"]
    assert assigned["proj/b.gguf"] == ["p1"]
    assert assigned["chat.jinja"] == ["p2"]
    assert all("p3" not in v and "p4" not in v for v in assigned.values())
    assert str(outside) not in assigned


@pytest.fixture
def fake_bin(tmp_path):
    p = tmp_path / "llama-server"
    p.write_text("#!/bin/sh\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


async def _register_bin(c, path):
    r = await c.put("/api/admin/binaries", json={
        "binaries": [{"name": "rocm", "path": str(path), "device_env": ""}]})
    assert r.status_code == 200, r.text


@pytest.fixture(autouse=True)
def _clear_help_cache():
    routes_admin._BIN_HELP_CACHE.clear()
    yield
    routes_admin._BIN_HELP_CACHE.clear()


@pytest.mark.asyncio
async def test_binary_help_ok_and_cached(web_app, web_client, fake_bin,
                                         monkeypatch):
    calls = []

    def fake_run(path):
        calls.append(path)
        return "usage: llama-server [options]"   # non-zero exits still fine
    monkeypatch.setattr(routes_admin, "_run_binary_help", fake_run)
    app = web_app()
    async with web_client(app) as c:
        await _register_bin(c, fake_bin)
        r1 = await c.get("/api/admin/binaries/rocm/help")
        r2 = await c.get("/api/admin/binaries/rocm/help")
    assert r1.status_code == 200
    j = r1.json()
    assert j["name"] == "rocm" and j["path"] == str(fake_bin)
    assert "usage: llama-server" in j["help"]
    assert r2.status_code == 200
    assert calls == [str(fake_bin)]          # second call served from cache


@pytest.mark.asyncio
async def test_binary_help_unknown_404(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/binaries/nope/help")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_binary_help_not_executable_400(web_app, web_client, tmp_path):
    app = web_app()
    async with web_client(app) as c:
        await _register_bin(c, tmp_path / "missing-server")
        r = await c.get("/api/admin/binaries/rocm/help")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_binary_help_timeout_502(web_app, web_client, fake_bin,
                                       monkeypatch):
    def fake_run(path):
        raise subprocess.TimeoutExpired([path, "--help"], 15)
    monkeypatch.setattr(routes_admin, "_run_binary_help", fake_run)
    app = web_app()
    async with web_client(app) as c:
        await _register_bin(c, fake_bin)
        r = await c.get("/api/admin/binaries/rocm/help")
    assert r.status_code == 502
    assert "timed out" in r.json()["detail"]


@pytest.mark.asyncio
async def test_binary_help_no_output_502(web_app, web_client, fake_bin,
                                         monkeypatch):
    monkeypatch.setattr(routes_admin, "_run_binary_help", lambda path: "  \n")
    app = web_app()
    async with web_client(app) as c:
        await _register_bin(c, fake_bin)
        r = await c.get("/api/admin/binaries/rocm/help")
    assert r.status_code == 502


def test_run_binary_help_combines_streams(monkeypatch):
    class _R:
        returncode = 1                    # some builds exit non-zero on --help
        stdout = "usage out\n"
        stderr = "usage err\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _R())
    assert routes_admin._run_binary_help("/x") == "usage out\nusage err\n"
