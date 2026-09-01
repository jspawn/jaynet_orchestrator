"""_backend_recovered: transient backend outages (litellm restart ~10-30s)
get a grace window before the suite aborts. A dead backend must still trip
the brake after the probes run out."""
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from runtime.eval_runner import _backend_recovered


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.mark.asyncio
async def test_recovers_when_proxy_answers():
    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), _Ok)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cfg = {"orchestrator": {"litellm_base": f"http://127.0.0.1:{port}"}}
        assert await _backend_recovered(cfg, attempts=1, delay_s=0) is True
    finally:
        srv.shutdown()


@pytest.mark.asyncio
async def test_dead_backend_exhausts_probes():
    cfg = {"orchestrator": {"litellm_base": f"http://127.0.0.1:{_free_port()}"}}
    assert await _backend_recovered(cfg, attempts=2, delay_s=0) is False


@pytest.mark.asyncio
async def test_missing_base_is_down():
    assert await _backend_recovered({}, attempts=1, delay_s=0) is False
