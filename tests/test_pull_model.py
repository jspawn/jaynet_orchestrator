"""pull-model CLI: the thin wrapper around runtime/hf_pull keeps its output
contract (menu, error listings, MODEL_PATH as the LAST stdout line —
quickstart.sh greps it). Core logic is tested in test_hf_pull.py.

The script has no package, so it is loaded by path. Network is monkeypatched
away at the hf_pull level.
"""
import importlib.machinery
import importlib.util
import io
from pathlib import Path

import pytest

from runtime import hf_pull

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "pull-model"

# Extensionless script — spec_from_file_location alone can't pick a loader.
loader = importlib.machinery.SourceFileLoader("pull_model", str(SCRIPT))
spec = importlib.util.spec_from_file_location("pull_model", SCRIPT, loader=loader)
pm = importlib.util.module_from_spec(spec)
loader.exec_module(pm)

SIBLINGS = [
    {"rfilename": "README.md"},
    {"rfilename": "model-Q6_K.gguf", "size": 6_000_000_000},
    {"rfilename": "model-Q4_K_M.gguf", "size": 4_000_000_000},
    {"rfilename": "model-Q8_0.gguf"},                       # size may be absent
]


class _FakeResponse:
    """Minimal urlopen stand-in: context manager + headers + chunked read."""
    def __init__(self, body):
        self._buf = io.BytesIO(body if isinstance(body, bytes)
                               else __import__("json").dumps(body).encode())
        self.headers = {"Content-Length": str(len(self._buf.getvalue()))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self._buf.read(n)


def _patch_api(monkeypatch, payload):
    monkeypatch.setattr(hf_pull.urllib.request, "urlopen",
                        lambda url, timeout: _FakeResponse(payload))


def test_main_prints_model_path_last(monkeypatch, tmp_path, capsys):
    body = b"w"
    def fake_urlopen(req, timeout):
        url = getattr(req, "full_url", req)     # Request object or plain str
        if "/api/" in url:
            return _FakeResponse({"siblings": SIBLINGS})
        return _FakeResponse(body)
    monkeypatch.setattr(hf_pull.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(hf_pull.paths, "MODELS_DIR", tmp_path)

    pm.main(["org/repo", "model-Q8_0.gguf"])

    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-1] == f"MODEL_PATH={tmp_path}/org/repo/model-Q8_0.gguf"
    assert (tmp_path / "org" / "repo" / "model-Q8_0.gguf").exists()


def test_main_unknown_file_lists_candidates(monkeypatch, capsys):
    _patch_api(monkeypatch, {"siblings": SIBLINGS})
    with pytest.raises(SystemExit) as e:
        pm.main(["org/repo", "model-Q5_K_M.gguf"])
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "not in 3 .gguf file(s)" in err and "model-Q4_K_M.gguf" in err


def test_main_bad_repo_exits_with_error(monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        pm.main(["not-a-repo", "x.gguf"])
    assert "invalid repo id" in str(e.value.code)


def test_yes_without_file_lists_and_exits_2(monkeypatch, capsys):
    _patch_api(monkeypatch, {"siblings": SIBLINGS})
    with pytest.raises(SystemExit) as e:
        pm.main(["org/repo", "--yes"])
    assert e.value.code == 2
    out = capsys.readouterr().out
    assert "model-Q6_K.gguf" in out and "6.00 GB" in out
    assert "README.md" not in out
