#!/usr/bin/env bash
# Local mirror of CI's "pip-audit dependency lockfiles" step — run this
# before committing so a known-vulnerability finding never reaches GitHub.
# litellm <=1.93 needs Python <3.14, so its lock is audited under 3.13
# (same as .github/workflows/ci.yml).
set -euo pipefail
cd "$(dirname "$0")/.."

uv run --with pip-audit pip-audit -r requirements.lock --no-deps
uv run --with pip-audit pip-audit -r requirements-tools.lock --no-deps
uv run --python 3.13 --with pip-audit \
  pip-audit -r requirements-litellm.lock --no-deps
echo "dep-audit: OK — all three lockfiles clean"
