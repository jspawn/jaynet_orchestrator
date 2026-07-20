"""Web-layer regressions: saved-chat ownership and the quick-reply fast-path.

Bug 1: POST /api/chats with an existing chat id kept the victim's `owner` but
deleted and re-inserted their turns — knowing a chat id meant being able to
destroy it. ChatStore.upsert now refuses cross-owner updates (returns None)
and the endpoint answers 404, the same not-found style as get/rename/delete.

Bug 2: the quick-reply fast-path in POST /api/chat stashed `tasks[run_id] =
None`, never set run_owner, and never cleaned up — so /api/stream/{run_id}
404'd for the owner and /api/admin/status 500'd (None.done AttributeError)
forever after the first greeting.

Budget governance: POST /api/chat layers ceilings as admin global defaults <
per-user account defaults < request overrides, and a request override may only
LOWER a ceiling (per-key min) — never raise it past the effective default.

Security hardening batch:
- Legacy owner-NULL chats (pre-migration rows) are shared read-only history:
  anyone can GET them, but PATCH/DELETE are admin-only until the first save
  with an owner claims the row (sets owner).
- Deleting a user revokes their API tokens and deletes their saved chats; the
  on-disk dirs are reported as leftover_paths for manual cleanup (no rmtree).
- Inline output previews of text/html and image/svg+xml carry
  Content-Security-Policy: sandbox (blocks script execution same-origin).
- The global ORCH_WEB_TOKEN bearer is compared with hmac.compare_digest.
- Admin-created usernames must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ — they
  become path components (uploads/projects/chat-scratch owner dirs).
- Project file writes onto a directory path are a clean 4xx, not a 500.

Endpoint tests drive FastAPI in-process (see docs/testing-harness.md).
"""
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import yaml

import web
import web.server
from web.store import ChatStore

ROOT = Path(web.__file__).resolve().parent.parent


# ---- bug 1, unit: the store-level owner check --------------------------------
def test_upsert_refuses_cross_owner_update(tmp_path):
    s = ChatStore(str(tmp_path / "chats.db"))
    s.upsert("c1", "mine", [{"user_message": "u", "answer": "a"}], owner="alice")

    out = s.upsert("c1", "pwned", [{"user_message": "x", "answer": "y"}],
                   owner="bob")
    assert out is None                                       # refused
    chat = s.get("c1", owner="alice")
    assert chat["title"] == "mine"                           # victim untouched
    assert [t["user_message"] for t in chat["turns"]] == ["u"]

    out = s.upsert("c1", "renamed",
                   [{"user_message": "u2", "answer": "a2"}], owner="alice")
    assert out is not None and out["title"] == "renamed"     # owner can update


def test_upsert_legacy_null_owner_row_stays_claimable(tmp_path):
    """Rows predating ownership (owner NULL) may be updated by anyone; the
    first upsert carrying an owner claims the row (sets owner on it)."""
    s = ChatStore(str(tmp_path / "chats.db"))
    s.upsert("c1", "legacy", [{"user_message": "u", "answer": "a"}])   # owner NULL
    out = s.upsert("c1", "claimed", [{"user_message": "u", "answer": "a"}],
                   owner="bob")
    assert out is not None
    assert s.get("c1", owner="bob")["title"] == "claimed"


def test_null_owner_rename_delete_are_admin_only(tmp_path):
    """Legacy owner-NULL rows stay readable by everyone, but with no owner to
    match, rename/delete can only succeed for an admin (or an owner-less
    internal caller) until an upsert claims the row."""
    s = ChatStore(str(tmp_path / "chats.db"))
    s.upsert("c1", "legacy", [{"user_message": "u", "answer": "a"}])   # owner NULL
    assert s.get("c1", owner="bob") is not None               # read stays shared
    assert not s.rename("c1", "x", owner="bob")               # non-admin blocked
    assert not s.delete("c1", owner="bob")
    assert s.get("c1", owner="bob")["title"] == "legacy"      # untouched
    assert s.rename("c1", "adm", owner="bob", is_admin=True)  # admin allowed
    assert s.delete("c1", owner="bob", is_admin=True)
    assert s.get("c1") is None


