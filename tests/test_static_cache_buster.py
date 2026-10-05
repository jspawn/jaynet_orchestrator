"""Static-asset cache-buster gate (audit #27 D7, 4th recurrence): index.html
references app.js/app.css with a manual `?v=N` buster, and a change to either
asset without a buster bump leaves cached clients on the old code (live:
4102121's inline AV players invisible behind ?v=46/48). This test fails when
the last change to an asset is NEWER than the last change to its buster line
in index.html — committed history AND the uncommitted working tree both count
(a not-yet-committed bump already fixes it, a not-yet-committed asset edit is
the newest change there is). Skips gracefully where git or history is absent
(CI checkout without history)."""
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _git_ts(args):
    """Commit timestamp (%ct) of `git log -1 <args>`, or None when git or the
    history is unavailable."""
    if shutil.which("git") is None:
        return None
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%ct",
                            *args],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return int(out) if r.returncode == 0 and out.isdigit() else None


def _worktree_diff(path):
    """The uncommitted diff (unstaged + staged) for one path, '' when git is
    unavailable or the path is clean."""
    out = []
    for extra in ([], ["--cached"]):
        try:
            r = subprocess.run(["git", "-C", str(ROOT), "diff", *extra,
                                "--", path],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return ""
        if r.returncode == 0:
            out.append(r.stdout)
    return "\n".join(out)


@pytest.mark.parametrize("asset", ["app.js", "app.css"])
def test_cache_buster_not_older_than_asset(asset):
    asset_ts = _git_ts(["--", f"web/static/{asset}"])
    # -G (diff-regex), not -S: a buster BUMP keeps the occurrence count, so
    # only -G sees the value change on the reference line.
    buster_ts = _git_ts(["-G", rf"{asset}\?v=", "--", "web/static/index.html"])
    if asset_ts is None or buster_ts is None:
        pytest.skip("git or repo history unavailable")
    now = int(time.time())
    if _worktree_diff(f"web/static/{asset}"):
        asset_ts = now                       # uncommitted asset edit = newest
    if re.search(rf"^[+-].*{re.escape(asset)}\?v=",
                 _worktree_diff("web/static/index.html"), re.M):
        buster_ts = now                      # uncommitted bump already done
    assert buster_ts >= asset_ts, (
        f"web/static/{asset} changed after index.html's ?v= buster last moved "
        "— bump the buster so cached clients pick up the new asset")
