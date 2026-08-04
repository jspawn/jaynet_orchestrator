"""pull-model: GGUF filtering, exact-match selection, URL building, download layout.

The script has no package, so it is loaded by path (same trick as other script
tests). urllib is monkeypatched — no network.
"""
import importlib.machinery
import importlib.util
import io
from pathlib import Path

import pytest

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
    {"rfilename": "model.gguf.part"},                       # not a .gguf
    {"rfilename": "model-Q8_0.gguf"},                       # size may be absent
]


def test_gguf_files_filters_and_sorts():
    files = pm.gguf_files(SIBLINGS)
    assert [n for n, _ in files] == ["model-Q4_K_M.gguf", "model-Q6_K.gguf",
                                     "model-Q8_0.gguf"]
    assert dict(files)["model-Q6_K.gguf"] == 6_000_000_000
    assert dict(files)["model-Q8_0.gguf"] is None           # size optional


def test_select_file_exact_match():
    files = pm.gguf_files(SIBLINGS)
    assert pm.select_file(files, "model-Q6_K.gguf") == "model-Q6_K.gguf"


def test_select_file_miss_lists_candidates(capsys):
    files = pm.gguf_files(SIBLINGS)
    with pytest.raises(SystemExit) as e:
        pm.select_file(files, "model-Q5_K_M.gguf")
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "not in 3 .gguf file(s)" in err and "model-Q4_K_M.gguf" in err


def test_resolve_url_quotes_filename():
    url = pm.resolve_url("org/repo", "sub dir/my model.gguf")
    assert url == ("https://huggingface.co/org/repo/resolve/main/"
                   "sub%20dir/my%20model.gguf")


def test_target_dir_layout(monkeypatch, tmp_path):
    monkeypatch.setattr(pm.paths, "MODELS_DIR", tmp_path)
    assert pm.target_dir("org/repo") == tmp_path / "org" / "repo"


class _FakeResponse:
    """Minimal urlopen stand-in: context manager + headers + chunked read."""
    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self._buf.read(n)


def test_download_streams_part_then_renames(monkeypatch, tmp_path, capsys):
    body = b"x" * (3 * (1 << 20))      # 3 MiB — exercises the chunk loop
    monkeypatch.setattr(pm.urllib.request, "urlopen", lambda url, timeout: _FakeResponse(body))
    monkeypatch.setattr(pm.paths, "MODELS_DIR", tmp_path)

    dest = pm.download("org/repo", "m.gguf", pm.target_dir("org/repo"))

    assert dest == tmp_path / "org" / "repo" / "m.gguf"
    assert dest.read_bytes() == body
    assert not dest.with_name("m.gguf.part").exists()       # .part renamed away
    assert "100.0%" in capsys.readouterr().out


def test_main_prints_model_path_last(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(pm, "fetch_siblings", lambda repo: SIBLINGS)
    monkeypatch.setattr(pm.urllib.request, "urlopen", lambda url, timeout: _FakeResponse(b"w"))
    monkeypatch.setattr(pm.paths, "MODELS_DIR", tmp_path)

    pm.main(["org/repo", "model-Q8_0.gguf"])

    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-1] == f"MODEL_PATH={tmp_path}/org/repo/model-Q8_0.gguf"
    assert (tmp_path / "org" / "repo" / "model-Q8_0.gguf").exists()


def test_yes_without_file_lists_and_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(pm, "fetch_siblings", lambda repo: SIBLINGS)
    with pytest.raises(SystemExit) as e:
        pm.main(["org/repo", "--yes"])
    assert e.value.code == 2
    out = capsys.readouterr().out
    assert "model-Q6_K.gguf" in out and "6.00 GB" in out
