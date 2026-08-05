"""Admin backup/restore: GET /api/admin/backup produces a consistent .tar.gz
of the data stores; POST /api/admin/restore validates uploads before swapping.

Endpoint tests drive FastAPI in-process (see docs/testing-harness.md); the
web_app fixture points the data dir at a throwaway tmp_path.
"""
import io
import sqlite3
import tarfile

import pytest


def _targz(names: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in names.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_backup_returns_targz_with_stores(web_app, web_client, tmp_path):
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/backup")
    assert r.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        names = tar.getnames()
        # the seeded stores survive; live WAL/SHM sidecars never do
        assert "users.db" in names and "chats.db" in names
        assert not any(n.endswith(("-wal", "-shm")) for n in names)
        # the snapshot is a real, queryable sqlite db (online backup API)
        tar.extract("users.db", tmp_path)
    conn = sqlite3.connect(tmp_path / "users.db")
    try:
        rows = conn.execute("SELECT username FROM users").fetchall()
    finally:
        conn.close()
    assert ("admin",) in rows


@pytest.mark.asyncio
async def test_restore_rejects_garbage(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        # not a tarball at all
        r = await c.post("/api/admin/restore",
                         files={"file": ("x.tar.gz", b"definitely not a tarball",
                                         "application/gzip")})
        assert r.status_code == 400
        # valid tar.gz, but no recognized data store inside
        r = await c.post("/api/admin/restore",
                         files={"file": ("x.tar.gz", _targz({"readme.txt": b"hi"}),
                                         "application/gzip")})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_restore_swaps_store_and_requires_restart(web_app, web_client, tmp_path):
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/backup")
        assert r.status_code == 200
        r = await c.post("/api/admin/restore",
                         files={"file": ("backup.tar.gz", r.content,
                                         "application/gzip")})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "restart_required": True}
    # the swap really happened and no restore temp files were left behind
    assert (tmp_path / "users.db").exists()
    assert not list(tmp_path.glob("*.restore-tmp"))
