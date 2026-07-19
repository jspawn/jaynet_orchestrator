"""start-model.sh: one launcher, two modes.

Name mode (process_manager/systemd): runtime.yaml owns the slot — PORT / GPU /
alias in the .conf must be IGNORED. File mode (--preset, the serve.* dispatcher):
the .conf owns the slot. --dry-run prints the resolved command without launching,
so all of this runs hermetically in tmp dirs.
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "start-model.sh"
VENV_BIN = ROOT / ".venv" / "bin"     # python3 with pyyaml (name mode parses YAML)


def _write(path: Path, text: str) -> str:
    path.write_text(text)
    return str(path)


def _run(args, env_extra, tmp_path):
    model = _write(tmp_path / "model.gguf", "x")          # must exist (-f check)
    env = dict(os.environ)
    env["PATH"] = f"{VENV_BIN}:{env['PATH']}"
    env["LLAMA_BIN"] = "/bin/true"                        # passes the -x check
    env.update(env_extra)
    return model, subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, text=True,
        capture_output=True, timeout=30)


def _conf(tmp_path: Path, extra="") -> str:
    return _write(tmp_path / "preset.conf", f"""\
MODEL_PATH={tmp_path}/model.gguf
CTX_SIZE=262144
TEMP=0.7
{extra}""")


def _runtime_yaml(tmp_path: Path, conf: str) -> str:
    return _write(tmp_path / "runtime.yaml", f"""\
models:
  presets:
    brain:
      preset: {conf}
      alias: local-orchestrator
      port: 8090
      gpu: "0"
      served_id: brain-served-id
""")


def test_name_mode_runtime_yaml_owns_the_slot(tmp_path):
    conf = _conf(tmp_path, "PORT=9999\nVISIBLE_DEVICES=1\nALIAS=conf-alias\n")
    yaml_path = _runtime_yaml(tmp_path, conf)
    _, r = _run(["brain", "--dry-run"], {"ORCH_CONFIG": yaml_path}, tmp_path)
    assert r.returncode == 0, r.stderr
    # runtime.yaml wins: port/alias from the preset block, not the .conf
    assert "--port 8090" in r.stdout and "--alias brain-served-id" in r.stdout
    assert "9999" not in r.stdout and "conf-alias" not in r.stdout
    # GPU pin from runtime.yaml; conf sampling honoured
    assert "HIP_VISIBLE_DEVICES=0" in r.stdout and "--ctx-size 262144" in r.stdout


def test_file_mode_conf_owns_the_slot(tmp_path):
    conf = _conf(tmp_path, "PORT=8080\nHOST=127.0.0.1\nVISIBLE_DEVICES=1\nALIAS=my-model\n")
    # No ORCH_CONFIG at all — file mode must not touch runtime.yaml
    _, r = _run(["--preset", conf, "--dry-run"], {"ORCH_CONFIG": "/nonexistent"}, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "--port 8080" in r.stdout and "--alias my-model" in r.stdout
    assert "HIP_VISIBLE_DEVICES=1" in r.stdout
    assert "(file mode)" in r.stdout


def test_file_mode_defaults_when_conf_omits_slot_keys(tmp_path):
    conf = _conf(tmp_path)
    _, r = _run(["--preset", conf, "-d"], {}, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "--port 8080" in r.stdout                    # built-in default
    assert "--alias model" in r.stdout                  # falls back to model basename
    assert "HIP_VISIBLE_DEVICES" not in r.stdout        # no pin -> all GPUs/CPU


def test_missing_model_fails_loud(tmp_path):
    conf = _write(tmp_path / "preset.conf", "MODEL_PATH=/nonexistent/x.gguf\n")
    _, r = _run(["--preset", conf, "--dry-run"], {}, tmp_path)
    assert r.returncode != 0 and "model not found" in r.stderr


def test_unknown_catalog_preset_fails_loud(tmp_path):
    yaml_path = _runtime_yaml(tmp_path, _conf(tmp_path))
    _, r = _run(["nosuch", "--dry-run"], {"ORCH_CONFIG": yaml_path}, tmp_path)
    assert r.returncode != 0 and "not found in runtime.yaml" in r.stderr
