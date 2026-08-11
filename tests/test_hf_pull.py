"""runtime/hf_pull.py — validation, listing, streaming, jobs, suggestions.

Network is monkeypatched away (_FakeResponse / fake urlopen). MODELS_DIR is
redirected to tmp_path per test.
"""
import io
import json
import time

import pytest

from runtime import hf_pull


class _FakeResponse:
    def __init__(self, body):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self._buf = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(self._buf.getvalue()))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self._buf.read(n)


def _patch_urlopen(monkeypatch, payload):
    monkeypatch.setattr(hf_pull.urllib.request, "urlopen",
                        lambda url, timeout: _FakeResponse(payload))


@pytest.fixture(autouse=True)
def _models_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(hf_pull.paths, "MODELS_DIR", tmp_path)
    return tmp_path


# ---- validation --------------------------------------------------------------

def test_validate_repo():
    assert hf_pull.validate_repo(" org/repo-2.5 ") == "org/repo-2.5"
    for bad in ("", "noslash", "a/b/c", "../evil", "a//b", "-lead/x"):
        with pytest.raises(hf_pull.HfError):
            hf_pull.validate_repo(bad)


def test_validate_filename():
    assert hf_pull.validate_filename("m-Q6_K.gguf") == "m-Q6_K.gguf"
    assert hf_pull.validate_filename("sub dir/my model.gguf")  # spaces ok
    assert hf_pull.validate_filename("tools.jinja")            # templates too
    for bad in ("", "model.gguf.part", "../x.gguf", "a/../x.gguf",
                "noext.bin", ".gguf", "x.jinja.part"):
        with pytest.raises(hf_pull.HfError):
            hf_pull.validate_filename(bad)


# ---- listing / urls / paths ---------------------------------------------------

SIBLINGS = [
    {"rfilename": "README.md"},
    {"rfilename": "model-Q6_K.gguf", "size": 6_000_000_000},
    {"rfilename": "model-Q4_K_M.gguf", "size": 4_000_000_000},
    {"rfilename": "model.gguf.part"},
    {"rfilename": "model-Q8_0.gguf"},
    {"rfilename": "qwen3_tools.jinja", "size": 12_000},
]


def test_list_gguf_filters_and_sorts(monkeypatch):
    _patch_urlopen(monkeypatch, {"siblings": SIBLINGS})
    files = hf_pull.list_gguf("org/repo")
    assert [n for n, _ in files] == ["model-Q4_K_M.gguf", "model-Q6_K.gguf",
                                     "model-Q8_0.gguf"]
    assert dict(files)["model-Q8_0.gguf"] is None           # size optional


def test_list_files_includes_templates(monkeypatch):
    _patch_urlopen(monkeypatch, {"siblings": SIBLINGS})
    files = hf_pull.list_files("org/repo")
    kinds = {n: k for n, _, k in files}
    assert kinds["qwen3_tools.jinja"] == "jinja"
    assert kinds["model-Q6_K.gguf"] == "gguf"
    assert "README.md" not in kinds and "model.gguf.part" not in kinds


def test_resolve_url_quotes_filename():
    url = hf_pull.resolve_url("org/repo", "sub dir/my model.gguf")
    assert url == ("https://huggingface.co/org/repo/resolve/main/"
                   "sub%20dir/my%20model.gguf")


def test_target_path_confined(_models_dir):
    p = hf_pull.target_path("org/repo", "m.gguf")
    assert p == _models_dir / "org" / "repo" / "m.gguf"
    with pytest.raises(hf_pull.HfError):
        hf_pull.target_path("org/repo", "../escape.gguf")


# ---- streaming -----------------------------------------------------------------

