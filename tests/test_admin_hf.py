"""HF downloader admin routes — the preset-suggestion route must keep the
live-VRAM probe (sync smi subprocess, up to 20s worst case) OFF the event
loop. Regression pin for audit #18/#19 C1: `port=_next_free_port()` was
evaluated as a kwarg on the loop before asyncio.to_thread started."""
import threading

import pytest


@pytest.mark.asyncio
async def test_preset_suggestion_probes_off_the_loop(web_app, web_client,
                                                     monkeypatch):
    from runtime import hf_pull, serving

    loop_thread = threading.get_ident()
    probe_threads = []
    suggest_threads = []

    monkeypatch.setattr(
        serving, "read_vram",
        lambda ctx: probe_threads.append(threading.get_ident()) or [])

    def fake_suggest(repo, file, port=None):
        suggest_threads.append(threading.get_ident())
        return {"repo": repo, "file": file, "port": port}

    monkeypatch.setattr(hf_pull, "suggest_preset", fake_suggest)

    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/hf/preset-suggestion",
                        params={"repo": "x/y", "file": "m.gguf"})
    assert r.status_code == 200
    assert probe_threads and suggest_threads
    assert all(t != loop_thread for t in probe_threads + suggest_threads)
