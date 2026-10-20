"""launch_argv: canonical conf -> vllm/radiance argv translation.

The conf is the single store for every backend; build_argv renders the
backend's own flags from it. Switching a preset's binary type is the
translation — shared keys map, llama-only keys drop WITH a warning (the
admin must see what a switch suspends), unknown-to-us keys pass through
EXTRA_ARGS only.
"""
import pytest

from runtime.launch_argv import build_argv, main, parse_conf


@pytest.fixture()
def conf_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_MODELS", "/models")
    def _write(text):
        p = tmp_path / "t.conf"
        p.write_text(text)
        return str(p)
    return _write


def test_parse_conf_strips_comments_and_expands_models(conf_file):
    f = conf_file("# head\nMODEL_PATH=$ORCH_MODELS/x.gguf  # trailing\n"
                  "CTX_SIZE=8192\n\n  \nBADLINE\n")
    d = parse_conf(f)
    assert d == {"MODEL_PATH": "/models/x.gguf", "CTX_SIZE": "8192"}
    assert "BADLINE" not in d  # no '=' → skipped, not a key


def _kwargs(**over):
    kw = dict(binary="/bin/x", device_env="HIP_VISIBLE_DEVICES",
              host="127.0.0.1", port="8090", gpu="1", alias="m")
    kw.update(over)
    return kw


def test_vllm_shared_keys_map(tmp_path):
    model = tmp_path / "m.gguf"
    model.write_text("x")
    argv, env, warns = build_argv(
        "vllm", {"MODEL_PATH": str(model), "CTX_SIZE": "32768",
                 "KV_CACHE_DTYPE": "fp8", "GPU_MEM_UTIL": "0.92",
                 "MAX_NUM_SEQS": "32", "TOOLS_TEMPLATE": "/t.jinja"},
        **_kwargs(gpu="0,1"))
    assert argv[:3] == ["/bin/x", "serve", str(model)]
    assert "--tensor-parallel-size" in argv
    assert argv[argv.index("--tensor-parallel-size") + 1] == "2"
    for flag, val in (("--max-model-len", "32768"), ("--kv-cache-dtype", "fp8"),
                      ("--gpu-memory-utilization", "0.92"),
                      ("--max-num-seqs", "32"), ("--chat-template", "/t.jinja"),
                      ("--served-model-name", "m")):
        assert argv[argv.index(flag) + 1] == val
    assert env == {"HIP_VISIBLE_DEVICES": "0,1"}
    assert warns == []


def test_radiance_shared_keys_map(tmp_path):
    model = tmp_path / "m.rad"
    model.write_text("x")
    argv, env, warns = build_argv(
        "radiance", {"MODEL_PATH": str(model), "CTX_SIZE": "32768",
                     "KV_CACHE_DTYPE": "fp8", "MAX_NUM_SEQS": "8",
                     "EXTRA_ARGS": "--gpu-headroom-mib 96"},
        **_kwargs(alias=""))
    assert argv[:2] == ["/bin/x", "--model"]
    assert argv[argv.index("--tp") + 1] == "1"          # single card
    assert argv[argv.index("--max-model-len") + 1] == "32768"
    assert argv[argv.index("--kv-cache-dtype") + 1] == "fp8"
    assert argv[argv.index("--max-num-seqs") + 1] == "8"
    assert argv[-2:] == ["--gpu-headroom-mib", "96"]   # EXTRA_ARGS verbatim
    assert env == {"HIP_VISIBLE_DEVICES": "1"}
    assert warns == []


def test_llama_only_keys_drop_with_named_warnings(tmp_path):
    model = tmp_path / "m.gguf"
    model.write_text("x")
    conf = {"MODEL_PATH": str(model), "GPU_LAYERS": "99", "TEMP": "0.6",
            "MTP": "on", "CACHE_TYPE_K": "q8_0"}
    _, _, w_vllm = build_argv("vllm", conf, **_kwargs())
    _, _, w_rad = build_argv("radiance", conf, **_kwargs())
    for warns in (w_vllm, w_rad):
        dropped = {w.split(" ")[0] for w in warns}
        assert {"GPU_LAYERS", "TEMP", "MTP", "CACHE_TYPE_K"} <= dropped


def test_radiance_drops_vllm_only_and_alias_and_template(tmp_path):
    model = tmp_path / "m.rad"
    model.write_text("x")
    argv, _, warns = build_argv(
        "radiance", {"MODEL_PATH": str(model), "GPU_MEM_UTIL": "0.9",
                     "TOOLS_TEMPLATE": "/t.jinja"}, **_kwargs())
    assert "--gpu-memory-utilization" not in argv
    assert "--chat-template" not in argv
    joined = " ".join(warns)
    assert "GPU_MEM_UTIL" in joined and "TOOLS_TEMPLATE" in joined
    assert "ALIAS" in joined          # radiance ids itself


def test_hf_repo_id_accepted_without_file():
    argv, _, _ = build_argv("vllm", {"MODEL_PATH": "Qwen/Qwen3.8-27B-FP8"},
                            **_kwargs())
    assert argv[2] == "Qwen/Qwen3.8-27B-FP8"
    with pytest.raises(ValueError, match="model not found"):
        build_argv("vllm", {"MODEL_PATH": "/no/such/file.gguf"}, **_kwargs())


def test_cpu_refused_and_backend_validated(tmp_path):
    model = tmp_path / "m.gguf"
    model.write_text("x")
    with pytest.raises(ValueError, match="GPU engine"):
        build_argv("radiance", {"MODEL_PATH": str(model)}, **_kwargs(gpu=""))
    with pytest.raises(ValueError, match="unknown backend"):
        build_argv("llama", {"MODEL_PATH": str(model)}, **_kwargs())
    with pytest.raises(ValueError, match="MODEL_PATH"):
        build_argv("vllm", {}, **_kwargs())


def test_cli_dry_run(conf_file, capsys):
    model = conf_file("MODEL_PATH=$ORCH_MODELS/x.gguf\nCTX_SIZE=8192\n")
    # MODEL_PATH must exist or look like a repo id — point it at a repo id
    with open(model, "w") as f:
        f.write("MODEL_PATH=org/name\nCTX_SIZE=8192\n")
    rc = main(["--conf", model, "--type", "vllm", "--binary", "/bin/x",
               "--gpu", "0", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "/bin/x serve org/name" in out
    assert "--tensor-parallel-size 1" in out


def test_cli_missing_model_errors(conf_file, capsys):
    model = conf_file("CTX_SIZE=8192\n")
    rc = main(["--conf", model, "--type", "vllm", "--binary", "/bin/x",
               "--gpu", "0", "--dry-run"])
    assert rc == 1
    assert "MODEL_PATH" in capsys.readouterr().err