def test_stream_download_part_then_rename(monkeypatch, _models_dir):
    body = b"x" * (3 * (1 << 20))          # 3 MiB — exercises the chunk loop
    _patch_urlopen(monkeypatch, body)
    seen = []
    dest = hf_pull.stream_download("org/repo", "m.gguf",
                                   hf_pull.target_path("org/repo", "m.gguf"),
                                   progress=lambda d, t: seen.append((d, t)))
    assert dest.read_bytes() == body
    assert not dest.with_name("m.gguf.part").exists()
    assert seen[-1] == (len(body), len(body))


def test_stream_download_cancel_removes_part(monkeypatch, _models_dir):
    _patch_urlopen(monkeypatch, b"x" * (3 * (1 << 20)))
    calls = {"n": 0}
    def cancelled():                        # cancel after the first chunk
        calls["n"] += 1
        return calls["n"] > 1
    with pytest.raises(hf_pull.HfError, match="cancelled"):
        hf_pull.stream_download("org/repo", "m.gguf",
                                hf_pull.target_path("org/repo", "m.gguf"),
                                cancelled=cancelled)
    assert not (_models_dir / "org" / "repo" / "m.gguf.part").exists()


# ---- job manager ---------------------------------------------------------------

def test_job_lifecycle(monkeypatch, _models_dir):
    _patch_urlopen(monkeypatch, b"data")
    job = hf_pull.start_job("org/repo", "m.gguf")
    assert job["status"] == "running"
    for _ in range(100):                    # thread finishes quickly
        if hf_pull.jobs()[0]["status"] != "running":
            break
        time.sleep(0.01)
    done = hf_pull.jobs()[0]
    assert done["status"] == "done"
    assert (_models_dir / "org" / "repo" / "m.gguf").read_bytes() == b"data"
    assert "_cancel" not in done                       # no internals leak
    # duplicate: target now exists → refused
    with pytest.raises(hf_pull.HfError, match="already exists"):
        hf_pull.start_job("org/repo", "m.gguf")
    assert hf_pull.dismiss_job(done["id"]) is True
    assert hf_pull.jobs() == []


def test_job_cancel(monkeypatch, _models_dir):
    # a large fake body so the job is still running when we cancel
    _patch_urlopen(monkeypatch, b"x" * (64 * (1 << 20)))
    job = hf_pull.start_job("org/repo", "big.gguf")
    with pytest.raises(hf_pull.HfError, match="already downloading"):
        hf_pull.start_job("org/repo", "big.gguf")
    assert hf_pull.cancel_job(job["id"]) is not None
    for _ in range(200):
        st = hf_pull.jobs()[0]["status"]
        if st != "running":
            break
        time.sleep(0.01)
    assert hf_pull.jobs()[0]["status"] == "cancelled"
    assert not (_models_dir / "org" / "repo" / "big.gguf").exists()
    assert hf_pull.dismiss_job(job["id"]) is True


def test_dismiss_running_refused(monkeypatch, _models_dir):
    _patch_urlopen(monkeypatch, b"x" * (64 * (1 << 20)))
    job = hf_pull.start_job("org/repo", "run.gguf")
    assert hf_pull.dismiss_job(job["id"]) is False
    hf_pull.cancel_job(job["id"])


# ---- preset suggestion --------------------------------------------------------------

def test_suggest_name():
    assert hf_pull.suggest_name("Qwen3-0.6B-Q8_0.gguf") == "qwen3-0-6b-q8-0"
    assert hf_pull.suggest_name("model.Q4_K_M.gguf").startswith("model")
    assert hf_pull.suggest_name(".gguf") == "pulled-model"  # fallback
    assert len(hf_pull.suggest_name("x" * 200 + ".gguf")) <= 64


