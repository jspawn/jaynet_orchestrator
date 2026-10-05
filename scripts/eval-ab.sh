#!/usr/bin/env bash
# eval-ab — two-arm eval A/B for agent.state_file (CLM-style state.md injection).
#
#   scripts/eval-ab.sh            # default 8 cases x $REPS reps x 2 arms (on/off)
#   scripts/eval-ab.sh ID ...     # override the case list
#   REPS=5 scripts/eval-ab.sh     # more repeats (default 3)
#
# Arm A runs with agent.state_file.enabled: true, arm B with false. The script
# edits the LIVE runtime.yaml and restarts jaynet-web between arms; an EXIT trap
# always restores the flag and restarts the service — even on a mid-run abort —
# and warns when the live checkout is left dirty afterwards. The restore value
# is RESTORE_FLAG (default false: the shipped default AND the A/B verdict —
# docs/clm-bakeoff.md; set RESTORE_FLAG=true to end with the flag on).
# Scope is the single agent.state_file flag: the yaml path, arm semantics and
# the summary title are all hardcoded to it, this is not a generic A/B driver.
# Results land in $OUT_DIR (default /srv/data/eval-ab/<timestamp>/):
# <arm>-rep<N>.json per suite plus summary.md at the end.
set -euo pipefail
ENV_FILE="${JAYNET_ENV_FILE:-$HOME/.config/jaynet.env}"
ADMIN="${JAYNET_ADMIN:-http://127.0.0.1:8071}"
LIVE_YAML="${LIVE_YAML:-/srv/jaynet-orchestrator/config/runtime.yaml}"
REPS="${REPS:-3}"
OUT="${OUT_DIR:-/srv/data/eval-ab/$(date +%Y%m%d-%H%M%S)}"
IDS=("$@")
if [[ ${#IDS[@]} -eq 0 ]]; then
    # Core: cases where a state file should help. Controls: must stay neutral.
    IDS=(compaction-survival rlm-log-aggregate j-space-loop todo-list
         code-feature-spec fs-roundtrip datetime-awareness ask-user)
fi
TOKEN="$(grep -m1 '^JAYNET_WEB_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d "\"'")"
mkdir -p "$OUT"

api() { curl -sf -H "Authorization: Bearer $TOKEN" "$@"; }

wait_idle() { # block until no suite is running and the API answers
    for _ in $(seq 1 720); do
        local st
        st=$(api "$ADMIN/api/admin/evals/run-status" 2>/dev/null) || { sleep 5; continue; }
        [[ $(echo "$st" | python3 -c 'import sys,json;print(json.load(sys.stdin)["running"])') == "False" ]] && return 0
        sleep 10
    done
    echo "eval-ab: API never became idle" >&2; return 1
}

set_flag() { # true|false — edit live yaml, restart web, wait for API
    sed -i "/^  state_file:/{n;s/    enabled: .*/    enabled: $1/}" "$LIVE_YAML"
    local got
    got=$(grep -A1 '^  state_file:' "$LIVE_YAML" | tail -1 | xargs)
    if [[ "$got" != "enabled: $1" ]]; then
        # The sed above fails SILENTLY when the indentation doesn't match
        # exactly ('  state_file:' + 4-space '    enabled:') — without this
        # assert both arms run with the SAME flag and the A/B is a no-op.
        echo "eval-ab: FATAL: flag edit did not land (state_file line is now '$got'," >&2
        echo "  wanted 'enabled: $1') — fix the indentation in $LIVE_YAML." >&2
        return 1
    fi
    echo "eval-ab: flag -> $got"
    systemctl --user restart jaynet-web
    wait_idle
}

run_suite() { # <arm> <rep> — fire one suite, collect its results into $OUT
    local arm=$1 rep=$2 start body tmp
    start=$(date +%s)
    body=$(printf ',"%s"' "${IDS[@]}"); body="{\"ids\":[${body:1}]}"
    api -X POST -H 'Content-Type: application/json' -d "$body" \
        "$ADMIN/api/admin/evals/run" >/dev/null
    echo "eval-ab: $arm rep$rep started (${#IDS[@]} cases) at $(date +%H:%M)"
    wait_idle
    tmp=$(mktemp)
    api "$ADMIN/api/admin/evals/results?limit=500" > "$tmp"
    python3 - "$start" "$tmp" "$OUT/$arm-rep$rep.json" "${IDS[@]}" <<'EOF'
import json, sys
start, src, path = float(sys.argv[1]), sys.argv[2], sys.argv[3]
ids = set(sys.argv[4:])
with open(src) as f:
    rows = [r for r in json.load(f)["results"]
            if r.get("ts", 0) >= start - 120 and r.get("test_id") in ids]
rows.sort(key=lambda r: r.get("ts", 0))
# The window can catch the PREVIOUS suite's tail (its last cases land inside
# start-120s). A suite runs each case once, so the last row per test_id is
# this suite's own.
by_id = {r["test_id"]: r for r in rows}
rows = sorted(by_id.values(), key=lambda r: r.get("ts", 0))
with open(path, "w") as f:
    json.dump(rows, f, indent=1)
print(f"eval-ab: {path}: {len(rows)} results, "
      f"{sum(1 for r in rows if r.get('passed'))} passed")
EOF
    rm -f "$tmp"
}

summarize() {
    python3 - "$OUT" "$REPS" "${IDS[@]}" <<'EOF'
import glob, json, os, sys
out, reps = sys.argv[1], int(sys.argv[2])
ids = sys.argv[3:]
lines = ["# state_file A/B", "",
         f"- dir: `{out}`", f"- reps per arm: {reps}", "",
         "| case | ON | OFF |", "|---|---|---|"]
tot = {"on": 0, "off": 0}
for cid in ids:
    cells = []
    for arm in ("on", "off"):
        p = n = 0
        for f in sorted(glob.glob(os.path.join(out, f"{arm}-rep*.json"))):
            for r in json.load(open(f)):
                if r.get("test_id") == cid:
                    n += 1; p += bool(r.get("passed"))
        cells.append(f"{p}/{n}")
        tot[arm] += p
    lines.append(f"| {cid} | {cells[0]} | {cells[1]} |")
lines.append(f"| **total** | **{tot['on']}** | **{tot['off']}** |")
text = "\n".join(lines) + "\n"
with open(os.path.join(out, "summary.md"), "w") as f:
    f.write(text)
print(text)
EOF
}

echo "eval-ab: output dir $OUT"
# Restore-on-exit: NEVER leave the LIVE runtime.yaml on an arm's flag — a
# mid-run abort (Ctrl-C, failed suite, lost shell) used to strand the live
# service on whatever arm ran last. RESTORE_FLAG=false matches the shipped
# default and the A/B verdict; override only deliberately.
RESTORE_FLAG="${RESTORE_FLAG:-false}"
restore() {
    trap - EXIT   # once
    echo "eval-ab: restoring agent.state_file.enabled: $RESTORE_FLAG" >&2
    set_flag "$RESTORE_FLAG" \
        || echo "eval-ab: WARNING: flag restore failed — fix $LIVE_YAML and restart jaynet-web by hand" >&2
    git -C "$(dirname "$(dirname "$LIVE_YAML")")" diff --quiet -- config/runtime.yaml 2>/dev/null \
        || echo "eval-ab: WARNING: $LIVE_YAML differs from git HEAD — the live checkout is dirty" >&2
}
trap restore EXIT
# Continuation knobs: ARMS="off" and/or REP_START=2 skip already-done work
# (e.g. after a crash — recover missing suite files into $OUT_DIR first).
REP_START="${REP_START:-1}"
for arm in ${ARMS:-on off}; do
    if [[ $arm == on ]]; then set_flag true; else set_flag false; fi
    for rep in $(seq "$REP_START" "$REPS"); do run_suite "$arm" "$rep"; done
    REP_START=1
done
summarize
echo "eval-ab: done at $(date +%H:%M)"
