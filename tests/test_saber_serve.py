"""Saber lifecycle wrapper: detached container stays managed by JayNet."""
import os
import signal
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "saber-serve.sh"


def _fake_runtime(tmp_path):
    repo = tmp_path / "upstream"
    tools = repo / "tools"
    tools.mkdir(parents=True)
    (tools / "serve_27b.sh").write_text("""#!/usr/bin/env bash
# --host ${HOST:-127.0.0.1} --port $PORT (JayNet loopback patch marker)
set -u
printf 'launcher:%s:CNAME=%s\\n' "$*" "${CNAME:-}" >> "$FAKE_LOG"
if [[ " $* " == *" stop "* ]]; then exit 0; fi
exit "${FAKE_START_RC:-0}"
""")
    (tools / "serve_27b.sh").chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "podman").write_text("""#!/usr/bin/env bash
set -u
printf 'runtime:%s:CNAME=%s\\n' "$*" "${CNAME:-}" >> "$FAKE_LOG"
case "$1" in
  logs) exec sleep 30 ;;
  wait) touch "$FAKE_LOG.waiting"; exec sleep "${FAKE_WAIT_S:-0.1}" ;;
  inspect) echo 0 ;;
esac
""")
    (bindir / "podman").chmod(0o755)
    return repo, bindir


def _env(tmp_path, repo, bindir):
    log = tmp_path / "calls.log"
    env = os.environ.copy()
    env.update({"SABER_REPO": str(repo), "SABER_CONTAINER": "podman",
                "PATH": f"{bindir}:{env['PATH']}", "FAKE_LOG": str(log),
                "DEVICE": "1", "PORT": "18080", "HOST": "127.0.0.1",
                "CNAME": "saber-jaynet-18080"})
    return env, log


def test_saber_waits_for_detached_container_and_cleans_up(tmp_path):
    repo, bindir = _fake_runtime(tmp_path)
    env, log = _env(tmp_path, repo, bindir)
    result = subprocess.run(
        [str(SCRIPT), "--enable-prefix-caching", "on"], env=env,
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    assert "--tp 1 --no-build --enable-prefix-caching on" in calls
    assert "runtime:logs -f saber-jaynet-18080" in calls
    assert "runtime:wait saber-jaynet-18080" in calls
    assert "runtime:inspect" in calls
    assert "launcher:--tp 1 stop" in calls


def test_saber_sigterm_stops_only_its_container(tmp_path):
    repo, bindir = _fake_runtime(tmp_path)
    env, log = _env(tmp_path, repo, bindir)
    env["FAKE_WAIT_S"] = "30"
    proc = subprocess.Popen([str(SCRIPT)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 5
    waiting = Path(str(log) + ".waiting")
    while not waiting.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert waiting.exists()
    from tools.model import measure as MM
    assert MM._server_pids_by_port(18080) == [proc.pid]
    proc.send_signal(signal.SIGTERM)
    _, stderr = proc.communicate(timeout=5)
    assert proc.returncode == 143, stderr.decode()
    assert "launcher:--tp 1 stop:CNAME=saber-jaynet-18080" in log.read_text()


def test_saber_start_failure_still_cleans_up(tmp_path):
    repo, bindir = _fake_runtime(tmp_path)
    env, log = _env(tmp_path, repo, bindir)
    env["FAKE_START_RC"] = "7"
    result = subprocess.run([str(SCRIPT)], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 7
    assert "launcher:--tp 1 stop" in log.read_text()


def test_saber_refuses_unpatched_public_bind(tmp_path):
    repo, bindir = _fake_runtime(tmp_path)
    (repo / "tools" / "serve_27b.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    env, log = _env(tmp_path, repo, bindir)
    result = subprocess.run([str(SCRIPT)], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert "loopback-host patch" in result.stderr
    assert not log.exists()
