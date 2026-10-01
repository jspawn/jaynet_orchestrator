"""h5i plugin hooks — per-run browser session cleanup.

browser.browse opens per-run sessions (jaynet-<run-id8>); the model almost
never calls close, and h5i keeps a live browser process + registry record
per open session forever (live: ~80 stale jaynet-* sessions, all "still
live", blocking `h5i browser rm`). The default session name is
deterministic, so this hook recomputes it from the run id — no shared state
with the tool module (plugin files load standalone, not as a package).

Explicit `session=` overrides are deliberate multi-session flows and stay
user-managed (the hook only reaps the per-run default).
"""

from __future__ import annotations

import shutil
import subprocess


def on_run_end(payload: dict) -> None:
    """Close the run's default h5i browser session if the run used
    browser.browse. Best-effort: any failure (no binary, no such session,
    busy session) is swallowed — run finish must never break on cleanup."""
    if "browser.browse" not in ((payload or {}).get("tools") or []):
        return
    rid = str((payload or {}).get("request_id") or "")
    if not rid:
        return
    binary = ""
    cfg = ((payload or {}).get("config") or {})
    p = str(((cfg.get("plugins") or {}).get("h5i") or {}).get("binary") or "").strip()
    if p:
        binary = p                     # configured path wins, same as the tools
    else:
        binary = shutil.which("h5i") or ""
    if not binary:
        return
    try:
        subprocess.run(
            [binary, "browser", "close", "--session", f"jaynet-{rid[:8]}"],
            capture_output=True, timeout=10)
    except Exception:
        pass
