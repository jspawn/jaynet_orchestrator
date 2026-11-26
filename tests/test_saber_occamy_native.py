"""Occamy compatibility wrapper: delegate to the unified Saber launcher."""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "saber-occamy-native.sh"


def test_occamy_wrapper_delegates_to_unified_launcher(tmp_path):
    repo = tmp_path / "repo"
    tools = repo / "tools"
    tools.mkdir(parents=True)
    log = tmp_path / "argv.log"
    launcher = tools / "serve_occamy_native.sh"
    launcher.write_text(f"""#!/usr/bin/env bash
printf 'repo=%s args=%s\\n' "${{SABER_REPO:-}}" "$*" > {log}
""")
    launcher.chmod(0o755)
    env = os.environ.copy()
    env["SABER_REPO"] = str(repo)
    result = subprocess.run([str(SCRIPT), "--no-build", "--spec-depth", "1"],
                            env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert log.read_text() == f"repo={repo} args=--no-build --spec-depth 1\n"


def test_occamy_wrapper_names_a_missing_unified_launcher(tmp_path):
    env = os.environ.copy()
    env["SABER_REPO"] = str(tmp_path)
    result = subprocess.run([str(SCRIPT), "--no-build"], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert "serve_occamy_native.sh" in result.stderr
