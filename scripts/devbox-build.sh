#!/usr/bin/env bash
# Build the JayNet devbox toolchain image (see containers/devbox/Containerfile).
# The devbox gives code.run real build environments (rust/go/node/C/C++/java)
# in a per-run podman container instead of the host-limited firejail sandbox.
# Enable afterwards: tools.code.devbox.enabled: true in config/runtime.yaml.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="${1:-jaynet-devbox:latest}"
if ! command -v podman >/dev/null 2>&1; then
  echo "ERROR: podman not found — install it first (e.g. pacman -S podman / apt install podman)" >&2
  exit 1
fi

echo "==> Building $IMAGE (this pulls a base image + toolchains; a few minutes the first time)"
podman build -t "$IMAGE" -f containers/devbox/Containerfile containers/devbox
echo "==> Done. Enable the devbox in config/runtime.yaml:"
echo "      tools.code.devbox.enabled: true"
