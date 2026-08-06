# The testing harness (`test.run`)

This is the capability that lets the orchestrator **test code the way a developer
does**: write a small test that drives the target *in-process* (no network, no
live server), mock anything external (the model, HTTP, the clock), run it against
a venv that actually has the test deps, and read structured pass/fail back. It is
the same loop a human runs — write test → run → read failures → fix → rerun — made
available to the agent as a tool.

It is a **separate concern from the web console's features** (auth, 2FA, tool
toggles): those are things the app *does*; this is how the agent *verifies* code.

## Why in-process + mocks (the core idea)

FastAPI speaks ASGI. Instead of starting `uvicorn` on a port, `httpx` can talk
that contract directly in memory via `ASGITransport`, so a test sends a *real*
request through real routing, middleware, validation and cookies, and gets a real
response — with no socket and no server. External systems the box can't reach in a
test (the local model behind LiteLLM, cloud providers) are swapped at their
boundary: replace `runtime.run` with a coroutine that records its arguments and
returns immediately; point a status probe at an `httpx` stub. Everything *you* wrote
runs for real; only the outside world is faked. The result is fast, deterministic,
and hermetic.

## The tool

`test.run` (private, requires confirmation):

| arg | meaning |
|-----|---------|
| `test` | contents of a single test file (written as `test_main.py`) |
| `files` | map of `path -> contents` for multi-file tests (modules, `conftest.py`, fixtures) |
| `command` | override the run command (default `pytest -q`); use for `-k`/`-x`/markers or a plain script |
| `pythonpath` | extra `PYTHONPATH` entries (the project root is always included) |
| `timeout_s` | quick-mode wall-clock cap (default 120) |
| `detached` | run as a background job for long suites; returns a `job_id` |
| `name` | label for the detached job |

**Quick mode** (default) runs pytest as a bounded subprocess and returns parsed
counts inline: `{passed, failed, errors, skipped, ok, returncode, duration_s,
stdout, stderr}`. **Detached mode** hands the same command to `job.start`, so the
suite runs in the background and is tracked by `job.status` / `job.logs` /
`job.list` like any other job (a 0 exit code means it passed).

Tests can import the orchestrator's own code (`web.server`, `runtime.*`,
`tools.*`) because the project root is on `PYTHONPATH`.

## Setup (once)

Point `tools.test.python` at a venv that has the deps and install them:

```bash
uv pip install -r requirements.txt -r requirements-web.txt -r requirements-test.txt
```

## The pattern to write (worked example)

A canonical quick check of the web layer — create the app against a throwaway
config, mock the model, exercise a real endpoint. (Inside the repo's own
`tests/`, don't copy this harness — use the shared `web_app` / `web_client` /
`record_run` fixtures from `tests/conftest.py` instead.)

```python
# test_login.py — driven by test.run in quick mode
import os, tempfile
from pathlib import Path
import yaml, httpx, pytest
import web                       # importable: project root is on PYTHONPATH
ROOT = Path(web.__file__).resolve().parent.parent   # locate files via the package,
                                                     # NOT the cwd (test.run isolates it)

def _app():
    base = tempfile.mkdtemp()
    os.makedirs(f"{base}/config"); os.makedirs(f"{base}/prompts")
    cfg = yaml.safe_load(open(ROOT / "config/runtime.yaml"))
    cfg["trace"]["db_path"] = f"{base}/trace.db"
    cfg["orchestrator"]["system_prompt"] = "prompts/orchestrator.md"
    cfg["web"] = {"chats_db": f"{base}/chats.db", "users_db": f"{base}/users.db"}
    open(f"{base}/prompts/orchestrator.md", "w").write("P")
    yaml.safe_dump(cfg, open(f"{base}/config/runtime.yaml", "w"))
    os.environ.update(ORCH_ADMIN_USER="admin", ORCH_ADMIN_PASSWORD="pw",
                      ORCH_SESSION_SECRET="t")
    from web.server import create_app
    app = create_app(f"{base}/config/runtime.yaml")
    async def fake_run(msg, **kw):      # mock the model — no LiteLLM needed
        return {}
    app.state.runtime.run = fake_run
    return app

@pytest.mark.asyncio
async def test_login_then_authenticated():
    app = _app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/api/me")).status_code == 401          # gated
        r = await c.post("/api/login", json={"username": "admin", "password": "pw"})
        assert r.status_code == 200                                  # cookie set
        assert (await c.get("/api/me")).json()["username"] == "admin"
```

Hand that to `test.run` as `test=<contents>`; quick mode returns
`{"passed": 1, "ok": true, ...}`.

### Idioms worth reusing

- **Mock at the boundary.** Swap `app.state.runtime.run` for a recorder coroutine;
  assert on what it was called with (e.g. that a disabled tool was filtered out)
  rather than invoking a model.
- **Throwaway state.** Always point `web.*`/`trace` DB paths at a temp dir so a
  test never touches live data.
- **Deterministic time.** For anything time-based (TOTP, expiry), compute the
  expected value with the same helper the code uses, at `time.time()`, so there's
  no clock flakiness.
- **Two failure modes.** A gated API returns `401`; a gated page redirects `302`
  to `/login`. Assert the right one.
- **Long suites → `detached=true`**, then poll `job.status`.

## What it can and can't tell you

It exercises the real request path and your real logic. It cannot tell you that a
*live* external system agrees — that the actual LiteLLM endpoint streams usage, or
that a phone authenticator's code is accepted. Those stay manual, live checks. The
harness is for everything below that line, which is most of it.
