"""Path-traversal guards for client-controlled run_ids.

A saved chat's turns carry run_ids chosen by the client; they are used as path
components under outputs/. These tests pin that a forged run_id (../, absolute,
nested) can neither delete nor read nor copy outside outputs/, and that the
save boundary rejects it with a clean 4xx. Endpoint tests drive FastAPI
in-process (see docs/testing-harness.md).
"""
import uuid

import pytest

from runtime.outputs import (
    delete_output,
    is_safe_run_id,
    mark_saved,
    read_manifest,
    stage_and_bundle,
)
from web import projects as PJ


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


# ---- unit: owner scoping (client-supplied run_ids in saved chats) -----------
def _staged(outputs, rid, owner, tmp_path):
    src = tmp_path / f"src-{rid[:6]}.txt"
    src.write_text("x")
    stage_and_bundle(outputs, rid, owner, [str(src)], None, 1 << 20)


def test_mark_saved_owner_scoped(tmp_path):
    outputs = tmp_path / "outputs"
    rid = uuid.uuid4().hex
    _staged(outputs, rid, "alice", tmp_path)
    mark_saved(outputs, rid, True, owner="bob")        # not yours -> no-op
    assert read_manifest(outputs, rid)["saved"] is False
    mark_saved(outputs, rid, True, owner="alice")
    assert read_manifest(outputs, rid)["saved"] is True
    mark_saved(outputs, rid, False)                    # owner None -> unchecked
    assert read_manifest(outputs, rid)["saved"] is False


def test_delete_output_owner_scoped(tmp_path):
    outputs = tmp_path / "outputs"
    rid = uuid.uuid4().hex
    _staged(outputs, rid, "alice", tmp_path)
    delete_output(outputs, rid, owner="bob")           # not yours -> kept
    assert (outputs / rid).exists()
    delete_output(outputs, rid, owner="alice")
    assert not (outputs / rid).exists()


def test_delete_output_manifestless_orphan_allowed(tmp_path):
    """No manifest = orphan staging with no owner to protect; still deletable."""
    outputs = tmp_path / "outputs"
    rid = uuid.uuid4().hex
    (outputs / rid / "files").mkdir(parents=True)
    delete_output(outputs, rid, owner="bob")
    assert not (outputs / rid).exists()


# ---- endpoint: in-process app (conftest web_app/web_client fixtures) ----------
@pytest.mark.asyncio
async def test_save_chat_rejects_forged_run_id(tmp_path, web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        bad = {"turns": [{"user_message": "u", "answer": "a",
                          "run_id": "../../projects"}]}
        assert (await c.post("/api/chats", json=bad)).status_code == 422
        ok = {"turns": [{"user_message": "u", "answer": "a",
                         "run_id": uuid.uuid4().hex}]}
        assert (await c.post("/api/chats", json=ok)).status_code == 200
        non = {"turns": [{"user_message": "u", "answer": "a"}]}
        assert (await c.post("/api/chats", json=non)).status_code == 200


@pytest.mark.asyncio
async def test_delete_chat_never_deletes_outside_outputs(tmp_path, web_app, web_client):
    """Stored turns (legacy or tampered DB rows) with hostile run_ids: deleting
    the chat must skip them, not rmtree outside outputs/."""
    app = web_app()
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
    async with web_client(app) as c:
        r = await c.delete("/api/chats/c1")
    assert r.status_code == 200
    assert (victim / "keep.txt").is_file()     # traversal ids refused
    assert not (outputs / rid).exists()        # real output still cleaned up


@pytest.mark.asyncio
async def test_promote_chat_cannot_steal_other_users_files(tmp_path, web_app, web_client):
    """The project sweep copies outputs/<rid>/files; a forged rid pointing at
    another owner's project must be skipped, a legit rid still sweeps."""
    app = web_app()
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
    async with web_client(app) as c:
        r = await c.post("/api/chats/c2/promote", json={"name": "P"})
    assert r.status_code == 200
    root = PJ.files_root(projects, "admin", r.json()["project"]["id"])
    assert not (root / "secret.txt").exists()  # no cross-user theft
    assert (root / "mine.txt").is_file()       # own output still swept


@pytest.mark.asyncio
async def test_save_chat_cannot_pin_other_users_output(tmp_path, web_app, web_client):
    """A forged run_id in saved turns must not flip saved:true on an output
    owned by someone else."""
    app = web_app()
    outputs = tmp_path / "outputs"
    rid = uuid.uuid4().hex
    _staged(outputs, rid, "victim", tmp_path)
    async with web_client(app) as c:
        r = await c.post("/api/chats", json={
            "turns": [{"user_message": "u", "answer": "a", "run_id": rid}]})
    assert r.status_code == 200
    assert read_manifest(outputs, rid)["saved"] is False


@pytest.mark.asyncio
async def test_delete_chat_cannot_delete_other_users_output(tmp_path, web_app, web_client):
    """Deleting your chat must not delete an output whose manifest belongs to
    another user, even if a turn carries its run_id."""
    app = web_app()
    outputs = tmp_path / "outputs"
    rid = uuid.uuid4().hex
    _staged(outputs, rid, "victim", tmp_path)
    app.state.chats.upsert("c3", "t", [
        {"user_message": "u", "answer": "a", "run_id": rid},
    ], owner="admin")
    async with web_client(app) as c:
        r = await c.delete("/api/chats/c3")
    assert r.status_code == 200
    assert (outputs / rid).exists()            # victim's output untouched
