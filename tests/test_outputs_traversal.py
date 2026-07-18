"""Path-traversal guards for client-controlled run_ids.

A saved chat's turns carry run_ids chosen by the client; they are used as path
components under outputs/. These tests pin that a forged run_id (../, absolute,
nested) can neither delete nor read nor copy outside outputs/, and that the
save boundary rejects it with a clean 4xx. Endpoint tests drive FastAPI
in-process (see docs/testing-harness.md).
"""
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import yaml

import web
from runtime.outputs import (delete_output, is_safe_run_id, mark_saved,
                             read_manifest, stage_and_bundle)
from web import projects as PJ

ROOT = Path(web.__file__).resolve().parent.parent


# ---- unit: the guard itself -------------------------------------------------
def test_safe_run_id_accepts_minted_and_plain_names():
    assert is_safe_run_id(uuid.uuid4().hex)
    assert is_safe_run_id("run123")


@pytest.mark.parametrize("rid", ["..", "../x", "a/b", "a\\b", "/etc", ".", "",
                                 "a\x00b", None, 123])
def test_safe_run_id_rejects_traversal(rid):
    assert not is_safe_run_id(rid)


def test_delete_output_refuses_traversal(tmp_path):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    for rid in ("../victim", "..", str(victim)):       # relative, parent, absolute
        delete_output(outputs, rid)                    # must not raise
        assert (victim / "keep.txt").is_file(), rid


def test_delete_output_still_deletes_real_run(tmp_path):
    outputs = tmp_path / "outputs"
    rid = uuid.uuid4().hex
    (outputs / rid / "files").mkdir(parents=True)
    delete_output(outputs, rid)
    assert not (outputs / rid).exists()
    assert outputs.is_dir()                            # root itself untouched


def test_mark_saved_refuses_traversal(tmp_path):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    outside = tmp_path / "manifest.json"
    outside.write_text('{"saved": false}')
    mark_saved(outputs, "..", True)                    # must not raise or write
    assert outside.read_text() == '{"saved": false}'


def test_read_manifest_refuses_traversal(tmp_path):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (tmp_path / "manifest.json").write_text('{"owner": "eve"}')
    assert read_manifest(outputs, "..") is None


def test_stage_and_bundle_rejects_bad_run_id(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    with pytest.raises(ValueError):
        stage_and_bundle(tmp_path / "outputs", "../evil", "alice",
                         [str(f)], None, 1 << 20)


# ---- endpoint: in-process app (docs/testing-harness.md pattern) -------------
def _app(tmp_path, monkeypatch):
    base = tmp_path
    (base / "config").mkdir()
    (base / "prompts").mkdir()
    cfg = yaml.safe_load(open(ROOT / "config/runtime.yaml"))
    cfg["trace"]["db_path"] = str(base / "trace.db")
    cfg["orchestrator"]["system_prompt"] = "prompts/orchestrator.md"
    cfg["web"] = {"chats_db": str(base / "chats.db"),
                  "users_db": str(base / "users.db"),
                  "outputs_dir": str(base / "outputs"),
                  "projects_dir": str(base / "projects")}
    (base / "prompts" / "orchestrator.md").write_text("P")
    yaml.safe_dump(cfg, open(base / "config" / "runtime.yaml", "w"))
    monkeypatch.setenv("ORCH_ADMIN_USER", "admin")
    monkeypatch.setenv("ORCH_ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("ORCH_SESSION_SECRET", "t")
    from web.server import create_app
    app = create_app(str(base / "config" / "runtime.yaml"))

    async def fake_run(msg, **kw):      # mock the model — no LiteLLM needed
        return {}
    app.state.runtime.run = fake_run
    return app


@asynccontextmanager
async def _client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/login", json={"username": "admin", "password": "pw"})
        assert r.status_code == 200
        yield c


@pytest.mark.asyncio
async def test_save_chat_rejects_forged_run_id(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with _client(app) as c:
        bad = {"turns": [{"user_message": "u", "answer": "a",
                          "run_id": "../../projects"}]}
        assert (await c.post("/api/chats", json=bad)).status_code == 422
        ok = {"turns": [{"user_message": "u", "answer": "a",
                         "run_id": uuid.uuid4().hex}]}
        assert (await c.post("/api/chats", json=ok)).status_code == 200
        non = {"turns": [{"user_message": "u", "answer": "a"}]}
        assert (await c.post("/api/chats", json=non)).status_code == 200


@pytest.mark.asyncio
async def test_delete_chat_never_deletes_outside_outputs(tmp_path, monkeypatch):
    """Stored turns (legacy or tampered DB rows) with hostile run_ids: deleting
    the chat must skip them, not rmtree outside outputs/."""
    app = _app(tmp_path, monkeypatch)
    outputs = tmp_path / "outputs"
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    rid = uuid.uuid4().hex
    (outputs / rid / "files").mkdir(parents=True)
    app.state.chats.upsert("c1", "t", [
        {"user_message": "u", "answer": "a", "run_id": "../victim"},
        {"user_message": "u", "answer": "a", "run_id": str(victim)},
        {"user_message": "u", "answer": "a", "run_id": rid},
    ], owner="admin")
    async with _client(app) as c:
        r = await c.delete("/api/chats/c1")
    assert r.status_code == 200
    assert (victim / "keep.txt").is_file()     # traversal ids refused
    assert not (outputs / rid).exists()        # real output still cleaned up


@pytest.mark.asyncio
async def test_promote_chat_cannot_steal_other_users_files(tmp_path, monkeypatch):
    """The project sweep copies outputs/<rid>/files; a forged rid pointing at
    another owner's project must be skipped, a legit rid still sweeps."""
    app = _app(tmp_path, monkeypatch)
    projects = tmp_path / "projects"
    meta = PJ.create_project(projects, "victim", "V")
    (PJ.files_root(projects, "victim", meta["id"]) / "secret.txt").write_text("s")
    src = tmp_path / "mine.txt"
    src.write_text("m")
    good = uuid.uuid4().hex
    stage_and_bundle(tmp_path / "outputs", good, "admin", [str(src)], None, 1 << 20)
    forged = f"../projects/victim/{meta['id']}"
    app.state.chats.upsert("c2", "t", [
        {"user_message": "u", "answer": "a", "run_id": forged},
        {"user_message": "u", "answer": "a", "run_id": good},
    ], owner="admin")
    async with _client(app) as c:
        r = await c.post("/api/chats/c2/promote", json={"name": "P"})
    assert r.status_code == 200
    root = PJ.files_root(projects, "admin", r.json()["project"]["id"])
    assert not (root / "secret.txt").exists()  # no cross-user theft
    assert (root / "mine.txt").is_file()       # own output still swept