def test_suggest_preset(monkeypatch, _models_dir):
    dest = hf_pull.target_path("org/repo", "My-Model-Q6_K.gguf")
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"z" * (1 << 30))       # 1 GiB → vram ~1.1
    monkeypatch.setattr(hf_pull, "list_files",
                        lambda repo: [("My-Model-Q6_K.gguf", None, "gguf")])
    s = hf_pull.suggest_preset("org/repo", "My-Model-Q6_K.gguf", port=8100)
    assert s["name"] == "my-model-q6-k"
    assert s["vram_gib"] == 1.1
    assert s["port"] == 8100
    assert f"MODEL_PATH={dest}" in s["conf"]
    assert "PORT=8100" in s["conf"] and "ALIAS=my-model-q6-k" in s["conf"]
    assert "MMPROJ" not in s["conf"] and "TOOLS_TEMPLATE" not in s["conf"]
    assert "mmproj" not in s and "chat_template" not in s


def test_suggest_preset_wires_mmproj_and_template(monkeypatch, _models_dir):
    siblings = [("My-VL-Q6_K.gguf", None, "gguf"),
                ("mmproj-F16.gguf", None, "gguf"),
                ("vl_tools.jinja", None, "jinja")]
    monkeypatch.setattr(hf_pull, "list_files", lambda repo: siblings)
    s = hf_pull.suggest_preset("org/repo", "My-VL-Q6_K.gguf", port=8100)
    mp = hf_pull.target_path("org/repo", "mmproj-F16.gguf")
    tp = hf_pull.target_path("org/repo", "vl_tools.jinja")
    assert f"MMPROJ={mp}" in s["conf"] and "MMPROJ_OFFLOAD=on" in s["conf"]
    assert f"TOOLS_TEMPLATE={tp}" in s["conf"]
    assert s["mmproj"] == {"file": "mmproj-F16.gguf", "downloaded": False}
    assert s["chat_template"] == {"file": "vl_tools.jinja",
                                  "downloaded": False}
    # downloaded siblings flip the flag
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_bytes(b"x")
    s2 = hf_pull.suggest_preset("org/repo", "My-VL-Q6_K.gguf", port=8100)
    assert s2["mmproj"]["downloaded"] is True
    # the model file itself is never picked as its own mmproj
    monkeypatch.setattr(hf_pull, "list_files",
                        lambda repo: [("mmproj-like-name-Q6_K.gguf", None, "gguf")])
    s3 = hf_pull.suggest_preset("org/repo", "mmproj-like-name-Q6_K.gguf")
    assert "mmproj" not in s3


def test_suggest_preset_refuses_template_and_tolerates_offline(
        monkeypatch, _models_dir):
    with pytest.raises(hf_pull.HfError, match="only make sense for a .gguf"):
        hf_pull.suggest_preset("org/repo", "tools.jinja")

    def _boom(repo):
        raise hf_pull.HfError("offline")
    monkeypatch.setattr(hf_pull, "list_files", _boom)
    s = hf_pull.suggest_preset("org/repo", "M-Q6_K.gguf")   # no extras, no crash
    assert "MODEL_PATH=" in s["conf"] and "mmproj" not in s


# ---- auth header + stale-part sweep --------------------------------------------

def test_hf_token_header(monkeypatch):
    seen = []
    monkeypatch.setattr(hf_pull.urllib.request, "urlopen",
                        lambda req, timeout: seen.append(req) or _FakeResponse({"siblings": []}))
    monkeypatch.delenv("HF_TOKEN", raising=False)
    hf_pull.list_gguf("org/repo")
    assert seen[0].get_header("Authorization") is None
    monkeypatch.setenv("HF_TOKEN", "hf_test123")
    hf_pull.list_gguf("org/repo")
    assert seen[1].get_header("Authorization") == "Bearer hf_test123"


def test_clean_stale_parts(_models_dir):
    import os
    fresh = _models_dir / "a" / "new.gguf.part"
    stale = _models_dir / "b" / "old.gguf.part"
    keep = _models_dir / "b" / "model.gguf"
    for p in (fresh, stale, keep):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    assert hf_pull.clean_stale_parts(min_age_s=3600) == 1
    assert not stale.exists() and fresh.exists() and keep.exists()
