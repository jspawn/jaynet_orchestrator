#!/usr/bin/env python3
"""slash-run — fire a slash command (direct tool call) at the live JayNet API
and stream the run to completion, auto-approving confirmation prompts.

Built for confirmation-gated ops tools like `model.measure` that take minutes
and must not die on the 300s confirmation timeout.

Usage:
  slash-run.py "/model.measure preset=cybertiel-35b-a3b"
  slash-run.py --no-approve "/h5i.browser.test ..."   # just watch
  slash-run.py --timeout 2400 "/model.measure preset=…"

Env: JAYNET_ENV_FILE (default ~/.config/jaynet.env) for JAYNET_WEB_TOKEN,
JAYNET_ADMIN (default http://127.0.0.1:8071).
"""
import argparse
import json
import os
import sys
import time
import urllib.request

ADMIN = os.environ.get("JAYNET_ADMIN", "http://127.0.0.1:8071")


def token() -> str:
    env = os.environ.get("JAYNET_ENV_FILE",
                         os.path.expanduser("~/.config/jaynet.env"))
    try:
        for line in open(env):
            line = line.strip()
            if line.startswith("JAYNET_WEB_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return os.environ.get("JAYNET_WEB_TOKEN", "")


def _post(path: str, body: dict, timeout: float = 30) -> dict:
    req = urllib.request.Request(
        ADMIN + path, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token(),
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
        return data if isinstance(data, dict) else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", help="slash command, e.g. '/model.measure preset=x'")
    ap.add_argument("--no-approve", action="store_true",
                    help="do not auto-approve confirmation requests")
    ap.add_argument("--timeout", type=float, default=2400,
                    help="overall watch timeout in seconds (default 2400)")
    ap.add_argument("--quiet", action="store_true",
                    help="only print the final answer")
    args = ap.parse_args()
    tok = token()
    if not tok:
        print("no JAYNET_WEB_TOKEN found", file=sys.stderr)
        return 2

    run_id = _post("/api/chat", {"message": args.command})["run_id"]
    if not args.quiet:
        print(f"run_id {run_id}")

    req = urllib.request.Request(
        f"{ADMIN}/api/stream/{run_id}",
        headers={"Authorization": "Bearer " + tok, "Accept": "text/event-stream"})
    deadline = time.monotonic() + args.timeout
    answer = None
    status = "timeout"
    with urllib.request.urlopen(req, timeout=args.timeout + 60) as stream:
        for raw in stream:
            if time.monotonic() > deadline:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except ValueError:
                continue
            etype = ev.get("type")
            data = ev.get("data") or {}
            if etype == "confirmation_request":
                cid = data.get("confirmation_id")
                tool = data.get("tool")
                if args.no_approve:
                    print(f"confirmation_request {tool} id={cid} (NOT approved)")
                else:
                    _post(f"/api/approve/{run_id}",
                          {"confirmation_id": cid, "approved": True})
                    print(f"approved {tool} (id={str(cid)[:8]}…)")
            elif etype == "run_finish":
                status = data.get("status", "?")
                answer = data.get("answer", "")
                break
            elif not args.quiet and etype not in ("ping",):
                txt = (data.get("message") or data.get("content") or "")
                print(f"[{etype}] {str(txt)[:200]}")

    print(f"\n--- status: {status} ---")
    if answer:
        print(answer)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
