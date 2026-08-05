"""Regression tests for the external-audit fixes (S3/S4/S6/S7/S8/S9/S10, B5/B6).

Each test names the audit item it covers. Self-contained; uses the shared
fixtures from conftest (run/ctx/git_repo/project/web_app/web_client).
"""
from __future__ import annotations

import ipaddress
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

from conftest import run
from runtime.events import EventBus
from runtime.outputs import stage_and_bundle
from tools.fs.ops import FsRead
from tools.git.remote import GitFetch
from tools.research.loop import _source_score
from tools.web.search_fetch import _ip_blocked, ssrf_refusal


# ---------------------------------------------------------------- S3 (outputs)
def test_deliver_skips_nested_symlink_escaping_source_root(tmp_path):
    """A dir handed to deliver.files must not leak symlink targets that live
    outside the copied tree (copytree's default dereferences them)."""
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "ok.txt").write_text("fine")
    (ws / "inner.txt").write_text("x")
    os.symlink(secret, ws / "sub" / "leak")        # nested, escapes the tree
    os.symlink("inner.txt", ws / "keeplink")       # in-tree link -> preserved

    outputs = tmp_path / "outputs"
    m = stage_and_bundle(outputs, "run1", None, [str(ws)], None, 10 * 1024 * 1024)
    assert m["kind"] == "targz"

    staged = outputs / "run1" / "files" / "ws"
    assert (staged / "sub" / "ok.txt").read_text() == "fine"
    assert not (staged / "sub" / "leak").exists()       # skipped, not dereferenced
    assert (staged / "keeplink").is_symlink()           # in-tree links kept as links

    with tarfile.open(outputs / "run1" / "delivery.tar.gz") as tar:
        blob = b"".join(tar.extractfile(mem).read()
                        for mem in tar.getmembers() if mem.isfile())
    assert b"TOP-SECRET" not in blob


# ---------------------------------------------------------------- S4 (git.fetch)
def _git_cfg(repo: Path) -> dict:
    return {"tools": {"git": {"allowed_roots": [str(repo)],
                              "default_repo": str(repo)}},
            "trace": {}}


@pytest.fixture
def repo_with_origin(git_repo, tmp_path):
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "-C", str(git_repo), "remote", "add", "origin",
                    str(bare)], check=True)
    return git_repo


def test_fetch_rejects_url_shaped_remotes(git_repo, ctx, tmp_path):
    c = lambda: ctx(config=_git_cfg(git_repo))
    pwned = tmp_path / "pwned"
    r = run(GitFetch().execute({"remote": f"ext::touch {pwned}"}, c()))
    assert r.status == "error" and "unsafe remote" in r.error
    assert not pwned.exists()                          # nothing executed
    for bad in ("https://evil.example/x.git", "git@evil.example:x.git"):
        r = run(GitFetch().execute({"remote": bad}, c()))
        assert r.status == "error" and "unsafe remote" in r.error


def test_fetch_rejects_unconfigured_remote_name(git_repo, ctx):
    r = run(GitFetch().execute({"remote": "upstream"},
                               ctx(config=_git_cfg(git_repo))))
    assert r.status == "error" and "unknown remote" in r.error


def test_fetch_accepts_configured_remote(repo_with_origin, ctx):
    r = run(GitFetch().execute({}, ctx(config=_git_cfg(repo_with_origin))))
    assert r.status == "ok"


# ---------------------------------------------------------------- S6 (code.deps)
def test_deps_run_scrubs_inherited_secrets(monkeypatch, tmp_path):
    from tools.code import deps
    monkeypatch.setenv("TAVILY_API_KEY", "sk-secret")
    rc, out, _ = run(deps._run(
        [sys.executable, "-c", "import os; print(os.environ.get('TAVILY_API_KEY'))"],
        tmp_path))
    assert rc == 0 and out.strip() == "None"


def test_deps_is_not_read_only():
    from tools.code.deps import CodeDeps
    assert CodeDeps.read_only is False   # it creates venvs / installs packages


# ---------------------------------------------------------------- S7 (SSRF)
def test_ssrf_blocks_ipv4_mapped_ipv6():
    assert _ip_blocked(ipaddress.ip_address("::ffff:100.64.0.1")) == "carrier-grade NAT"
    assert _ip_blocked(ipaddress.ip_address("::ffff:127.0.0.1")) == "loopback"
    assert _ip_blocked(ipaddress.ip_address("::ffff:169.254.0.1")) is not None
    # Plain IPv4 and public IPv6 behaviour unchanged.
    assert _ip_blocked(ipaddress.ip_address("127.0.0.1")) == "loopback"
    assert _ip_blocked(ipaddress.ip_address("2001:4860:4860::8888")) is None
    assert run(ssrf_refusal("::ffff:127.0.0.1")) == "loopback"


# ---------------------------------------------------------------- S8 (research)
def test_research_source_trust_is_label_aligned():
    assert _source_score("https://arxiv.org/abs/1234", {}) == 0.9
    assert _source_score("https://export.arxiv.org/abs/1234", {}) == 0.9
    assert _source_score("https://evil-arxiv.org/abs/1234", {}) == 0.5  # unknown
    assert _source_score("https://example.gov/x", {}) == 0.9            # ".gov" tier
    assert _source_score("https://notgov.com/x", {}) == 0.5
    deny = {"deny": ["bad.com"]}
    assert _source_score("https://sub.bad.com/x", deny) == 0.0
    assert _source_score("https://notbad.com/x", deny) == 0.5


# ---------------------------------------------------------------- S9 (uploads)
def test_upload_rejects_oversized_content_length_before_read(web_app, web_client):
    app = web_app()                      # default max_upload_mb is 25
    async def go():
        async with web_client(app) as c:
            r = await c.post("/api/upload?filename=x.bin", content=b"tiny",
                             headers={"content-length": str(26 * 1024 * 1024)})
            assert r.status_code == 413
    run(go())


# ---------------------------------------------------------------- S10 (events)
def test_event_bus_bounds_subscriber_queue_drop_oldest():
    bus = EventBus(max_queue=10)
    q = bus.subscribe("r")

    async def pub():
        for i in range(50):
            await bus.publish("r", {"seq": i})
    run(pub())                           # no QueueFull, publisher unaffected

    assert q.qsize() == 10
    events = [q.get_nowait() for _ in range(10)]
    assert events[0]["seq"] == 40 and events[-1]["seq"] == 49  # oldest dropped


# ---------------------------------------------------------------- B5 (browser)
def test_browser_deliver_removes_tmp_dir(ctx, tmp_path):
    from tools.browser.tools import _deliver
    tmp_root = Path(tempfile.gettempdir())
    before = set(tmp_root.glob("browser-*"))
    c = ctx(config={"web": {"outputs_dir": str(tmp_path / "outputs")}})
    m = run(_deliver(c, b"png-bytes", "shot.png"))
    assert m["size"] == len(b"png-bytes")
    assert set(tmp_root.glob("browser-*")) == before   # nothing leaked


# ---------------------------------------------------------------- B6 (fs.read)
def test_fs_read_truncated_bytes_flag(project, ctx):
    (project / "big.txt").write_text("x" * 5000)
    r = run(FsRead().execute({"path": "big.txt", "max_bytes": 100}, ctx()))
    assert r.status == "ok" and r.result["truncated_bytes"] is True
    r = run(FsRead().execute({"path": "app.py"}, ctx()))
    assert r.status == "ok" and r.result["truncated_bytes"] is False