def test_upsert_claims_null_owner_row(tmp_path):
    """The claim is what ends the shared/admin-only phase: after it, normal
    ownership rules apply to the row."""
    s = ChatStore(str(tmp_path / "chats.db"))
    s.upsert("c1", "legacy", [{"user_message": "u", "answer": "a"}])   # owner NULL
    out = s.upsert("c1", "claimed", [{"user_message": "u2", "answer": "a2"}],
                   owner="bob")
    assert out is not None
    assert s.get("c1", owner="bob")["owner"] == "bob"         # claimed
    assert s.get("c1", owner="alice") is None                 # no longer shared
    assert not s.rename("c1", "x", owner="alice")             # others blocked
    assert not s.delete("c1", owner="alice")
    assert s.rename("c1", "x", owner="bob")                   # new owner can modify


# ---- endpoint: in-process app (docs/testing-harness.md pattern) --------------
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
async def _client(app, username="admin", password="pw"):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/login", json={"username": username,
                                             "password": password})
        assert r.status_code == 200
        yield c


@pytest.mark.asyncio
async def test_save_chat_cannot_clobber_other_users_chat(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.users.create("eve", "pw2")
    async with _client(app) as c:
        r = await c.post("/api/chats", json={
            "id": "c1", "title": "mine",
            "turns": [{"user_message": "u", "answer": "a"}]})
        assert r.status_code == 200
    async with _client(app, "eve", "pw2") as c:
        r = await c.post("/api/chats", json={
            "id": "c1", "title": "pwned",
            "turns": [{"user_message": "evil", "answer": "evil"}]})
        assert r.status_code == 404                     # same as "no such chat"
        assert (await c.get("/api/chats/c1")).status_code == 404
    chat = app.state.chats.get("c1", owner="admin")     # victim's chat intact
    assert chat["title"] == "mine"
    assert [t["user_message"] for t in chat["turns"]] == ["u"]
    async with _client(app) as c:                       # owner can still update
        r = await c.post("/api/chats", json={
            "id": "c1", "title": "renamed",
            "turns": [{"user_message": "u2", "answer": "a2"}]})
        assert r.status_code == 200 and r.json()["title"] == "renamed"


@pytest.mark.asyncio
async def test_fast_path_run_is_tracked_and_streamable(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": "canned reply")
    app = _app(tmp_path, monkeypatch)
    async with _client(app) as c:
        r = await c.post("/api/chat", json={"message": "hi"})
        assert r.status_code == 200
        rid = r.json()["run_id"]
        # A real task, not None — /api/admin/status must not AttributeError.
        assert isinstance(app.state.tasks.get(rid), asyncio.Task)
        # run_owner is registered, so the owner's SSE stream is not a 404.
        # (wait_for: ASGITransport does not enforce httpx timeouts, and a
        # stream that never terminates would otherwise hang the suite.)
        r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
        assert r.status_code == 200
        assert "canned reply" in r.text and "run_finish" in r.text
        # Admin status survives fast-path runs (previously 500'd forever).
        assert (await c.get("/api/admin/status")).status_code == 200


@pytest.mark.asyncio
async def test_fast_path_run_state_is_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": "canned reply")
    monkeypatch.setattr(web.server, "_FORGET_AFTER_S", 0)   # cleanup immediately
    app = _app(tmp_path, monkeypatch)
    async with _client(app) as c:
        rid = (await c.post("/api/chat", json={"message": "hi"})).json()["run_id"]
        # Done-callback retires the task and the replay buffer (no leak).
        for _ in range(100):
            if rid not in app.state.tasks and rid not in app.state.bus._buffer:
                break
            await asyncio.sleep(0.02)
        assert rid not in app.state.tasks
        assert rid not in app.state.bus._buffer


# ---- budget governance on POST /api/chat --------------------------------------
def _record_run(app):
    """Capture the kwargs runtime.run is called with (the run itself is faked)."""
    seen = {}

    async def rec(msg, **kw):
        seen.update(kw)
        return {}

    app.state.runtime.run = rec
    return seen


async def _chat_budget(c, seen, payload):
    """POST /api/chat (quick-reply disabled by the caller) and wait for the
    background run task to have been invoked."""
    r = await c.post("/api/chat", json=payload)
    assert r.status_code == 200
    for _ in range(100):
        if seen:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("run was never invoked")


@pytest.mark.asyncio
async def test_chat_budget_override_cannot_raise_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = _app(tmp_path, monkeypatch)
    app.state.runtime.config["budgets"]["max_iterations"] = 50
    app.state.runtime.config["budgets"]["max_cost_usd"] = 1.0
    seen = _record_run(app)
    async with _client(app) as c:
        await _chat_budget(c, seen, {"message": "work", "budget_overrides": {
            "max_iterations": 500,          # above the 50 ceiling -> clamped
            "max_cost_usd": 0.10}})         # below the 1.0 ceiling -> honoured
    bo = seen["budget_overrides"]
    assert bo["max_iterations"] == 50
    assert bo["max_cost_usd"] == 0.10


@pytest.mark.asyncio
async def test_chat_applies_per_user_budget_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = _app(tmp_path, monkeypatch)
    app.state.users.set_budget_defaults("admin", {"max_cost_usd": 0.25,
                                                  "max_iterations": 30})
    seen = _record_run(app)
    async with _client(app) as c:
        await _chat_budget(c, seen, {"message": "work"})   # no request overrides
    bo = seen["budget_overrides"]
    assert bo["max_cost_usd"] == 0.25 and bo["max_iterations"] == 30
    # keys the user didn't set still come from the admin global defaults
    assert bo["max_wall_clock_s"] == \
        app.state.runtime.config["budgets"]["max_wall_clock_s"]


@pytest.mark.asyncio
async def test_chat_override_beats_user_default_only_when_lower(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = _app(tmp_path, monkeypatch)
    app.state.users.set_budget_defaults("admin", {"max_iterations": 30})
    seen = _record_run(app)
    async with _client(app) as c:
        await _chat_budget(c, seen, {"message": "work", "budget_overrides": {
            "max_iterations": 40}})         # above the user's 30 -> clamped to 30
        assert seen["budget_overrides"]["max_iterations"] == 30
        seen.clear()
        await _chat_budget(c, seen, {"message": "work", "budget_overrides": {
            "max_iterations": 20}})         # below the user's 30 -> honoured
        assert seen["budget_overrides"]["max_iterations"] == 20


@pytest.mark.asyncio
async def test_user_default_cannot_exceed_global_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = _app(tmp_path, monkeypatch)
    app.state.runtime.config["budgets"]["max_cost_usd"] = 1.0
    app.state.users.set_budget_defaults("admin", {"max_cost_usd": 999.0,
                                                  "max_iterations": 30})
    seen = _record_run(app)
    async with _client(app) as c:
        await _chat_budget(c, seen, {"message": "work"})
    bo = seen["budget_overrides"]
    assert bo["max_cost_usd"] == 1.0        # clamped to the admin global ceiling
    assert bo["max_iterations"] == 30       # below global -> honoured


@pytest.mark.asyncio
async def test_wall_clock_zero_global_allows_positive_override(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = _app(tmp_path, monkeypatch)
    app.state.runtime.config["budgets"]["max_wall_clock_s"] = 0   # no ceiling
    seen = _record_run(app)
    async with _client(app) as c:
        await _chat_budget(c, seen, {"message": "work", "budget_overrides": {
            "max_wall_clock_s": 3600}})     # tightening "no ceiling" is allowed
    assert seen["budget_overrides"]["max_wall_clock_s"] == 3600


# ---- legacy owner-NULL chats: shared read, admin-only modify ------------------
@pytest.mark.asyncio
async def test_null_owner_chat_read_shared_write_admin_only(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.users.create("eve", "pw2")
    app.state.chats.upsert("legacy", "legacy",
                           [{"user_message": "u", "answer": "a"}])   # owner NULL
    async with _client(app, "eve", "pw2") as c:
        # legacy shared history: anyone authenticated can still read it
        assert (await c.get("/api/chats/legacy")).status_code == 200
        r = await c.patch("/api/chats/legacy", json={"title": "pwned"})
        assert r.status_code == 404                       # same not-found style
        assert (await c.delete("/api/chats/legacy")).status_code == 404
    assert app.state.chats.get("legacy")["title"] == "legacy"   # untouched
    async with _client(app) as c:                               # session admin
        r = await c.patch("/api/chats/legacy", json={"title": "curated"})
        assert r.status_code == 200
        assert (await c.delete("/api/chats/legacy")).status_code == 200
    assert app.state.chats.get("legacy") is None


@pytest.mark.asyncio
async def test_null_owner_chat_claimed_by_first_save(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.users.create("eve", "pw2")
    app.state.users.create("mallory", "pw3")
    app.state.chats.upsert("legacy", "legacy",
                           [{"user_message": "u", "answer": "a"}])   # owner NULL
    async with _client(app, "eve", "pw2") as c:
        r = await c.post("/api/chats", json={
            "id": "legacy", "title": "claimed",
            "turns": [{"user_message": "u2", "answer": "a2"}]})
        assert r.status_code == 200
        # claimed now — the new owner may rename/delete it
        r = await c.patch("/api/chats/legacy", json={"title": "mine"})
        assert r.status_code == 200
    assert app.state.chats.get("legacy")["owner"] == "eve"
    async with _client(app, "mallory", "pw3") as c:
        # ...and for everyone else it stops being shared/writable
        assert (await c.get("/api/chats/legacy")).status_code == 404
        assert (await c.delete("/api/chats/legacy")).status_code == 404


# ---- user deletion revokes credentials and saved data -------------------------
@pytest.mark.asyncio
async def test_delete_user_revokes_tokens_and_removes_chats(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.users.create("eve", "pw2")
    token = app.state.users.create_api_token("eve", "cli")["token"]
    app.state.chats.upsert("c1", "eve's chat",
                           [{"user_message": "u", "answer": "a"}], owner="eve")
    dirs = [tmp_path / "uploads" / "eve", tmp_path / "projects" / "eve"]
    for d in dirs:                                       # on-disk leftovers
        d.mkdir(parents=True)
        (d / "f.txt").write_text("x")

    async def me_with_bearer():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get("/api/me", headers={"Authorization": f"Bearer {token}"})

    assert (await me_with_bearer()).status_code == 200   # token live before
    async with _client(app) as c:                        # admin
        r = await c.delete("/api/admin/users/eve")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["deleted"] == "eve"
    assert sorted(body["leftover_paths"]) == sorted(str(d) for d in dirs)
    assert all(d.is_dir() for d in dirs)                 # left for manual cleanup
    assert app.state.chats.get("c1") is None             # saved chats gone
    assert (await me_with_bearer()).status_code == 401   # old token dead
    # a recreated account with the same name is not authenticable with it
    app.state.users.create("eve", "pw2")
    assert (await me_with_bearer()).status_code == 401


# ---- inline output preview ----------------------------------------------------
def _stage_output(tmp_path, name, content=b"x", owner="admin"):
    """Write a minimal outputs/<run_id>/ with a manifest, as a finished run
    would have left behind (kind 'file' -> files/<name>)."""
    rid = uuid.uuid4().hex
    d = tmp_path / "outputs" / rid
    (d / "files").mkdir(parents=True)
    (d / "files" / name).write_bytes(content)
    (d / "manifest.json").write_text(json.dumps(
        {"owner": owner, "name": name, "kind": "file", "size": len(content)}))
    return rid


@pytest.mark.asyncio
async def test_inline_preview_sandboxes_html_and_svg(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    html = _stage_output(tmp_path, "page.html", b"<script>alert(1)</script>")
    svg = _stage_output(tmp_path, "icon.svg", b'<svg onload="alert(1)"/>')
    txt = _stage_output(tmp_path, "notes.txt", b"hello")
    async with _client(app) as c:
        for rid, ctype in ((html, "text/html"), (svg, "image/svg+xml")):
            r = await c.get(f"/api/output/{rid}", params={"inline": 1})
            assert r.status_code == 200
            assert r.headers["content-type"].startswith(ctype)
            assert r.headers["content-security-policy"] == "sandbox"
            assert r.headers["content-disposition"] == "inline"
            # the download (non-inline) path is unchanged
            r = await c.get(f"/api/output/{rid}")
            assert "content-security-policy" not in r.headers
        # other inline media types are served as before — no sandbox header
        r = await c.get(f"/api/output/{txt}", params={"inline": 1})
        assert r.status_code == 200
        assert "content-security-policy" not in r.headers


# ---- global bearer token ------------------------------------------------------
@pytest.mark.asyncio
async def test_global_web_token_bearer_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_WEB_TOKEN", "s3cret-token")
    app = _app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/me", headers={"Authorization": "Bearer s3cret-token"})
        assert r.status_code == 200
        assert r.json()["username"] == "_token" and r.json()["is_admin"]
        for wrong in ("s3cret-tok", "s3cret-tokenX", ""):
            r = await c.get("/api/me", headers={"Authorization": f"Bearer {wrong}"})
            assert r.status_code == 401, wrong
        # raw non-ASCII bytes must not 500 the comparison (starlette decodes
        # headers as latin-1, so the bearer str can carry non-ASCII chars)
        r = await c.get("/api/me", headers={"Authorization": b"Bearer s\xc3\xa9cret"})
        assert r.status_code == 401


# ---- username validation (usernames become path components) -------------------
@pytest.mark.asyncio
async def test_admin_create_user_validates_username(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with _client(app) as c:
        for bad in ("../x", "..", ".dotfile", "", "a/b", "a\\b", "white space",
                    "-leading-dash", "x" * 65):
            r = await c.post("/api/admin/users",
                             json={"username": bad, "password": "password1"})
            assert r.status_code == 400, bad
            assert app.state.users.get(bad) is None
        r = await c.post("/api/admin/users",
                         json={"username": "ok.user-1_2", "password": "password1"})
        assert r.status_code == 200
        assert app.state.users.get("ok.user-1_2") is not None


# ---- project file write onto a directory path ---------------------------------
@pytest.mark.asyncio
async def test_project_write_file_directory_path_is_4xx(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with _client(app) as c:
        pid = (await c.post("/api/projects", json={"name": "P"})).json()["id"]
        # empty path resolves to the project root itself (used to 500)
        r = await c.put(f"/api/projects/{pid}/file", params={"path": ""}, content=b"x")
        assert r.status_code == 400
        r = await c.put(f"/api/projects/{pid}/file", content=b"x")   # missing param
        assert r.status_code == 422
        assert (await c.post(f"/api/projects/{pid}/mkdir",
                             json={"path": "sub"})).status_code == 200
        r = await c.put(f"/api/projects/{pid}/file", params={"path": "sub"},
                        content=b"x")                                # existing dir
        assert r.status_code == 400


# ---- llama-server /metrics parsing (admin process stats) ----

def test_parse_llama_metrics_keeps_plain_counters():
    from web.server import _parse_llama_metrics
    text = (
        "# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed\n"
        "# TYPE llamacpp:prompt_tokens_total counter\n"
        "llamacpp:prompt_tokens_total 32094\n"
        "llamacpp:tokens_predicted_seconds_total 61.714\n"
        "llamacpp:requests_processing 0\n"
        "other_metric 5\n"
        "llamacpp:broken notanumber\n"
    )
    m = _parse_llama_metrics(text)
    assert m == {"prompt_tokens_total": 32094.0,
                 "tokens_predicted_seconds_total": 61.714,
                 "requests_processing": 0.0}
    assert _parse_llama_metrics("") == {}


# ---- LiteLLM status probe must use the unauthenticated liveness route --------
@pytest.mark.asyncio
async def test_admin_status_probes_litellm_liveness(tmp_path, monkeypatch):
    """The admin page's status card probed litellm_base + /health with no
    Authorization header; LiteLLM's /health requires a key, so every admin-page
    load made the proxy log an auth ERROR ("No api key passed in"). The probe
    now targets the unauthenticated /health/liveliness route."""
    app = _app(tmp_path, monkeypatch)
    urls = []

    class _Resp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **kw): pass

        async def __aenter__(self): return self

        async def __aexit__(self, *a): return False

        async def get(self, url, **kw):
            urls.append(url)
            return _Resp()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/login", json={"username": "admin", "password": "pw"})
        assert r.status_code == 200
        # Patch only the module attribute: constructions *inside the app* get the
        # fake, while this already-created test client keeps the real class.
        monkeypatch.setattr(web.server.httpx, "AsyncClient", _FakeClient)
        r = await c.get("/api/admin/status")
        assert r.status_code == 200
    assert any(u.endswith("/health/liveliness") for u in urls)
    assert not any(u.endswith("/health") for u in urls)


# ---- project file download: inline preview mode ------------------------------
@pytest.mark.asyncio
async def test_project_download_inline_serves_media_type(tmp_path, monkeypatch):
    """?inline=1 (the file explorer's image preview) serves the file with the
    media type guessed from its suffix and no attachment disposition; the plain
    download path keeps octet-stream + attachment."""
    app = _app(tmp_path, monkeypatch)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    async with _client(app) as c:
        pid = (await c.post("/api/projects", json={"name": "p"})).json()["id"]
        r = await c.put(f"/api/projects/{pid}/file", params={"path": "pic.png"},
                        content=png)
        assert r.status_code == 200
        r = await c.get(f"/api/projects/{pid}/download",
                        params={"path": "pic.png", "inline": 1})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/png")
        assert "attachment" not in r.headers.get("content-disposition", "")
        r = await c.get(f"/api/projects/{pid}/download", params={"path": "pic.png"})
        assert r.headers["content-type"] == "application/octet-stream"
        assert "attachment" in r.headers["content-disposition"]
