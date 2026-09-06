#!/usr/bin/env bash
# Scheduled data-dir backup (readiness audit OPS-2: backups were manual-only).
# Writes one timestamped archive (0600) into ${JAYNET_BACKUPS:-/srv/backups}
# and keeps the newest 7 dailies + 4 weeklies (Sunday = weekly).
#
# Wired up by systemd/jaynet-backup.timer (daily 03:17), or run by hand:
#   scripts/backup.sh
# Restore is still the admin console path (Admin → Backup → restore) or by
# hand from the archive — see docs/operations.md.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
OUT="${JAYNET_BACKUPS:-/srv/backups}"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY=python3

mkdir -p "$OUT"
chmod 700 "$OUT" 2>/dev/null || true
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
JAYNET_DATA="${JAYNET_DATA:-/srv/data}" "$PY" -m runtime.backup "$OUT"

# Retention: 7 newest dailies always; of the rest keep the 4 newest Sunday
# snapshots (weeklies), delete the remainder.
cd "$OUT"
ls -1t jaynet-backup-*.tar.gz 2>/dev/null | tail -n +8 | \
  { keep=""; i=0
    while IFS= read -r f; do
      # file date from the name: jaynet-backup-YYYYMMDD-HHMMSS.tar.gz
      d="${f#jaynet-backup-}"; d="${d%%-*}"
      if [ "$(date -d "$d" +%u 2>/dev/null || echo 0)" = "7" ] && [ "$i" -lt 4 ]; then
        keep="$keep $f"; i=$((i+1))
      else
        rm -f -- "$f"
      fi
    done; }
echo "[backup] done — kept: $(ls -1 jaynet-backup-*.tar.gz 2>/dev/null | wc -l) archive(s) in $OUT"
