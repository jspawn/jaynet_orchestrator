"""Occamy native launcher: FP8 packs, official MTP and loopback HTTP argv."""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "saber-occamy-native.sh"


def _tree(tmp_path):
    packs = tmp_path / "packs"
    fp8 = packs / "fp8"
    layers = packs / "layers"
    globals_ = packs / "globals"
    checkpoint = tmp_path / "checkpoint"
    prebuilt = tmp_path / "repo" / "prebuilt" / "gfx1201"
    for directory in (fp8, layers, globals_, checkpoint, prebuilt):
        directory.mkdir(parents=True)
    for name in ("config.json", "tokenizer.json", "chat_template.jinja"):
        (checkpoint / name).write_text("{}")
    mtp = tmp_path / "mtp.safetensors"
    mtp.write_text("MTP")
    hip = tmp_path / "libamdhip64.so.7"
    hip.write_text("")
    return fp8, layers, globals_, checkpoint, mtp, hip


def test_occamy_native_builds_loopback_fp8_mtp_argv(tmp_path):
    fp8, layers, globals_, checkpoint, mtp, hip = _tree(tmp_path)
    log = tmp_path / "argv.log"
    binary = tmp_path / "repo" / "target" / "release" / "saber"
    binary.parent.mkdir(parents=True)
    binary.write_text(f"""#!/usr/bin/env bash
printf '%s\\n' "$@" > {log}
""")
    binary.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "SABER_REPO": str(tmp_path / "repo"), "SABER_BIN": str(binary),
        "OCCAMY_FP8": str(fp8), "OCCAMY_LAYERS": str(layers),
        "OCCAMY_GLOBALS": str(globals_), "OCCAMY_CHECKPOINT": str(checkpoint),
        "OCCAMY_MTP": str(mtp), "HIP_LIB": str(hip),
        "HIP_DEVICES": "0,1", "DEVICE": "0", "HOST": "127.0.0.1",
        "PORT": "18099", "MAX_CTX": "262144", "MAX_REQUESTS": "1",
        "SPEC_DEPTH": "1", "MODEL": "occamy-fp8-saber-tp2",
    })
    result = subprocess.run([str(SCRIPT), "--no-build"], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    args = log.read_text().splitlines()

    def value(flag):
        return args[args.index(flag) + 1]

    assert args[0] == "occamy-exec"
    assert value("--fp8-experts") == str(fp8)
    assert value("--layers") == str(layers)
    assert value("--globals") == str(globals_)
    assert value("--checkpoint") == str(checkpoint)
    assert value("--mtp") == str(mtp)
    assert value("--spec-depth") == "1"
    assert value("--host") == "127.0.0.1"
    assert value("--serve") == "18099"
    assert value("--max-ctx") == "262144"
    assert value("--max-requests") == "1"
    assert value("--served-name") == "occamy-fp8-saber-tp2"


def test_occamy_native_rejects_mtp_with_multiple_request_slots(tmp_path):
    fp8, layers, globals_, checkpoint, mtp, hip = _tree(tmp_path)
    env = os.environ.copy()
    env.update({
        "SABER_REPO": str(tmp_path / "repo"),
        "OCCAMY_FP8": str(fp8), "OCCAMY_LAYERS": str(layers),
        "OCCAMY_GLOBALS": str(globals_), "OCCAMY_CHECKPOINT": str(checkpoint),
        "OCCAMY_MTP": str(mtp), "HIP_LIB": str(hip), "MAX_REQUESTS": "2",
    })
    result = subprocess.run([str(SCRIPT), "--no-build"], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert "MTP needs MAX_REQUESTS=1" in result.stderr
