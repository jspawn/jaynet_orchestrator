"""launch_argv: canonical conf -> vllm/radiance/saber translation.

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


def _saber_conf(tmp_path, *, tp=1):
    suffix = "-tp1-fp8head" if tp == 1 else "-fp8head"
    model = tmp_path / "packs" / f"qwen38-27b-nvfp4-devfinal{suffix}"
    draft = tmp_path / "packs" / f"qwen38-dflash2-drafter{suffix}"
    ranks = ("rank0.pack",) if tp == 1 else ("rank0.pack", "rank1.pack")
    for pack in (model, draft):
        pack.mkdir(parents=True)
        for rank in ranks:
            (pack / rank).write_text("SBRPACK1")
    return {"MODEL_PATH": str(model), "MTP_MODEL": str(draft),
            "CTX_SIZE": "65536", "MAX_NUM_SEQS": "4",
            "EXTRA_ARGS": "--enable-prefix-caching on --vision lazy"}


def test_saber_tp1_maps_packs_devices_and_shared_keys(tmp_path):
    argv, env, warns = build_argv(
        "saber", _saber_conf(tmp_path), **_kwargs(gpu="1", alias="qwen-saber"))
    assert argv == ["/bin/x", "--enable-prefix-caching", "on",
                    "--vision", "lazy"]
    assert env["DEVICE"] == "1" and "HIP_DEVICES" not in env
    assert "HIP_VISIBLE_DEVICES" not in env
    assert env["PORT"] == "8090" and env["HOST"] == "127.0.0.1"
    assert env["MAX_CTX"] == "65536" and env["MAX_SEQS"] == "4"
    assert env["MODEL"] == "qwen-saber"
    assert env["CNAME"] == "saber-jaynet-8090"
    assert env["TIER"].endswith("-tp1-fp8head")
    assert env["DRAFT"].endswith("-tp1-fp8head")
    assert env["SABER_PACKS"] == str(tmp_path / "packs")
    assert warns == []


def test_saber_tp2_uses_two_rank_packs_and_warns_about_llama_keys(tmp_path):
    conf = _saber_conf(tmp_path, tp=2) | {"TEMP": "0.6", "MTP": "off"}
    _, env, warns = build_argv("saber", conf, **_kwargs(gpu="0,1"))
    assert env["HIP_DEVICES"] == "0,1" and "DEVICE" not in env
    dropped = {w.split(" ")[0] for w in warns}
    assert dropped == {"TEMP", "MTP"}


def test_saber_rejects_wrong_tp_or_missing_draft(tmp_path):
    with pytest.raises(ValueError, match="DFlash2"):
        build_argv("saber", {"MODEL_PATH": str(tmp_path)}, **_kwargs())
    conf = _saber_conf(tmp_path)
    with pytest.raises(ValueError, match="TP=1"):
        build_argv("saber", conf, **_kwargs(gpu="0,1"))
    with pytest.raises(ValueError, match="one GPU"):
        build_argv("saber", conf, **_kwargs(gpu="0,1,2"))


def _flashnext_conf(tmp_path):
    pack = tmp_path / "nvme" / "saber-packs" / "qwen38-flashnext-quark-trunk8"
    for rank in ("rank0", "rank1"):
        (pack / rank).mkdir(parents=True, exist_ok=True)
        (pack / rank / "trunk8.pack").write_text("SBRPACK1")
    (pack / "manifest.json").write_text("{}")
    return {"SABER_MODEL": "flashnext", "MODEL_PATH": str(pack),
            "CTX_SIZE": "262144", "EXTRA_ARGS": "--fn-vision lazy"}


def test_saber_flashnext_maps_tp2_pack_without_dflash2(tmp_path):
    conf = _flashnext_conf(tmp_path)
    argv, env, warns = build_argv(
        "saber", conf, **_kwargs(gpu="0,1", alias="qwen-flashnext"))
    assert argv == ["/bin/x", "--fn-vision", "lazy"]
    assert env["SABER_MODEL"] == "flashnext"
    assert env["PACK"] == conf["MODEL_PATH"]
    assert env["SABER_NVME"] == str(tmp_path / "nvme")
    assert env["HIP_DEVICES"] == "0,1" and "DEVICE" not in env
    assert env["MODEL"] == "qwen-flashnext"
    assert env["MAX_CTX"] == "262144"
    assert warns == []


def test_saber_flashnext_requires_tp2_and_complete_pack(tmp_path):
    conf = _flashnext_conf(tmp_path)
    with pytest.raises(ValueError, match="two GPUs"):
        build_argv("saber", conf, **_kwargs(gpu="1"))
    (tmp_path / "nvme" / "saber-packs" /
     "qwen38-flashnext-quark-trunk8" / "rank1" / "trunk8.pack").unlink()
    with pytest.raises(ValueError, match="rank1/trunk8.pack"):
        build_argv("saber", conf, **_kwargs(gpu="0,1"))


def _occamy_conf(tmp_path):
    packs = tmp_path / "packs"
    paths = {key: packs / key.lower() for key in
             ("OCCAMY_FP8", "OCCAMY_LAYERS", "OCCAMY_GLOBALS")}
    checkpoint = tmp_path / "checkpoint"
    for directory in (*paths.values(), checkpoint):
        directory.mkdir(parents=True)
    (checkpoint / "config.json").write_text("{}")
    mtp = tmp_path / "mtp.safetensors"
    mtp.write_text("MTP")
    repo = tmp_path / "repo"
    repo.mkdir()
    binary = repo / "saber"
    binary.write_text("")
    return {"SABER_MODEL": "occamy", "MODEL_PATH": str(paths["OCCAMY_FP8"]),
            "OCCAMY_LAYERS": str(paths["OCCAMY_LAYERS"]),
            "OCCAMY_GLOBALS": str(paths["OCCAMY_GLOBALS"]),
            "OCCAMY_CHECKPOINT": str(checkpoint), "MTP_MODEL": str(mtp),
            "SABER_REPO": str(repo), "SABER_BIN": str(binary),
            "SPEC_DRAFT_N_MAX": "1", "CTX_SIZE": "262144",
            "MAX_NUM_SEQS": "1", "EXTRA_ARGS": "--verbose"}


def test_saber_occamy_maps_fp8_packs_mtp_and_loopback(tmp_path):
    conf = _occamy_conf(tmp_path)
    argv, env, warns = build_argv(
        "saber", conf, **_kwargs(gpu="0,1", alias="occamy-fp8"))
    assert argv == ["/bin/x", "--verbose"]
    assert env["SABER_MODEL"] == "occamy"
    assert env["OCCAMY_FP8"] == conf["MODEL_PATH"]
    assert env["OCCAMY_MTP"] == conf["MTP_MODEL"]
    assert env["SPEC_DEPTH"] == "1" and env["MAX_REQUESTS"] == "1"
    assert env["HIP_DEVICES"] == "0,1" and env["DEVICE"] == "0"
    assert env["HOST"] == "127.0.0.1" and env["MODEL"] == "occamy-fp8"
    assert env["SABER_BIN"] == conf["SABER_BIN"]
    assert env["MAX_CTX"] == "262144"
    assert warns == []


def test_saber_occamy_requires_consecutive_cards_and_single_mtp_request(tmp_path):
    conf = _occamy_conf(tmp_path)
    with pytest.raises(ValueError, match="consecutive GPUs"):
        build_argv("saber", conf, **_kwargs(gpu="1,0"))
    conf["MAX_NUM_SEQS"] = "2"
    with pytest.raises(ValueError, match="MAX_NUM_SEQS=1"):
        build_argv("saber", conf, **_kwargs(gpu="0,1"))
