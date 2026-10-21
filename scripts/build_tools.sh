#!/bin/bash
# ============================================================================
#  build_tools.sh -- Build llama.cpp, stable-diffusion.cpp, whisper.cpp,
#                     piper TTS, omnivoice.cpp (OmniVoice TTS), vLLM (ROCm,
#                     from source), the Strata HIP engine, and the
#                     llm-tools venv
#  Backends:  vulkan (default; AMD/NVIDIA/Intel/mixed, no vendor SDK),
#             rocm (AMD), cuda (NVIDIA), sycl (Intel oneAPI),
#             cpu (no GPU; AVX2/AVX512/AMX via -march=native)
#
#  Usage:
#    ./build_tools.sh                    # show this help (builds nothing)
#    ./build_tools.sh all                # all projects, asks which backends
#    ./build_tools.sh llama rocm         # just llama.cpp ROCm (skips the menu)
#    ./build_tools.sh sd vulkan          # just stable-diffusion.cpp Vulkan
#    ./build_tools.sh whisper cuda       # just whisper.cpp CUDA (NVIDIA)
#    ./build_tools.sh llama sycl         # just llama.cpp SYCL (Intel oneAPI)
#    ./build_tools.sh rocm               # all projects, ROCm only
#    ./build_tools.sh piper              # piper TTS (venv install, no GPU backend)
#    ./build_tools.sh tools              # llm-tools venv (hf download tooling)
#    ./build_tools.sh --clean llama cuda # nuke build dir first, then build
#    ./build_tools.sh k2horizon rocm     # patched K2-Horizon llama.cpp fork
#    ./build_tools.sh spark rocm         # Spark-X2.5 llama.cpp fork (spark2_5 arch)
#    ./build_tools.sh bonsai rocm        # PrismML llama.cpp fork, prism branch
#                                         # (ternary PQ2_0/PTQ1_0 + Hadamard runtime)
#    ./build_tools.sh omnivoice rocm     # OmniVoice TTS (omnivoice.cpp port:
#                                         #  tts-server, omnivoice-tts, omnivoice-codec)
#    ./build_tools.sh vllm rocm          # vLLM from source for ROCm (opt-in, heavy:
#                                         #  ~20-40 min first build, multi-GB venv;
#                                         #  needs python3.12 + ROCm at /opt/rocm)
#    ./build_tools.sh strata rocm        # Strata HIP engine for Qwen3.8-Flash-Next
#                                         #  (engine only, no model download; upstream's
#                                         #  setup.sh does the full install + server)
#    ./build_tools.sh radiance rocm      # radiance C++/HIP server (RDNA4: gfx1200/1201;
#                                         #  OpenAI/Anthropic/llama-server APIs, .rad
#                                         #  containers; codeberg.org/StillDeadcode/radiance)
#    ./build_tools.sh update             # rebuild everything already built
#                                         # (every source tree/venv present —
#                                         #  vanilla trees sync to latest, the
#                                         #  forks keep their pinned commits;
#                                         #  add --clean to rebuild from scratch)
#    ./build_tools.sh delete             # interactive picker: remove builds
#                                         #  (source tree + installed prefixes —
#                                         #   deleting the TREE is what stops
#                                         #   `update` from rebuilding it)
#    ./build_tools.sh delete llama rocm  # remove one build directly
#    ./build_tools.sh delete --stale     # prune old versioned prefixes only
#                                         #  (keeps each current symlink target)
#    -y/--yes: skip the delete confirmation prompt
#
#  Projects:  llama, sd, whisper, piper, tools, k2horizon, spark, bonsai,
#             omnivoice, vllm, strata, radiance, all
#             (k2horizon/spark/bonsai/omnivoice/vllm/strata/radiance are NOT
#             part of "all" -- experimental forks / opt-in tools)
#  Special:   update -- rebuild every tree/venv already present (see above)
#  Backends:  rocm, vulkan, cuda, sycl, cpu
#             (omitted: menu; non-interactive: vulkan)
#  Env vars:  JOBS=16                 parallelism (default: nproc)
#             BIN_DIR=/opt/x         install dir for built binaries (skips prompt;
#                                        last choice persists in ~/.config/jaynet-build-tools.env)
#             BUILD_HOME=~/builds    work area for source trees/venvs/patches
#                                        (default ~/jaynet-build — outside the repo)
#             AMDGPU_TARGETS=gfx1100 AMD gfx target for ROCm builds (skips prompt)
#             TORCH_INDEX_URL=...    pytorch wheel index for the vllm ROCm torch
#                                        (default https://download.pytorch.org/whl/rocm7.2)
#             CUDA_ARCHS=89          CUDA arch list (default "native" = auto)
#             MARCH=znver4           CPU -march for C/C++ (default "native")
#             PIPER_VOICE_DIR=...    piper voices dir (default ~/jaynet-models/piper)
#
#  At startup the script asks where to install the built binaries
#  (default: ~/jaynet-bin), then which backends + GPU targets to build for.
#  llama.cpp/whisper.cpp install as self-contained VERSIONED prefixes
#  ($BIN_DIR/llama.cpp.rocm-b10343/{bin,lib}) with a stable symlink
#  ($BIN_DIR/llama.cpp.rocm -> ...) flipped after each build — a rebuild
#  never yanks libs from under a running server, rollback is one ln -sf.
# ============================================================================

set -euo pipefail

# -- Paths ------------------------------------------------------------------
# SCRIPT_DIR = where this script lives (inside the JayNet repo) — only for
# files the repo SHIPS (the llm-tools requirements list). BUILD_HOME = the
# work area: all cloned source trees, build dirs, venvs and patches live
# there, OUTSIDE the JayNet checkout so a repo update/re-clone never touches
# them (multi-GB trees). Default ~/jaynet-build; set BUILD_HOME to override.
# Migrating from the standalone helper_scripts repo: BUILD_HOME=/path/to/
# helper_scripts ./build_tools.sh update rebuilds the old trees in place, or
# move them: mv ~/helper_scripts/{llama,stable-diffusion,whisper,omnivoice}*/
#   ~/helper_scripts/piper ~/helper_scripts/llm-tools ~/helper_scripts/patches
#   ~/jaynet-build/
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_HOME="${BUILD_HOME:-${HOME}/jaynet-build}"
BASE_DIR="$BUILD_HOME"   # all trees/venvs/patches anchor on the work area
ROCM_DIR="${BASE_DIR}/llama.cpp-rocm"
VULKAN_DIR="${BASE_DIR}/llama.cpp-vulkan"
CUDA_DIR="${BASE_DIR}/llama.cpp-cuda"
SYCL_DIR="${BASE_DIR}/llama.cpp-sycl"
SD_ROCM_DIR="${BASE_DIR}/stable-diffusion.cpp-rocm"
SD_VULKAN_DIR="${BASE_DIR}/stable-diffusion.cpp-vulkan"
SD_CUDA_DIR="${BASE_DIR}/stable-diffusion.cpp-cuda"
SD_SYCL_DIR="${BASE_DIR}/stable-diffusion.cpp-sycl"
WHISPER_ROCM_DIR="${BASE_DIR}/whisper.cpp-rocm"
WHISPER_VULKAN_DIR="${BASE_DIR}/whisper.cpp-vulkan"
WHISPER_CUDA_DIR="${BASE_DIR}/whisper.cpp-cuda"
WHISPER_SYCL_DIR="${BASE_DIR}/whisper.cpp-sycl"
# CPU-only trees: no GPU backend flags at all; llama.cpp's GGML_NATIVE plus
# the -march flag below give the host's best SIMD paths (AVX2/AVX512/AMX).
CPU_DIR="${BASE_DIR}/llama.cpp-cpu"
SD_CPU_DIR="${BASE_DIR}/stable-diffusion.cpp-cpu"
WHISPER_CPU_DIR="${BASE_DIR}/whisper.cpp-cpu"
# K2-Horizon fork: upstream llama.cpp has no k2-horizon arch support yet; the
# model cards (e.g. vincespeed/K2-Horizon-MoVA-36B-A4B-APEX-GGUF) require the
# MBZUAI-IFM fork at a pinned commit plus two patches (MSVC tokenizer regex --
# Windows-only but harmless here; parallel-norm residual wiring -- required on
# every OS, otherwise garbage "> > >" output). NOT synced to HEAD like the
# other trees: the pin is the point.
K2_REPO="https://github.com/MBZUAI-IFM/llama.cpp"
K2_COMMIT="35999d101cf2233fc54f09c3c8d599da7303ce02"
K2_PATCH_DIR="${BASE_DIR}/patches/k2horizon"
# Pinned to a fixed HF revision (not resolve/main) + sha256-checked: these
# patches are code we apply to a tree we build and run.
K2_PATCH_BASE="https://huggingface.co/vincespeed/K2-Horizon-MoVA-36B-A4B-APEX-GGUF/resolve/ecdc6f157e7db48bfcafe003d05befd79d28ca0b/patches"
K2_PATCHES=(0001-fix-k2-horizon-msvc-regex.patch 0002-fix-k2-horizon-parallel-norm.patch)
declare -A K2_PATCH_SHA256=(
    [0001-fix-k2-horizon-msvc-regex.patch]=09dea72b9b2cd882322db723edf3a1eb8358439bd18e1ce94bbbae100c72797a
    [0002-fix-k2-horizon-parallel-norm.patch]=db6028f2d133a0bdcfc69150c455221c565f79bb9202a9eee6aefe38c4bb0825
)
K2_ROCM_DIR="${BASE_DIR}/llama.cpp-k2horizon-rocm"
K2_VULKAN_DIR="${BASE_DIR}/llama.cpp-k2horizon-vulkan"
K2_CUDA_DIR="${BASE_DIR}/llama.cpp-k2horizon-cuda"
K2_CPU_DIR="${BASE_DIR}/llama.cpp-k2horizon-cpu"
# Spark-X2.5 fork: mainline llama.cpp has no spark2_5 arch (hybrid sliding-
# window attention) — the model card (XHToken/Spark-X2.5-4B) points at the
# author's fork. Pinned to a reviewed commit like K2: everything built here
# ends up running inside JayNet, so `update` must not silently pull new
# upstream code. To move the pin: review the fork's new commits, then bump
# SPARK_COMMIT.
SPARK_REPO="https://github.com/XHToken/llama.cpp"
SPARK_COMMIT="4a3635c32fc9f044c2bde9ebeabf50c7e1ec5991"
SPARK_ROCM_DIR="${BASE_DIR}/llama.cpp-spark-rocm"
SPARK_CPU_DIR="${BASE_DIR}/llama.cpp-spark-cpu"
# PrismML fork (prism branch): Ternary-Bonsai models need the custom ternary
# kernels + Hadamard activation runtime — stock llama.cpp rejects PQ2_0 /
# PTQ1_0 (model card: prism-ml/Ternary-Bonsai-2-27B-gguf). ROCm works (RDNA3
# confirmed upstream). Pinned like the other forks (see SPARK above).
BONSAI_REPO="https://github.com/PrismML-Eng/llama.cpp"
# (the pin is a commit on the fork's prism branch — the ternary kernels live
# there, the default branch just tracks upstream)
BONSAI_COMMIT="e3de715222b2667c54a267441159a5729f11155e"
BONSAI_ROCM_DIR="${BASE_DIR}/llama.cpp-bonsai-rocm"
BONSAI_CPU_DIR="${BASE_DIR}/llama.cpp-bonsai-cpu"
# OmniVoice TTS (omnivoice.cpp): GGML port of k2-fsa/OmniVoice — massively
# multilingual zero-shot TTS with voice cloning + voice design. Ships
# tts-server (OpenAI-compatible HTTP with a cloned-voice registry),
# omnivoice-tts and omnivoice-codec. ggml is a submodule (recursive clone);
# GGML_HIP/GGML_VULKAN/GGML_CUDA pass straight through to it (whisper-style),
# so no upstream ROCm build script is needed. Binaries land in build/ ROOT,
# not build/bin — install_omni_prefix handles that. NOT part of "all".
# Pinned to the commit we validated live (see SPARK above for how to bump).
OMNI_REPO="https://github.com/ServeurpersoCom/omnivoice.cpp"
OMNI_COMMIT="2c32cd336881ef12fcf69245d0cedce2e4d19d6c"
OMNI_ROCM_DIR="${BASE_DIR}/omnivoice.cpp-rocm"
OMNI_VULKAN_DIR="${BASE_DIR}/omnivoice.cpp-vulkan"
OMNI_CUDA_DIR="${BASE_DIR}/omnivoice.cpp-cuda"
OMNI_CPU_DIR="${BASE_DIR}/omnivoice.cpp-cpu"
# vLLM (ROCm-only, opt-in -- NOT part of "all"): source build of the vanilla
# vllm-project/vllm tree (no pin -- syncs to HEAD on `update` like the other
# vanilla trees). Python package with a HIP/CMake native extension + Rust
# frontend, so the build is an editable pip install into a dedicated
# python3.12 venv (torch ROCm wheels target <=3.13 -- the distro's newer
# system python won't do), NOT a bin/lib prefix: the $BIN_DIR side is a tiny
# wrapper + symlink (piper/llm-tools analogue). Layout: VLLM_ROCM_DIR holds
# the clone (vllm/) and the venv (.venv/) side by side. TORCH_INDEX_URL lets
# an AMD wheel-index rev override the default rocm7.2.
VLLM_ROCM_DIR="${BASE_DIR}/vllm-rocm"
VLLM_SRC_DIR="${VLLM_ROCM_DIR}/vllm"
VLLM_VENV="${VLLM_ROCM_DIR}/.venv"
TORCH_INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/rocm7.2}"
# Strata (ROCm/HIP-only, opt-in -- NOT part of "all"): inference engine for
# Qwen3.8-Flash-Next (125B MoE) on consumer PCs -- llama.cpp/ggml-derived,
# GPU holds the hot experts, RAM/SSD the rest. Vanilla clone of
# Niko1221/Strata (syncs to HEAD on `update` like the other vanilla trees).
# We build the engine ONLY (cmake, docs/AMD_HIP.md): upstream's
# `./setup.sh --backend hip` also works but installs TheRock ROCm wheels into
# a venv, downloads the ~60-70 GB model pack and starts a server -- none of
# which a build tool should do. The engine binary lands in build-hip/ ROOT
# (not build/bin) like omnivoice -- install_strata_prefix handles that.
STRATA_ROCM_DIR="${BASE_DIR}/strata-rocm"
# radiance (codeberg.org/StillDeadcode/radiance): C++/HIP inference server for
# RDNA4, OpenAI-compatible. ROCm-only here (its AVX host backend is a fallback
# upstream provides, not a JayNet target). Installs a real prefix (bin +
# share/radiance plugins) via cmake --install -- the plugins must sit next to
# the binary, so a bare binary copy like strata's is NOT enough.
RADIANCE_ROCM_DIR="${BASE_DIR}/radiance-rocm"
# GPU/CPU tuning knobs. Empty here = asked later, but only when the matching
# backend is actually in the build plan; env presets skip the prompts.
#   AMDGPU_TARGETS: gfx target for ROCm builds (e.g. gfx1100, gfx1201)
#   CUDA_ARCHS:     CUDA arch list; "native" lets nvcc auto-detect
#   MARCH:          -march for C/C++ files ("native" = this host's CPU)
AMDGPU_TARGETS="${AMDGPU_TARGETS:-}"
CUDA_ARCHS="${CUDA_ARCHS:-}"
MARCH="${MARCH:-native}"
# Piper TTS (piper1-gpl) is a Python package -- no GPU backend, no CMake.
# Installed into a dedicated venv; voices (onnx + onnx.json) live with the
# other models so the orchestrator's voice.tts.command can reference them.
PIPER_VENV="${BASE_DIR}/piper/.venv"
PIPER_VOICE_DIR="${PIPER_VOICE_DIR:-${HOME}/jaynet-models/piper}"
PIPER_VOICE="en_US-lessac-high"
PIPER_VOICE_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/high"
# llm-tools venv: Python tooling for this repo — the `hf` CLI (+ fast xet
# transfers) for model downloads. JayNet users can also pull models with
# JayNet's own scripts/pull-model or the admin downloader. Kept minimal on
# purpose -- the finetuning stack lives elsewhere (own venv).
TOOLS_VENV="${BASE_DIR}/llm-tools"
TOOLS_REQUIREMENTS="${SCRIPT_DIR}/build_tools.requirements.txt"
# Shared install location for the SD web UI frontend. The Vite project lives
# inside each SD source tree at examples/server/frontend, but the built
# artifacts are copied here so both backends use the same canonical path
# via --serve-html-path.
SD_FRONTEND_INSTALL_DIR="${BASE_DIR}/stable-diffusion/web/server/frontend"
SD_FRONTEND_HTML="${SD_FRONTEND_INSTALL_DIR}/dist/index.html"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 8)}"

# -- Args -------------------------------------------------------------------
# Parse projects (llama, sd, whisper, piper, tools, all) and backends
# (rocm, vulkan, cuda, sycl) independently. If only backends specified, all
# projects are built. If only projects specified, the default backends
# (rocm+vulkan) are built. No args at all shows the usage header and exits.
# piper and tools are backend-less: each produces a single job.
usage() { sed -n '/^#  Usage:/,/====/p' "$0"; }

if [[ $# -eq 0 ]]; then
    usage
    exit 0
fi

PROJECTS=()
BACKENDS=()
CLEAN=0
UPDATE=0
DELETE=0
STALE=0
YES=0
for arg in "$@"; do
    case "$arg" in
        llama|sd|whisper|piper|tools|k2horizon|spark|bonsai|omnivoice|vllm|strata|radiance) PROJECTS+=("$arg") ;;
        all) PROJECTS+=(llama sd whisper piper tools) ;;
        rocm|vulkan|cuda|sycl|cpu) BACKENDS+=("$arg") ;;
        update) UPDATE=1 ;;
        delete) DELETE=1 ;;
        --stale) STALE=1 ;;
        --clean|-c)  CLEAN=1 ;;
        -y|--yes)  YES=1 ;;
        -h|--help)
            usage
            exit 0
            ;;
        *) echo "Unknown arg: $arg (try --help)"; exit 1 ;;
    esac
done
[[ ${#PROJECTS[@]} -eq 0 && $UPDATE -eq 0 && $DELETE -eq 0 ]] && PROJECTS=(llama sd whisper piper tools)
# BACKENDS stays empty here on purpose: the menu (after the install-dir
# prompt) or the non-interactive default fills it in before JOBS_LIST is built.

# -- Colors -----------------------------------------------------------------
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

step() { echo -e "\n${CYAN}${BOLD}==>${NC} ${BOLD}$*${NC}"; }
ok()   { echo -e "${GREEN}OK${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC}  $*"; }
fail() { echo -e "${RED}FAIL${NC} $*"; exit 1; }
# Install hints name both supported distro families: pkg_hint <pacman pkgs>
# <apt pkgs> → "sudo pacman -S a b  |  sudo apt install a b".
pkg_hint() { echo "sudo pacman -S $1  |  sudo apt install $2"; }

# Binaries must also work after the source/build tree is deleted: CMake puts
# the build/bin dir into RUNPATH, which dies with the tree. Adding $ORIGIN
# variants makes build-tree AND installed-prefix layouts (bin + sibling lib)
# self-contained. CMake appends its own entries after these, so build-tree
# execution keeps working too.
ORIGIN_RPATH=(-DCMAKE_BUILD_RPATH='\$ORIGIN;\$ORIGIN/../lib')

# -- Install dir --------------------------------------------------------------
# Ask where to install the built binaries. Default: the dir you used last
# time (persisted in $_BIN_STATE), else ~/jaynet-bin. BIN_DIR env overrides
# both; the prompt is also skipped when stdin is not a tty (then the
# persisted/default dir is used silently). The persistence exists because a
# re-clone must not silently relocate the install dir — JayNet's binary
# registry points at the stable symlinks under $BIN_DIR.
_BIN_STATE="${XDG_CONFIG_HOME:-${HOME}/.config}/jaynet-build-tools.env"
if [[ -z "${BIN_DIR:-}" && -f "$_BIN_STATE" ]]; then
    source "$_BIN_STATE"   # sets BIN_DIR
fi
BIN_DIR="${BIN_DIR:-}"
if [[ -z "$BIN_DIR" ]]; then
    if [[ -t 0 ]]; then
        read -r -p "Install directory for built binaries [${HOME}/jaynet-bin]: " BIN_DIR
        BIN_DIR="${BIN_DIR:-${HOME}/jaynet-bin}"
    else
        BIN_DIR="${HOME}/jaynet-bin"
    fi
fi
# read/env values keep a typed ~ literal; expand it so ~/bin doesn't
# become a literal ./~/bin directory
BIN_DIR="${BIN_DIR/#\~/${HOME}}"
BIN_DIR="${BIN_DIR%/}"
mkdir -p "$BIN_DIR"
mkdir -p "$(dirname "$_BIN_STATE")"
printf 'BIN_DIR=%q\n' "$BIN_DIR" > "$_BIN_STATE"

# Copy built binaries into $BIN_DIR. Args: <build/bin dir> <binary>...
# NOTE: stub-copy only — the copied binaries keep their RUNPATH into the
# build tree, so they break when the source dir is deleted. Fine for sd.cpp
# (builds mostly-static); llama.cpp AND whisper.cpp use install_prefix
# instead (both are served long-running by JayNet).
install_bins() {
    local src="$1"; shift
    local f
    for f in "$@"; do
        if [[ -x "$src/$f" ]]; then
            cp "$src/$f" "$BIN_DIR/$f"
            ok "Installed $f -> $BIN_DIR/$f"
        fi
    done
}

# Symlink a venv CLI into $BIN_DIR (copying would break the venv shebang).
install_link() {
    local target="$1"
    local name
    name="$(basename "$target")"
    if [[ -x "$target" ]]; then
        ln -sf "$target" "$BIN_DIR/$name"
        ok "Linked $name -> $BIN_DIR/$name"
    fi
}

# Install a cmake build as a self-contained, versioned prefix and flip a
# stable symlink to it: $BIN_DIR/<family>.<backend>-<tag>/{bin,lib} with
# $BIN_DIR/<family>.<backend> -> that dir. We copy build/bin BY HAND instead
# of cmake --install: upstream writes install rules for every tool (even the
# ones we never build), so --install dies mid-run with a half-populated
# prefix. Reinstalling into the SAME prefix would leave stale versioned
# libs and can mix ABIs — versioned prefixes sidestep that, never yank libs
# from under a running server, make the flip atomic, and rollback is one
# `ln -sf` back. Delete old versions by hand.
# Args: <source tree> <family> <backend> <binary that must exist>
install_prefix() {
    local src_dir="$1" family="$2" backend="$3" want="$4"
    local tag dest link
    tag="$(git -C "$src_dir" describe --tags --always 2>/dev/null || echo unknown)"
    dest="$BIN_DIR/${family}.${backend}-${tag}"
    link="$BIN_DIR/${family}.${backend}"
    step "Install ${family} (${backend}) -> ${dest}"
    rm -rf "$dest"
    mkdir -p "$dest/bin" "$dest/lib"
    # shared libs (+ their symlinks) -> lib, executables -> bin
    local f
    for f in "$src_dir"/build/bin/*.so*; do
        [[ -e "$f" ]] && cp -a "$f" "$dest/lib/"
    done
    find "$src_dir/build/bin" -maxdepth 1 -type f -executable ! -name '*.so*' \
        -exec cp -a {} "$dest/bin/" \;
    [[ -x "$dest/bin/$want" ]] || fail "install produced no $want in $dest/bin"
    # An old stub-copy install leaves a REAL directory at the link path.
    [[ -d "$link" && ! -L "$link" ]] && rm -rf "$link"
    ln -sfn "$dest" "$link"
    ok "$family $backend: $link -> ${dest##*/}"
    ok "use: $link/bin/$want  (its lib/ must be on LD_LIBRARY_PATH —"
    ok "  JayNet's start-model.sh prepends <bin>/../lib automatically)"
}

# -- Existing-build detection ---------------------------------------------------
# Every source tree/venv under this checkout maps 1:1 to a (project, backend)
# job. Shared by `update` (what to rebuild) and `delete` (what can go).
existing_jobs() {
    local pair
    for pair in \
        "$ROCM_DIR:llama-rocm"           "$VULKAN_DIR:llama-vulkan" \
        "$CUDA_DIR:llama-cuda"           "$SYCL_DIR:llama-sycl" \
        "$CPU_DIR:llama-cpu" \
        "$SD_ROCM_DIR:sd-rocm"           "$SD_VULKAN_DIR:sd-vulkan" \
        "$SD_CUDA_DIR:sd-cuda"           "$SD_SYCL_DIR:sd-sycl" \
        "$SD_CPU_DIR:sd-cpu" \
        "$WHISPER_ROCM_DIR:whisper-rocm" "$WHISPER_VULKAN_DIR:whisper-vulkan" \
        "$WHISPER_CUDA_DIR:whisper-cuda" "$WHISPER_SYCL_DIR:whisper-sycl" \
        "$WHISPER_CPU_DIR:whisper-cpu" \
        "$K2_ROCM_DIR:k2horizon-rocm"    "$K2_VULKAN_DIR:k2horizon-vulkan" \
        "$K2_CUDA_DIR:k2horizon-cuda"    "$K2_CPU_DIR:k2horizon-cpu" \
        "$SPARK_ROCM_DIR:spark-rocm"     "$SPARK_CPU_DIR:spark-cpu" \
        "$BONSAI_ROCM_DIR:bonsai-rocm"   "$BONSAI_CPU_DIR:bonsai-cpu" \
        "$OMNI_ROCM_DIR:omnivoice-rocm"   "$OMNI_VULKAN_DIR:omnivoice-vulkan" \
        "$OMNI_CUDA_DIR:omnivoice-cuda"   "$OMNI_CPU_DIR:omnivoice-cpu" \
        "$VLLM_SRC_DIR:vllm-rocm" \
        "$STRATA_ROCM_DIR:strata-rocm" \
        "$RADIANCE_ROCM_DIR:radiance-rocm" \
    ; do
        [[ -d "${pair%%:*}/.git" ]] && echo "${pair##*:}"
    done
    [[ -d "$PIPER_VENV" ]] && echo piper
    [[ -d "$TOOLS_VENV/bin" ]] && echo tools
    return 0
}

# -- Update mode ---------------------------------------------------------------
# `update` rebuilds exactly what's already built: every source tree/venv under
# this checkout maps 1:1 to a (project, backend) job. Vanilla trees sync to
# latest; the forks re-pin at their reviewed commits (K2 also re-applies its
# patches) by design. Nothing is cloned
# here — a tree that doesn't exist was never built and stays untouched.
if [[ $UPDATE -eq 1 ]]; then
    JOBS_LIST=()
    while IFS= read -r j; do JOBS_LIST+=("$j"); done < <(existing_jobs)
    [[ ${#JOBS_LIST[@]} -eq 0 ]] \
        && fail "update: no existing builds found under $BASE_DIR — run a normal build first"
    # PROJECTS/BACKENDS only feed the banner + menu from here on; reconstruct
    # them (deduped) so the plan line reads sensibly.
    PROJECTS=(); BACKENDS=()
    for j in "${JOBS_LIST[@]}"; do
        case "$j" in
            piper|tools) PROJECTS+=("$j") ;;
            *) [[ " ${PROJECTS[*]} " == *" ${j%%-*} "* ]] || PROJECTS+=("${j%%-*}")
               [[ " ${BACKENDS[*]} " == *" ${j##*-} "* ]] || BACKENDS+=("${j##*-}") ;;
        esac
    done
    step "Update plan (existing builds): ${JOBS_LIST[*]}"
fi

# -- Delete mode ---------------------------------------------------------------
# `delete` removes a build completely: the source tree under this checkout
# AND its installed artifacts in $BIN_DIR. Removing the TREE is the point —
# `update` keys on trees, so deleting only the binaries (as done by hand
# before) left update rebuilding them on the next run.
if [[ $DELETE -eq 1 ]]; then
    # Job name -> paths to remove (one per line). Prefix families install as
    # <family>.<backend>-<tag>/ + a <family>.<backend> symlink; sd installs
    # flat binaries shared across backends (removed only with the LAST sd
    # tree); piper/tools are venvs + CLI links (piper voices are downloaded
    # assets and are KEPT).
    job_paths() {
        local job="$1" proj be family src
        case "$job" in
            piper) echo "$PIPER_VENV"; [[ -L $BIN_DIR/piper ]] && echo "$BIN_DIR/piper"; return ;;
            tools) echo "$TOOLS_VENV"; [[ -L $BIN_DIR/hf ]] && echo "$BIN_DIR/hf"; return ;;
            # vllm: clone+venv tree, the wrapper dir, and the vllm symlink
            vllm-rocm)
                echo "$VLLM_ROCM_DIR"
                [[ -d "$BIN_DIR/vllm-rocm" ]] && echo "$BIN_DIR/vllm-rocm"
                [[ -L "$BIN_DIR/vllm" ]] && echo "$BIN_DIR/vllm"
                return 0 ;;
        esac
        proj="${job%%-*}"; be="${job##*-}"
        case "$proj" in
            llama)     src="$BASE_DIR/llama.cpp-$be";              family="llama.cpp" ;;
            whisper)   src="$BASE_DIR/whisper.cpp-$be";            family="whisper.cpp" ;;
            sd)        src="$BASE_DIR/stable-diffusion.cpp-$be";   family="" ;;
            k2horizon) src="$BASE_DIR/llama.cpp-k2horizon-$be";    family="llama.cpp-k2horizon" ;;
            spark)     src="$BASE_DIR/llama.cpp-spark-$be";        family="llama.cpp-spark" ;;
            bonsai)    src="$BASE_DIR/llama.cpp-bonsai-$be";       family="llama.cpp-bonsai" ;;
            omnivoice) src="$BASE_DIR/omnivoice.cpp-$be";          family="omnivoice.cpp" ;;
            strata)    src="$BASE_DIR/strata-$be";                 family="strata" ;;
            radiance)  src="$BASE_DIR/radiance-$be";               family="radiance" ;;
            *) return 1 ;;
        esac
        echo "$src"
        [[ -z "$family" ]] && return 0
        local d
        for d in "$BIN_DIR/${family}.${be}"-*/; do
            [[ -d "$d" ]] && echo "${d%/}"
        done
        [[ -L "$BIN_DIR/${family}.${be}" || -e "$BIN_DIR/${family}.${be}" ]] \
            && echo "$BIN_DIR/${family}.${be}"
        return 0
    }

    # Running processes holding a path open (exe or mapped libs).
    procs_using() {
        local path="$1" p exe
        for p in /proc/[0-9]*; do
            exe="$(readlink "$p/exe" 2>/dev/null)" || continue
            if [[ "$exe" == "$path"* ]]; then
                echo "${p#/proc/} $(cat "$p/comm" 2>/dev/null)"
            elif grep -qm1 "$path" "$p/maps" 2>/dev/null; then
                echo "${p#/proc/} $(cat "$p/comm" 2>/dev/null)"
            fi
        done
        return 0
    }

    human_size() { numfmt --to=iec --suffix=B "$1" 2>/dev/null || echo "${1}B"; }
    paths_size() {
        local t=0 p s
        for p in "$@"; do
            s="$(du -sb "$p" 2>/dev/null | cut -f1)"
            t=$((t + ${s:-0}))
        done
        echo "$t"
    }

    # ---- stale-prefix prune: old versioned dirs, keep current targets ----
    if [[ $STALE -eq 1 ]]; then
        STALE_DIRS=()
        for link in "$BIN_DIR"/*; do
            [[ -L "$link" ]] || continue
            tgt="$(readlink "$link")"; base="${link##*/}"
            [[ "${tgt##*/}" == "$base"-* ]] || continue
            for d in "$BIN_DIR/$base"-*/; do
                [[ -d "$d" && "${d%/}" != "$tgt" ]] && STALE_DIRS+=("${d%/}")
            done
        done
        [[ ${#STALE_DIRS[@]} -eq 0 ]] && { ok "no stale versioned prefixes under $BIN_DIR"; exit 0; }
        step "Stale versioned prefixes (current symlink targets are KEPT):"
        for d in "${STALE_DIRS[@]}"; do
            printf "  %-60s %s\n" "$d" "$(human_size "$(du -sb "$d" | cut -f1)")"
        done
        echo "  total: $(human_size "$(paths_size "${STALE_DIRS[@]}")")"
        if [[ $YES -ne 1 ]]; then
            read -r -p "Delete ${#STALE_DIRS[@]} stale prefixes? [y/N] " ans
            [[ "$ans" == y || "$ans" == Y ]] || { echo "aborted"; exit 0; }
        fi
        rm -rf "${STALE_DIRS[@]}"
        ok "pruned ${#STALE_DIRS[@]} stale prefixes"
        exit 0
    fi

    # ---- job deletion ----
    ALL_JOBS=()
    while IFS= read -r j; do ALL_JOBS+=("$j"); done < <(existing_jobs)
    [[ ${#ALL_JOBS[@]} -eq 0 ]] \
        && fail "delete: no existing builds found under $BASE_DIR"

    SELECTED=()
    if [[ ${#PROJECTS[@]} -gt 0 || ${#BACKENDS[@]} -gt 0 ]]; then
        # Direct selection: project/backend args filter the existing jobs;
        # an omitted side is a wildcard (`delete llama` = every llama build).
        for j in "${ALL_JOBS[@]}"; do
            case "$j" in
                piper|tools)
                    [[ ${#PROJECTS[@]} -eq 0 || " ${PROJECTS[*]} " == *" $j "* ]] \
                        && SELECTED+=("$j") ;;
                *)  [[ ${#PROJECTS[@]} -eq 0 || " ${PROJECTS[*]} " == *" ${j%%-*} "* ]] \
                    && [[ ${#BACKENDS[@]} -eq 0 || " ${BACKENDS[*]} " == *" ${j##*-} "* ]] \
                        && SELECTED+=("$j") ;;
            esac
        done
        [[ ${#SELECTED[@]} -eq 0 ]] \
            && fail "delete: no existing build matches (present: ${ALL_JOBS[*]})"
    else
        [[ -t 0 ]] || fail "delete: no projects given and not interactive"
        echo "Builds present (space-separated numbers, 'a' = all):"
        i=1
        for j in "${ALL_JOBS[@]}"; do
            mapfile -t _p < <(job_paths "$j")
            printf "  %2d) %-18s %s\n" "$i" "$j" \
                "$(human_size "$(paths_size "${_p[@]}")")"
            i=$((i + 1))
        done
        read -r -p "> " pick
        [[ -z "$pick" ]] && { echo "aborted"; exit 0; }
        [[ "$pick" == a || "$pick" == A ]] && pick="$(seq -s' ' 1 ${#ALL_JOBS[@]})"
        for n in $pick; do
            [[ "$n" =~ ^[0-9]+$ && $n -ge 1 && $n -le ${#ALL_JOBS[@]} ]] \
                || fail "delete: invalid selection: $n"
            SELECTED+=("${ALL_JOBS[$((n - 1))]}")
        done
    fi

    # Plan: every path, its size, any process holding it open. sd's flat
    # binaries (sd, sd-cli, sd-server) are shared across sd backends — they
    # only go once the last sd tree is deleted.
    sd_going=0; sd_staying=0
    for j in "${SELECTED[@]}"; do [[ "$j" == sd-* ]] && sd_going=1; done
    for j in "${ALL_JOBS[@]}"; do
        [[ "$j" == sd-* && " ${SELECTED[*]} " != *" $j "* ]] && sd_staying=1
    done
    step "Delete plan:"
    PLAN=(); IN_USE=0
    for j in "${SELECTED[@]}"; do
        while IFS= read -r p; do PLAN+=("$p"); done < <(job_paths "$j")
        if [[ "$j" == sd-* ]]; then
            if [[ $sd_staying -eq 0 ]]; then
                for f in sd sd-cli sd-server; do
                    [[ -e "$BIN_DIR/$f" ]] && PLAN+=("$BIN_DIR/$f")
                done
            else
                warn "sd flat binaries ($BIN_DIR/sd{,-cli,-server}) stay: another sd build remains"
            fi
        fi
        [[ "$j" == piper ]] && warn "piper voices in $PIPER_VOICE_DIR are kept (downloaded assets)"
    done
    for p in "${PLAN[@]}"; do
        printf "  %-64s %s\n" "$p" "$(human_size "$(du -sb "$p" 2>/dev/null | cut -f1 || echo 0)")"
        users="$(procs_using "$p")"
        if [[ -n "$users" ]]; then
            IN_USE=1
            echo -e "${RED}    in use by:${NC} $(echo "$users" | tr '\n' ' ')"
        fi
    done
    echo "  total: $(human_size "$(paths_size "${PLAN[@]}")") over ${#PLAN[@]} paths"
    [[ $IN_USE -eq 1 ]] \
        && warn "paths are held by running processes — their binaries keep running, but a restart/swap will fail; stop them first (or -y to delete anyway)"
    if [[ $YES -ne 1 ]]; then
        read -r -p "Delete ${#PLAN[@]} paths? [y/N] " ans
        [[ "$ans" == y || "$ans" == Y ]] || { echo "aborted"; exit 0; }
    fi
    rm -rf "${PLAN[@]}"
    ok "deleted ${#PLAN[@]} paths (${SELECTED[*]}) — \`update\` will no longer touch them"
    exit 0
fi


# -- Backend selection ----------------------------------------------------------
# GPU-less projects (piper, tools) skip this entirely. Menu default = vulkan:
# one build runs on AMD/NVIDIA/Intel (incl. mixed-vendor splits) with no vendor
# SDK. Backends given on the command line skip the menu; non-tty -> vulkan.
GPU_PROJECTS=0
for p in "${PROJECTS[@]}"; do
    [[ "$p" == "piper" || "$p" == "tools" ]] || GPU_PROJECTS=1
done

if [[ ${#BACKENDS[@]} -eq 0 && $UPDATE -eq 0 ]]; then
    if [[ $GPU_PROJECTS -eq 1 && -t 0 ]]; then
        echo "Backends to build (space-separated numbers) [1]:"
        echo "  1) vulkan   AMD / NVIDIA / Intel / mixed -- no vendor SDK (default)"
        echo "  2) rocm     AMD native (needs ROCm: Arch rocm-hip-sdk / Ubuntu amdgpu-install)"
        echo "  3) cuda     NVIDIA native (needs the CUDA toolkit)"
        echo "  4) sycl     Intel oneAPI"
        echo "  5) cpu      no GPU -- AVX2/AVX512/AMX via -march=native"
        read -r -p "> " backend_pick
        backend_pick="${backend_pick:-1}"
        for n in $backend_pick; do
            case "$n" in
                1) BACKENDS+=(vulkan) ;;
                2) BACKENDS+=(rocm) ;;
                3) BACKENDS+=(cuda) ;;
                4) BACKENDS+=(sycl) ;;
                5) BACKENDS+=(cpu) ;;
                *) echo "Unknown backend choice: $n" >&2; exit 1 ;;
            esac
        done
    else
        BACKENDS=(vulkan)
    fi
fi

# Build a flat list of (project, backend) jobs to run (update mode already
# built its own JOBS_LIST from the trees on disk)
if [[ $UPDATE -eq 0 ]]; then
JOBS_LIST=()
for p in "${PROJECTS[@]}"; do
    if [[ "$p" == "piper" || "$p" == "tools" ]]; then
        JOBS_LIST+=("$p")
        continue
    fi
    # vllm is ROCm-only -- there is no vllm vulkan/cuda/sycl/cpu job
    if [[ "$p" == "vllm" ]]; then
        if [[ " ${BACKENDS[*]} " == *" rocm "* ]]; then
            JOBS_LIST+=("vllm-rocm")
        else
            warn "vllm is ROCm-only -- skipping (no rocm backend selected)"
        fi
        continue
    fi
    # strata is ROCm/HIP-only -- upstream has no vulkan/sycl engine backend
    if [[ "$p" == "strata" ]]; then
        if [[ " ${BACKENDS[*]} " == *" rocm "* ]]; then
            JOBS_LIST+=("strata-rocm")
        else
            warn "strata is ROCm-only -- skipping (no rocm backend selected)"
        fi
        continue
    fi
    # radiance is ROCm-only as a JayNet target (RDNA4 HIP kernels; its AVX
    # host backend is an upstream fallback we don't install)
    if [[ "$p" == "radiance" ]]; then
        if [[ " ${BACKENDS[*]} " == *" rocm "* ]]; then
            JOBS_LIST+=("radiance-rocm")
        else
            warn "radiance is ROCm-only -- skipping (no rocm backend selected)"
        fi
        continue
    fi
    for b in "${BACKENDS[@]}"; do
        JOBS_LIST+=("${p}-${b}")
    done
done
fi

# -- GPU targets for the native backends -----------------------------------------
# Asked only when the backend is actually in the job list; env presets skip.
if [[ -z "$AMDGPU_TARGETS" && " ${JOBS_LIST[*]} " == *"-rocm "* ]]; then
    if [[ -t 0 ]]; then
        echo "AMD GPU target for ROCm builds [gfx1201]:"
        echo "  gfx1030 RX 6800-6950 XT   gfx1031 RX 6700/6750 XT   gfx1032 RX 6600 (XT)"
        echo "  gfx1100 RX 7900 XT/XTX    gfx1101 RX 7700/7800 XT   gfx1102 RX 7600"
        echo "  gfx1103 Radeon 780M APU   gfx1151 Strix Halo"
        echo "  gfx1200 RX 9060 XT        gfx1201 RX 9070 (XT) / AI PRO R9700"
        read -r -p "> " AMDGPU_TARGETS
    fi
    AMDGPU_TARGETS="${AMDGPU_TARGETS:-gfx1201}"
fi
if [[ -z "$CUDA_ARCHS" && " ${JOBS_LIST[*]} " == *"-cuda "* ]]; then
    if [[ -t 0 ]]; then
        echo "CUDA architectures [native = auto-detect]:"
        echo "  61 GTX 10xx/P40   75 RTX 20xx/T4   86 RTX 30xx"
        echo "  89 RTX 40xx/L4/L40   120 RTX 50xx"
        read -r -p "> " CUDA_ARCHS
    fi
    CUDA_ARCHS="${CUDA_ARCHS:-native}"
fi

# -- Pre-flight checks ------------------------------------------------------
check_clone() {
    local dir="$1"
    local url="$2"
    local recursive="${3:-}"
    if [[ ! -d "$dir/.git" ]]; then
        warn "$dir not found, cloning..."
        mkdir -p "$(dirname "$dir")"
        if [[ "$recursive" == "recursive" ]]; then
            git clone --recursive "$url" "$dir"
        else
            git clone "$url" "$dir"
        fi
    fi
}

sync_repo() {
    local dir="$1"
    local recursive="${2:-}"
    step "Sync ${dir}"
    cd "$dir"
    git fetch --all --tags --quiet
    git pull --ff-only
    if [[ "$recursive" == "recursive" ]]; then
        git submodule update --init --recursive --quiet
    fi
    ok "at $(git describe --tags --always)"
}

# -- llama.cpp SIGTERM-safe exit patch ----------------------------------------
# Upstream issue ggml-org/llama.cpp#29581 (still open): the "hit Ctrl+C twice"
# branch of signal_handler() calls exit() from inside the POSIX signal
# handler. exit() is not async-signal-safe: a second SIGTERM/SIGINT landing
# mid-teardown runs atexit handlers and static destructors on the interrupted
# stack -- observed as a deadlock in glibc free() (the issue's stack) and as
# std::terminate in ~stream_session_manager() -> stop_gc() -> thread::join()
# with a coredump (our stack, llama-server under systemd stop). This carries
# the maintainer's proposed one-liner verbatim (std::_Exit skips atexit
# handlers and static destructors), so the day upstream merges it, the
# reverse-check below reports "already applied" instead of failing.
apply_sigterm_patch() {
    local dir="$1"
    local patch="${BASE_DIR}/patches/llama-sigterm-safe-exit.patch"
    mkdir -p "${BASE_DIR}/patches"
    # The patch is ours, so it is regenerated from the heredoc every run --
    # no download, no sha256 needed, and edits here take effect immediately.
    cat > "$patch" <<'PATCH'
--- a/tools/server/server.cpp
+++ b/tools/server/server.cpp
@@ -14,6 +14,7 @@
 
 #include <atomic>
 #include <clocale>
+#include <cstdlib>
 #include <exception>
 #include <signal.h>
 #include <thread> // for std::thread::hardware_concurrency
@@ -30,7 +31,7 @@
         // in case it hangs, we can force terminate the server by hitting Ctrl+C twice
         // this is for better developer experience, we can remove when the server is stable enough
         fprintf(stderr, "Received second interrupt, terminating immediately.\n");
-        exit(1);
+        std::_Exit(1); // ggml-org/llama.cpp#29581: exit() is not async-signal-safe
     }
 
     shutdown_handler(signal);
PATCH
    if git -C "$dir" apply --check "$patch" 2>/dev/null; then
        git -C "$dir" apply "$patch" && ok "applied SIGTERM-safe exit patch"
    elif git -C "$dir" apply --reverse --check "$patch" 2>/dev/null; then
        ok "SIGTERM-safe exit patch already applied (or merged upstream)"
    elif grep -q '_Exit(1)\|_exit(1)' "$dir/tools/server/server.cpp" 2>/dev/null; then
        ok "signal handler fixed upstream -- local SIGTERM patch no longer needed"
    else
        fail "SIGTERM patch applies neither way -- upstream changed signal_handler, revisit tools/server/server.cpp and drop or rebase the patch (issue #29581)"
    fi
}

# Vanilla llama.cpp trees track upstream master but carry the SIGTERM patch
# above. The patch leaves the tree dirty, which would break sync_repo's
# ff-only pull, so revert the patched file to pristine first, then re-apply.
sync_llama_repo() {
    local dir="$1"
    check_clone "$dir" "https://github.com/ggml-org/llama.cpp"
    git -C "$dir" checkout -- tools/server/server.cpp 2>/dev/null || true
    sync_repo "$dir"
    apply_sigterm_patch "$dir"
}

# -- SD web UI frontend build + install -------------------------------------
# sd-server only ships a placeholder at "/". The actual web UI lives in
# examples/server/frontend as a Vite project and must be built separately.
# We build it from the freshly-synced SD source tree, then copy the dist/
# output to a shared install location so both backends use the same path
# via --serve-html-path ${SD_FRONTEND_INSTALL_DIR}/dist/index.html
build_sd_frontend() {
    local src_dir="$1/examples/server/frontend"
    local src_label="$(basename "$1")"

    if [[ ! -d "$src_dir" ]]; then
        warn "Frontend source not found at $src_dir, skipping web UI"
        return 0
    fi

    step "Build SD web UI frontend (from $src_label)"

    # Prefer pnpm (lockfile is pnpm-lock.yaml), fall back to npm
    local pm=""
    if command -v pnpm >/dev/null 2>&1; then
        pm="pnpm"
    elif command -v npm >/dev/null 2>&1; then
        warn "pnpm not found, falling back to npm (slower, dep tree may differ)"
        pm="npm"
    else
        warn "Neither pnpm nor npm found -- skipping web UI build"
        warn "  Install with: $(pkg_hint pnpm pnpm)  (or nodejs/npm via your distro)"
        return 0
    fi

    # Skip rebuild if dist/index.html in the source tree is newer than
    # package.json, vite.config.js, and everything under src/
    local need_build=1
    if [[ -f "$src_dir/dist/index.html" ]] \
       && [[ "$src_dir/dist/index.html" -nt "$src_dir/package.json" ]] \
       && [[ "$src_dir/dist/index.html" -nt "$src_dir/vite.config.js" ]]; then
        local newest_src
        # -print -quit: stop at the first newer file; a `| head -1` pipe can
        # SIGPIPE find mid-stream and kill the script under pipefail
        newest_src=$(find "$src_dir/src" -type f -newer "$src_dir/dist/index.html" -print -quit 2>/dev/null)
        if [[ -z "$newest_src" ]]; then
            ok "Web UI dist/ in source tree is up to date, skipping rebuild"
            need_build=0
        fi
    fi

    if [[ $need_build -eq 1 ]]; then
        pushd "$src_dir" >/dev/null
        if [[ "$pm" == "pnpm" ]]; then
            pnpm install --frozen-lockfile
            pnpm run build
        else
            npm install
            npm run build
        fi
        popd >/dev/null

        if [[ ! -f "$src_dir/dist/index.html" ]]; then
            warn "Web UI build finished but dist/index.html not found"
            return 0
        fi
    fi

    # Install (copy) dist/ to the shared canonical location. We copy rather
    # than symlink so an `--clean` on the source tree won't break the install.
    step "Install web UI -> ${SD_FRONTEND_INSTALL_DIR}"
    mkdir -p "$SD_FRONTEND_INSTALL_DIR"
    rm -rf "${SD_FRONTEND_INSTALL_DIR}/dist"
    cp -r "$src_dir/dist" "${SD_FRONTEND_INSTALL_DIR}/dist"
    ok "Web UI installed: ${SD_FRONTEND_HTML}"
}

# -- llama.cpp ROCm build ---------------------------------------------------
build_llama_rocm() {
    sync_llama_repo "$ROCM_DIR"
    cd "$ROCM_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean ROCm build dir"
        rm -rf build
    fi

    step "Configure ROCm (${AMDGPU_TARGETS})"
    # Note: hipcc lives in /opt/rocm on Arch via the `rocm-hip-sdk` package

    # rocWMMA enables HW-accelerated flash attention on RDNA, but the headers
    # are in a separate Arch package (`rocwmma`). Auto-detect.
    local rocwmma_flag="OFF"
    if [[ -f "/opt/rocm/include/rocwmma/rocwmma-version.hpp" ]] \
       || [[ -f "/usr/include/rocwmma/rocwmma-version.hpp" ]]; then
        rocwmma_flag="ON"
        ok "rocWMMA headers found -- enabling HW flash-attn"
    else
        warn "rocWMMA headers NOT found -- building without HW flash-attn"
        warn "  Install with: $(pkg_hint rocwmma rocwmma-dev)"
    fi

    # Web UI is intentionally disabled: we run llama-server headless behind
    # LiteLLM. Both flags are currently required -- LLAMA_BUILD_UI=OFF alone
    # still triggers asset provisioning (npm build / HF bucket download / stale
    # dist embed), which is exactly what was breaking the build after the
    # Svelte+PWA web UI rework landed. Drop both flags + install nodejs/npm
    # if you ever want the embedded browser UI back.
    # TESTS=OFF: we only build 3 targets; skipping tests keeps configure lean
    # (and their broken install rules out of the tree).
    cmake -B build \
        -DGGML_HIP=ON \
        -DAMDGPU_TARGETS="$AMDGPU_TARGETS" \
        -DGGML_HIP_ROCWMMA_FATTN="$rocwmma_flag" \
        -DLLAMA_BUILD_UI=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_WEBUI=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_COMPILER=/opt/rocm/lib/llvm/bin/clang \
        -DCMAKE_CXX_COMPILER=/opt/rocm/lib/llvm/bin/clang++ \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build ROCm (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target llama-server llama-cli llama-bench

    if [[ -x "$ROCM_DIR/build/bin/llama-server" ]]; then
        ok "ROCm build at $ROCM_DIR/build/bin/llama-server"
    else
        fail "ROCm build produced no llama-server binary"
    fi

    install_prefix "$ROCM_DIR" llama.cpp rocm llama-server
}

# -- llama.cpp Vulkan build -------------------------------------------------
build_llama_vulkan() {
    sync_llama_repo "$VULKAN_DIR"
    cd "$VULKAN_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean Vulkan build dir"
        rm -rf build
    fi

    step "Configure Vulkan"
    # Web UI disabled here too -- see note in build_llama_rocm().
    cmake -B build \
        -DGGML_VULKAN=ON \
        -DLLAMA_BUILD_UI=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_WEBUI=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build Vulkan (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target llama-server llama-cli llama-bench

    if [[ -x "$VULKAN_DIR/build/bin/llama-server" ]]; then
        ok "Vulkan build at $VULKAN_DIR/build/bin/llama-server"
    else
        fail "Vulkan build produced no llama-server binary"
    fi

    install_prefix "$VULKAN_DIR" llama.cpp vulkan llama-server
}

# -- Intel oneAPI environment (for SYCL builds) -------------------------------
# SYCL builds need icx/icpx plus the oneAPI libs. Source the standard
# setvars.sh if the compiler isn't already on PATH.
ensure_oneapi() {
    if ! command -v icpx >/dev/null 2>&1; then
        if [[ -f /opt/intel/oneapi/setvars.sh ]]; then
            step "Source Intel oneAPI environment"
            # setvars.sh trips on unbound vars under `set -u`
            set +u
            source /opt/intel/oneapi/setvars.sh >/dev/null
            set -u
        else
            fail "icpx not found -- install Intel oneAPI (AUR: intel-oneapi-compiler-dpcpp-cpp)"
        fi
    fi
}

# -- llama.cpp CUDA build (NVIDIA) --------------------------------------------
build_llama_cuda() {
    sync_llama_repo "$CUDA_DIR"
    cd "$CUDA_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean CUDA build dir"
        rm -rf build
    fi

    step "Configure CUDA (archs: ${CUDA_ARCHS})"
    # Web UI disabled here too -- see note in build_llama_rocm().
    cmake -B build \
        -DGGML_CUDA=ON \
        -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCHS" \
        -DLLAMA_BUILD_UI=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_WEBUI=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build CUDA (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target llama-server llama-cli llama-bench

    if [[ -x "$CUDA_DIR/build/bin/llama-server" ]]; then
        ok "CUDA build at $CUDA_DIR/build/bin/llama-server"
    else
        fail "CUDA build produced no llama-server binary"
    fi

    install_prefix "$CUDA_DIR" llama.cpp cuda llama-server
}

# -- llama.cpp SYCL build (Intel oneAPI) ----------------------------------------
build_llama_sycl() {
    sync_llama_repo "$SYCL_DIR"
    ensure_oneapi
    cd "$SYCL_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean SYCL build dir"
        rm -rf build
    fi

    step "Configure SYCL (Intel oneAPI)"
    # Web UI disabled here too -- see note in build_llama_rocm().
    # No -march flags here: keep the oneAPI defaults; add -fsycl-targets via
    # CMAKE_CXX_FLAGS if you need to target something other than the default.
    cmake -B build \
        -DGGML_SYCL=ON \
        -DLLAMA_BUILD_UI=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_WEBUI=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_COMPILER=icx \
        -DCMAKE_CXX_COMPILER=icpx

    step "Build SYCL (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target llama-server llama-cli llama-bench

    if [[ -x "$SYCL_DIR/build/bin/llama-server" ]]; then
        ok "SYCL build at $SYCL_DIR/build/bin/llama-server"
    else
        fail "SYCL build produced no llama-server binary"
    fi

    install_prefix "$SYCL_DIR" llama.cpp sycl llama-server
}

# -- whisper.cpp ROCm build ---------------------------------------------------
build_whisper_rocm() {
    check_clone "$WHISPER_ROCM_DIR" "https://github.com/ggml-org/whisper.cpp"
    sync_repo "$WHISPER_ROCM_DIR"
    cd "$WHISPER_ROCM_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean whisper ROCm build dir"
        rm -rf build
    fi

    step "Configure whisper.cpp ROCm (${AMDGPU_TARGETS})"
    # No ffmpeg: the orchestrator web UI sends 16 kHz mono WAV, which
    # whisper-server decodes natively. Add -DWHISPER_FFMPEG=ON if you ever
    # need to feed it arbitrary audio formats.
    cmake -B build \
        -DGGML_HIP=ON \
        -DAMDGPU_TARGETS="$AMDGPU_TARGETS" \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_COMPILER=/opt/rocm/lib/llvm/bin/clang \
        -DCMAKE_CXX_COMPILER=/opt/rocm/lib/llvm/bin/clang++ \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build whisper ROCm (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target whisper-server whisper-cli

    if [[ -x "$WHISPER_ROCM_DIR/build/bin/whisper-server" ]]; then
        ok "whisper ROCm build at $WHISPER_ROCM_DIR/build/bin/whisper-server"
    else
        fail "whisper ROCm build produced no whisper-server binary"
    fi

    install_prefix "$WHISPER_ROCM_DIR" whisper.cpp rocm whisper-server
}

# -- whisper.cpp Vulkan build -------------------------------------------------
build_whisper_vulkan() {
    check_clone "$WHISPER_VULKAN_DIR" "https://github.com/ggml-org/whisper.cpp"
    sync_repo "$WHISPER_VULKAN_DIR"
    cd "$WHISPER_VULKAN_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean whisper Vulkan build dir"
        rm -rf build
    fi

    step "Configure whisper.cpp Vulkan"
    cmake -B build \
        -DGGML_VULKAN=ON \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build whisper Vulkan (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target whisper-server whisper-cli

    if [[ -x "$WHISPER_VULKAN_DIR/build/bin/whisper-server" ]]; then
        ok "whisper Vulkan build at $WHISPER_VULKAN_DIR/build/bin/whisper-server"
    else
        fail "whisper Vulkan build produced no whisper-server binary"
    fi

    install_prefix "$WHISPER_VULKAN_DIR" whisper.cpp vulkan whisper-server
}

# -- whisper.cpp CUDA build (NVIDIA) --------------------------------------------
build_whisper_cuda() {
    check_clone "$WHISPER_CUDA_DIR" "https://github.com/ggml-org/whisper.cpp"
    sync_repo "$WHISPER_CUDA_DIR"
    cd "$WHISPER_CUDA_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean whisper CUDA build dir"
        rm -rf build
    fi

    step "Configure whisper.cpp CUDA (archs: ${CUDA_ARCHS})"
    # No ffmpeg -- see note in build_whisper_rocm().
    cmake -B build \
        -DGGML_CUDA=ON \
        -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCHS" \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build whisper CUDA (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target whisper-server whisper-cli

    if [[ -x "$WHISPER_CUDA_DIR/build/bin/whisper-server" ]]; then
        ok "whisper CUDA build at $WHISPER_CUDA_DIR/build/bin/whisper-server"
    else
        fail "whisper CUDA build produced no whisper-server binary"
    fi

    install_prefix "$WHISPER_CUDA_DIR" whisper.cpp cuda whisper-server
}

# -- whisper.cpp SYCL build (Intel oneAPI) ---------------------------------------
build_whisper_sycl() {
    check_clone "$WHISPER_SYCL_DIR" "https://github.com/ggml-org/whisper.cpp"
    sync_repo "$WHISPER_SYCL_DIR"
    ensure_oneapi
    cd "$WHISPER_SYCL_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean whisper SYCL build dir"
        rm -rf build
    fi

    step "Configure whisper.cpp SYCL (Intel oneAPI)"
    # No ffmpeg -- see note in build_whisper_rocm().
    cmake -B build \
        -DGGML_SYCL=ON \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_COMPILER=icx \
        -DCMAKE_CXX_COMPILER=icpx

    step "Build whisper SYCL (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target whisper-server whisper-cli

    if [[ -x "$WHISPER_SYCL_DIR/build/bin/whisper-server" ]]; then
        ok "whisper SYCL build at $WHISPER_SYCL_DIR/build/bin/whisper-server"
    else
        fail "whisper SYCL build produced no whisper-server binary"
    fi

    install_prefix "$WHISPER_SYCL_DIR" whisper.cpp sycl whisper-server
}

# -- piper TTS install (venv, CPU only) ---------------------------------------
build_piper() {
    step "Install piper TTS -> ${PIPER_VENV}"

    command -v python3 >/dev/null 2>&1 || fail "python3 not found"
    command -v curl    >/dev/null 2>&1 || fail "curl not found (needed for voice download)"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean piper venv"
        rm -rf "$PIPER_VENV"
    fi

    # A venv moved with the checkout keeps a dead pip shebang (bin/python
    # still resolves via the system interpreter) — probe pip itself and
    # recreate when broken, not just when missing
    if ! "$PIPER_VENV/bin/pip" --version >/dev/null 2>&1; then
        rm -rf "$PIPER_VENV"
        python3 -m venv "$PIPER_VENV"
    fi
    "$PIPER_VENV/bin/pip" install --quiet --upgrade pip
    "$PIPER_VENV/bin/pip" install --quiet --upgrade piper-tts

    # Default voice model (onnx + config); skip if already present
    mkdir -p "$PIPER_VOICE_DIR"
    local onnx="${PIPER_VOICE_DIR}/${PIPER_VOICE}.onnx"
    local json="${PIPER_VOICE_DIR}/${PIPER_VOICE}.onnx.json"
    if [[ ! -f "$onnx" ]]; then
        step "Download voice ${PIPER_VOICE}"
        curl -fL --progress-bar -o "$onnx" "${PIPER_VOICE_BASE}/${PIPER_VOICE}.onnx"
        curl -fL --progress-bar -o "$json" "${PIPER_VOICE_BASE}/${PIPER_VOICE}.onnx.json"
    else
        ok "Voice ${PIPER_VOICE} already present"
    fi

    if [[ -x "$PIPER_VENV/bin/piper" ]]; then
        ok "piper at $PIPER_VENV/bin/piper (voice: $onnx)"
    else
        fail "piper install produced no piper binary in $PIPER_VENV/bin"
    fi

    install_link "$PIPER_VENV/bin/piper"
}

# -- llm-tools venv (hf download tooling) -------------------------------------
build_llmtools() {
    step "Install llm-tools venv -> ${TOOLS_VENV}"

    command -v python3 >/dev/null 2>&1 || fail "python3 not found"
    [[ -f "$TOOLS_REQUIREMENTS" ]] || fail "requirements not found: $TOOLS_REQUIREMENTS"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean llm-tools venv"
        rm -rf "$TOOLS_VENV"
    fi

    # A venv moved with the checkout keeps a dead pip shebang (bin/python
    # still resolves via the system interpreter) — probe pip itself and
    # recreate when broken, not just when missing
    if ! "$TOOLS_VENV/bin/pip" --version >/dev/null 2>&1; then
        rm -rf "$TOOLS_VENV"
        python3 -m venv "$TOOLS_VENV"
    fi
    "$TOOLS_VENV/bin/pip" install --quiet --upgrade pip
    "$TOOLS_VENV/bin/pip" install --quiet --upgrade -r "$TOOLS_REQUIREMENTS"

    if [[ -x "$TOOLS_VENV/bin/hf" ]]; then
        ok "hf CLI at $TOOLS_VENV/bin/hf"
    else
        fail "llm-tools install produced no hf binary in $TOOLS_VENV/bin"
    fi

    install_link "$TOOLS_VENV/bin/hf"
}

# -- stable-diffusion.cpp ROCm build ----------------------------------------
build_sd_rocm() {
    check_clone "$SD_ROCM_DIR" "https://github.com/leejet/stable-diffusion.cpp" recursive
    sync_repo "$SD_ROCM_DIR" recursive
    cd "$SD_ROCM_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean SD ROCm build dir"
        rm -rf build
    fi

    step "Configure stable-diffusion.cpp ROCm (${AMDGPU_TARGETS})"
    # Note: SD uses -DSD_HIPBLAS=ON (not GGML_HIP) and wants both GPU_TARGETS
    # and AMDGPU_TARGETS depending on ROCm version. Ninja is recommended.
    # rocWMMA is kept OFF for SD -- per AUR notes it regressed on ROCm 7+.
    cmake -B build \
        -G Ninja \
        -DSD_HIPBLAS=ON \
        -DGPU_TARGETS="$AMDGPU_TARGETS" \
        -DAMDGPU_TARGETS="$AMDGPU_TARGETS" \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_COMPILER=/opt/rocm/lib/llvm/bin/clang \
        -DCMAKE_CXX_COMPILER=/opt/rocm/lib/llvm/bin/clang++ \
        -DCMAKE_HIP_COMPILER=/opt/rocm/lib/llvm/bin/clang++ \
        -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON \
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON

    step "Build SD ROCm (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS"

    if [[ -x "$SD_ROCM_DIR/build/bin/sd" ]] \
       || [[ -x "$SD_ROCM_DIR/build/bin/sd-cli" ]]; then
        ok "SD ROCm build at $SD_ROCM_DIR/build/bin/"
    else
        fail "SD ROCm build produced no sd/sd-cli binary"
    fi

    install_bins "$SD_ROCM_DIR/build/bin" sd sd-cli sd-server

    # Build + install web UI from this freshly-synced source tree.
    # Both backends do this; whichever runs second just refreshes the install.
    build_sd_frontend "$SD_ROCM_DIR"
}

# -- stable-diffusion.cpp Vulkan build --------------------------------------
build_sd_vulkan() {
    check_clone "$SD_VULKAN_DIR" "https://github.com/leejet/stable-diffusion.cpp" recursive
    sync_repo "$SD_VULKAN_DIR" recursive
    cd "$SD_VULKAN_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean SD Vulkan build dir"
        rm -rf build
    fi

    step "Configure stable-diffusion.cpp Vulkan"
    cmake -B build \
        -DSD_VULKAN=ON \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build SD Vulkan (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS"

    if [[ -x "$SD_VULKAN_DIR/build/bin/sd" ]] \
       || [[ -x "$SD_VULKAN_DIR/build/bin/sd-cli" ]]; then
        ok "SD Vulkan build at $SD_VULKAN_DIR/build/bin/"
    else
        fail "SD Vulkan build produced no sd/sd-cli binary"
    fi

    install_bins "$SD_VULKAN_DIR/build/bin" sd sd-cli sd-server

    # Build + install web UI from this freshly-synced source tree.
    # Both backends do this; whichever runs second just refreshes the install.
    build_sd_frontend "$SD_VULKAN_DIR"
}

# -- stable-diffusion.cpp CUDA build (NVIDIA) -----------------------------------
build_sd_cuda() {
    check_clone "$SD_CUDA_DIR" "https://github.com/leejet/stable-diffusion.cpp" recursive
    sync_repo "$SD_CUDA_DIR" recursive
    cd "$SD_CUDA_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean SD CUDA build dir"
        rm -rf build
    fi

    step "Configure stable-diffusion.cpp CUDA (archs: ${CUDA_ARCHS})"
    cmake -B build \
        -DSD_CUDA=ON \
        -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCHS" \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build SD CUDA (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS"

    if [[ -x "$SD_CUDA_DIR/build/bin/sd" ]] \
       || [[ -x "$SD_CUDA_DIR/build/bin/sd-cli" ]]; then
        ok "SD CUDA build at $SD_CUDA_DIR/build/bin/"
    else
        fail "SD CUDA build produced no sd/sd-cli binary"
    fi

    install_bins "$SD_CUDA_DIR/build/bin" sd sd-cli sd-server

    build_sd_frontend "$SD_CUDA_DIR"
}

# -- stable-diffusion.cpp SYCL build (Intel oneAPI) ------------------------------
build_sd_sycl() {
    check_clone "$SD_SYCL_DIR" "https://github.com/leejet/stable-diffusion.cpp" recursive
    sync_repo "$SD_SYCL_DIR" recursive
    ensure_oneapi
    cd "$SD_SYCL_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean SD SYCL build dir"
        rm -rf build
    fi

    step "Configure stable-diffusion.cpp SYCL (Intel oneAPI)"
    cmake -B build \
        -DSD_SYCL=ON \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_COMPILER=icx \
        -DCMAKE_CXX_COMPILER=icpx

    step "Build SD SYCL (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS"

    if [[ -x "$SD_SYCL_DIR/build/bin/sd" ]] \
       || [[ -x "$SD_SYCL_DIR/build/bin/sd-cli" ]]; then
        ok "SD SYCL build at $SD_SYCL_DIR/build/bin/"
    else
        fail "SD SYCL build produced no sd/sd-cli binary"
    fi

    install_bins "$SD_SYCL_DIR/build/bin" sd sd-cli sd-server

    build_sd_frontend "$SD_SYCL_DIR"
}

# -- CPU-only builds (no GPU backend) ------------------------------------------
# No GGML_HIP/VULKAN/CUDA/SYCL flags: llama.cpp/whisper.cpp/sd.cpp build their
# CPU backend with GGML_NATIVE=ON by default, and -march=$MARCH (default
# "native") unlocks the host's best SIMD paths (AVX2/AVX512, AMX on Xeon).
# Runtime threading is a llama-server flag (-t), not a build flag.
build_llama_cpu() {
    sync_llama_repo "$CPU_DIR"
    cd "$CPU_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean CPU build dir"
        rm -rf build
    fi

    step "Configure CPU (-march=$MARCH)"
    cmake -B build \
        -DLLAMA_BUILD_UI=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_WEBUI=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build CPU (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target llama-server llama-cli llama-bench

    if [[ -x "$CPU_DIR/build/bin/llama-server" ]]; then
        ok "CPU build at $CPU_DIR/build/bin/llama-server"
    else
        fail "CPU build produced no llama-server binary"
    fi

    install_prefix "$CPU_DIR" llama.cpp cpu llama-server
}

build_whisper_cpu() {
    check_clone "$WHISPER_CPU_DIR" "https://github.com/ggml-org/whisper.cpp"
    sync_repo "$WHISPER_CPU_DIR"
    cd "$WHISPER_CPU_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean whisper CPU build dir"
        rm -rf build
    fi

    step "Configure whisper.cpp CPU (-march=$MARCH)"
    cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build whisper CPU (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target whisper-server whisper-cli

    if [[ -x "$WHISPER_CPU_DIR/build/bin/whisper-server" ]]; then
        ok "whisper CPU build at $WHISPER_CPU_DIR/build/bin/whisper-server"
    else
        fail "whisper CPU build produced no whisper-server binary"
    fi

    install_prefix "$WHISPER_CPU_DIR" whisper.cpp cpu whisper-server
}

build_sd_cpu() {
    check_clone "$SD_CPU_DIR" "https://github.com/leejet/stable-diffusion.cpp" recursive
    sync_repo "$SD_CPU_DIR" recursive
    cd "$SD_CPU_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean SD CPU build dir"
        rm -rf build
    fi

    # Works, but expect minutes per image -- CPU SD is a fallback, not a plan.
    step "Configure stable-diffusion.cpp CPU (-march=$MARCH)"
    cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build SD CPU (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS"

    if [[ -x "$SD_CPU_DIR/build/bin/sd" ]] \
       || [[ -x "$SD_CPU_DIR/build/bin/sd-cli" ]]; then
        ok "SD CPU build at $SD_CPU_DIR/build/bin/"
    else
        fail "SD CPU build produced no sd/sd-cli binary"
    fi

    install_bins "$SD_CPU_DIR/build/bin" sd sd-cli sd-server

    build_sd_frontend "$SD_CPU_DIR"
}

# -- K2-Horizon fork helpers --------------------------------------------------
# Generic pin: clone if missing, then hold the tree at one reviewed commit.
# Unlike sync_repo this never pulls: a fork we build and run inside JayNet
# only moves when a human reviews the new commits and bumps the *_COMMIT var.
# A clean tree NOT at the pin is checked out; a dirty tree not at the pin is
# an unknown state and aborts. `recursive` also syncs submodules (omnivoice
# ships ggml as one).
pin_repo() {
    local dir="$1" repo="$2" commit="$3" recursive="${4:-}"
    if [[ ! -d "$dir/.git" ]]; then
        warn "$dir not found, cloning..."
        mkdir -p "$(dirname "$dir")"
        if [[ "$recursive" == "recursive" ]]; then
            git clone --recursive "$repo" "$dir"
        else
            git clone "$repo" "$dir"
        fi
    fi
    cd "$dir"
    if [[ "$(git rev-parse HEAD)" != "$commit" ]]; then
        if git diff --quiet && git diff --cached --quiet; then
            step "Pin $(basename "$dir") at ${commit:0:12}"
            git fetch --quiet origin
            git checkout --quiet "$commit"
        else
            fail "$dir is not at the pinned commit and has local changes -- resolve manually"
        fi
    fi
    if [[ "$recursive" == "recursive" ]]; then
        git submodule update --init --recursive --quiet
    fi
    ok "$(basename "$dir") pinned at $(git rev-parse --short HEAD)"
}

# K2-Horizon: pinned fork + two model-card patches on top. The patches leave
# the tree dirty by design -- pin_repo's clean-tree checkout only ever runs
# before the first patch application on a fresh clone.
sync_k2_repo() {
    pin_repo "$1" "$K2_REPO" "$K2_COMMIT"
}

# Download the two model-card patches once into patches/k2horizon/ and apply
# them idempotently (skip when already applied, fail when they match neither
# way -- that means the fork moved and the pin/patches need a revisit).
# Every patch is sha256-checked against K2_PATCH_SHA256 before use — the
# download URL is pinned to a fixed HF revision, and the checksum catches
# both a moved file and a tampered local copy.
apply_k2_patches() {
    local dir="$1"
    mkdir -p "$K2_PATCH_DIR"
    local f p
    for f in "${K2_PATCHES[@]}"; do
        p="$K2_PATCH_DIR/$f"
        if [[ ! -s "$p" ]]; then
            step "Download K2 patch $f"
            curl -fL --progress-bar -o "$p" "$K2_PATCH_BASE/$f" \
                || fail "patch download failed: $f"
        fi
        echo "${K2_PATCH_SHA256[$f]}  $p" | sha256sum --check --quiet \
            || fail "patch $f fails its sha256 check -- delete $p and re-run to re-download"
        if git -C "$dir" apply --check "$p" 2>/dev/null; then
            git -C "$dir" apply "$p" && ok "applied $f"
        elif git -C "$dir" apply --reverse --check "$p" 2>/dev/null; then
            ok "$f already applied"
        else
            fail "patch $f applies neither forward nor reverse -- fork moved? Check K2_COMMIT."
        fi
    done
}

# -- llama.cpp K2-Horizon builds (patched fork; rocm/vulkan/cuda/cpu) ---------
# Same flags as the matching vanilla builds; unknown flags on the (older)
# fork only trigger cmake warnings, not errors. Installs as its own family
# (llama.cpp-k2horizon.<backend>) so it never clobbers the vanilla prefixes.
build_k2horizon_rocm() {
    sync_k2_repo "$K2_ROCM_DIR"
    apply_k2_patches "$K2_ROCM_DIR"
    cd "$K2_ROCM_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean K2 ROCm build dir"
        rm -rf build
    fi

    local rocwmma_flag="OFF"
    if [[ -f "/opt/rocm/include/rocwmma/rocwmma-version.hpp" ]] \
       || [[ -f "/usr/include/rocwmma/rocwmma-version.hpp" ]]; then
        rocwmma_flag="ON"
    fi

    step "Configure K2-Horizon ROCm (${AMDGPU_TARGETS})"
    cmake -B build \
        -DGGML_HIP=ON \
        -DAMDGPU_TARGETS="$AMDGPU_TARGETS" \
        -DGGML_HIP_ROCWMMA_FATTN="$rocwmma_flag" \
        -DLLAMA_BUILD_UI=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_WEBUI=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_COMPILER=/opt/rocm/lib/llvm/bin/clang \
        -DCMAKE_CXX_COMPILER=/opt/rocm/lib/llvm/bin/clang++ \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build K2-Horizon ROCm (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target llama-server llama-cli llama-bench

    [[ -x "$K2_ROCM_DIR/build/bin/llama-server" ]] \
        || fail "K2 ROCm build produced no llama-server binary"
    ok "K2 ROCm build at $K2_ROCM_DIR/build/bin/llama-server"

    install_prefix "$K2_ROCM_DIR" llama.cpp-k2horizon rocm llama-server
}

build_k2horizon_vulkan() {
    sync_k2_repo "$K2_VULKAN_DIR"
    apply_k2_patches "$K2_VULKAN_DIR"
    cd "$K2_VULKAN_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean K2 Vulkan build dir"
        rm -rf build
    fi

    step "Configure K2-Horizon Vulkan"
    cmake -B build \
        -DGGML_VULKAN=ON \
        -DLLAMA_BUILD_UI=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_WEBUI=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build K2-Horizon Vulkan (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target llama-server llama-cli llama-bench

    [[ -x "$K2_VULKAN_DIR/build/bin/llama-server" ]] \
        || fail "K2 Vulkan build produced no llama-server binary"
    ok "K2 Vulkan build at $K2_VULKAN_DIR/build/bin/llama-server"

    install_prefix "$K2_VULKAN_DIR" llama.cpp-k2horizon vulkan llama-server
}

build_k2horizon_cuda() {
    sync_k2_repo "$K2_CUDA_DIR"
    apply_k2_patches "$K2_CUDA_DIR"
    cd "$K2_CUDA_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean K2 CUDA build dir"
        rm -rf build
    fi

    step "Configure K2-Horizon CUDA (archs: ${CUDA_ARCHS})"
    cmake -B build \
        -DGGML_CUDA=ON \
        -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCHS" \
        -DLLAMA_BUILD_UI=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_WEBUI=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build K2-Horizon CUDA (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target llama-server llama-cli llama-bench

    [[ -x "$K2_CUDA_DIR/build/bin/llama-server" ]] \
        || fail "K2 CUDA build produced no llama-server binary"
    ok "K2 CUDA build at $K2_CUDA_DIR/build/bin/llama-server"

    install_prefix "$K2_CUDA_DIR" llama.cpp-k2horizon cuda llama-server
}

build_k2horizon_cpu() {
    sync_k2_repo "$K2_CPU_DIR"
    apply_k2_patches "$K2_CPU_DIR"
    cd "$K2_CPU_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean K2 CPU build dir"
        rm -rf build
    fi

    step "Configure K2-Horizon CPU (-march=$MARCH)"
    cmake -B build \
        -DLLAMA_BUILD_UI=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_WEBUI=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build K2-Horizon CPU (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target llama-server llama-cli llama-bench

    [[ -x "$K2_CPU_DIR/build/bin/llama-server" ]] \
        || fail "K2 CPU build produced no llama-server binary"
    ok "K2 CPU build at $K2_CPU_DIR/build/bin/llama-server"

    install_prefix "$K2_CPU_DIR" llama.cpp-k2horizon cpu llama-server
}

# -- Spark-X2.5 fork helpers ----------------------------------------------------
# Pinned at SPARK_COMMIT (reviewed commit; see the config block for how to
# bump). No patches — unlike K2 there is nothing to apply on top.
sync_spark_repo() {
    pin_repo "$1" "$SPARK_REPO" "$SPARK_COMMIT"
}

build_spark_rocm() {
    sync_spark_repo "$SPARK_ROCM_DIR"
    cd "$SPARK_ROCM_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean Spark ROCm build dir"
        rm -rf build
    fi

    local rocwmma_flag="OFF"
    if [[ -f "/opt/rocm/include/rocwmma/rocwmma-version.hpp" ]] \
       || [[ -f "/usr/include/rocwmma/rocwmma-version.hpp" ]]; then
        rocwmma_flag="ON"
    fi

    step "Configure Spark-X2.5 ROCm (${AMDGPU_TARGETS})"
    cmake -B build \
        -DGGML_HIP=ON \
        -DAMDGPU_TARGETS="$AMDGPU_TARGETS" \
        -DGGML_HIP_ROCWMMA_FATTN="$rocwmma_flag" \
        -DLLAMA_BUILD_UI=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_WEBUI=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_COMPILER=/opt/rocm/lib/llvm/bin/clang \
        -DCMAKE_CXX_COMPILER=/opt/rocm/lib/llvm/bin/clang++ \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build Spark-X2.5 ROCm (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target llama-server llama-cli llama-bench

    [[ -x "$SPARK_ROCM_DIR/build/bin/llama-server" ]] \
        || fail "Spark ROCm build produced no llama-server binary"
    ok "Spark ROCm build at $SPARK_ROCM_DIR/build/bin/llama-server"

    install_prefix "$SPARK_ROCM_DIR" llama.cpp-spark rocm llama-server
}

build_spark_cpu() {
    sync_spark_repo "$SPARK_CPU_DIR"
    cd "$SPARK_CPU_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean Spark CPU build dir"
        rm -rf build
    fi

    step "Configure Spark-X2.5 CPU (-march=$MARCH)"
    cmake -B build \
        -DLLAMA_BUILD_UI=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_WEBUI=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build Spark-X2.5 CPU (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target llama-server llama-cli llama-bench

    [[ -x "$SPARK_CPU_DIR/build/bin/llama-server" ]] \
        || fail "Spark CPU build produced no llama-server binary"
    ok "Spark CPU build at $SPARK_CPU_DIR/build/bin/llama-server"

    install_prefix "$SPARK_CPU_DIR" llama.cpp-spark cpu llama-server
}

# -- llama.cpp PrismML/Bonsai fork builds (prism branch; rocm/cpu) -----------
# The ternary kernels live on the prism branch, pinned at BONSAI_COMMIT (a
# commit on that branch — clone fetches all branches, so the checkout works
# regardless of the default branch).
sync_bonsai_repo() {
    pin_repo "$1" "$BONSAI_REPO" "$BONSAI_COMMIT"
}

build_bonsai_rocm() {
    sync_bonsai_repo "$BONSAI_ROCM_DIR"
    cd "$BONSAI_ROCM_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean Bonsai ROCm build dir"
        rm -rf build
    fi

    local rocwmma_flag="OFF"
    if [[ -f "/opt/rocm/include/rocwmma/rocwmma-version.hpp" ]] \
       || [[ -f "/usr/include/rocwmma/rocwmma-version.hpp" ]]; then
        rocwmma_flag="ON"
    fi

    step "Configure Bonsai/PrismML ROCm (${AMDGPU_TARGETS})"
    cmake -B build \
        -DGGML_HIP=ON \
        -DAMDGPU_TARGETS="$AMDGPU_TARGETS" \
        -DGGML_HIP_ROCWMMA_FATTN="$rocwmma_flag" \
        -DLLAMA_BUILD_UI=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_WEBUI=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_COMPILER=/opt/rocm/lib/llvm/bin/clang \
        -DCMAKE_CXX_COMPILER=/opt/rocm/lib/llvm/bin/clang++ \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build Bonsai/PrismML ROCm (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target llama-server llama-cli llama-bench

    [[ -x "$BONSAI_ROCM_DIR/build/bin/llama-server" ]] \
        || fail "Bonsai ROCm build produced no llama-server binary"
    ok "Bonsai ROCm build at $BONSAI_ROCM_DIR/build/bin/llama-server"

    install_prefix "$BONSAI_ROCM_DIR" llama.cpp-bonsai rocm llama-server
}

build_bonsai_cpu() {
    sync_bonsai_repo "$BONSAI_CPU_DIR"
    cd "$BONSAI_CPU_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean Bonsai CPU build dir"
        rm -rf build
    fi

    step "Configure Bonsai/PrismML CPU (-march=$MARCH)"
    cmake -B build \
        -DLLAMA_BUILD_UI=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_WEBUI=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build Bonsai/PrismML CPU (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" --target llama-server llama-cli llama-bench

    [[ -x "$BONSAI_CPU_DIR/build/bin/llama-server" ]] \
        || fail "Bonsai CPU build produced no llama-server binary"
    ok "Bonsai CPU build at $BONSAI_CPU_DIR/build/bin/llama-server"

    install_prefix "$BONSAI_CPU_DIR" llama.cpp-bonsai cpu llama-server
}

# -- omnivoice.cpp (OmniVoice TTS) ------------------------------------------
# Binaries AND shared libs land in build/ ROOT (CMake sets the output dirs to
# CMAKE_BINARY_DIR), so install_prefix doesn't apply — same versioned-prefix
# pattern by hand. Args: <source tree> <backend>
install_omni_prefix() {
    local src_dir="$1" backend="$2"
    local tag dest link f
    tag="$(git -C "$src_dir" describe --tags --always 2>/dev/null || echo unknown)"
    dest="$BIN_DIR/omnivoice.cpp.${backend}-${tag}"
    link="$BIN_DIR/omnivoice.cpp.${backend}"
    step "Install omnivoice (${backend}) -> ${dest}"
    rm -rf "$dest"
    mkdir -p "$dest/bin" "$dest/lib"
    for f in "$src_dir"/build/*.so*; do
        [[ -e "$f" ]] && cp -a "$f" "$dest/lib/"
    done
    for f in omnivoice-tts omnivoice-codec tts-server; do
        [[ -x "$src_dir/build/$f" ]] || fail "omnivoice build produced no $f"
        cp "$src_dir/build/$f" "$dest/bin/$f"
    done
    ln -sfn "$dest" "$link"
    ok "Installed omnivoice ${backend}: $link/bin/tts-server"
}

build_omnivoice_rocm() {
    pin_repo "$OMNI_ROCM_DIR" "$OMNI_REPO" "$OMNI_COMMIT" recursive
    cd "$OMNI_ROCM_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean omnivoice ROCm build dir"
        rm -rf build
    fi

    step "Configure omnivoice.cpp ROCm (${AMDGPU_TARGETS})"
    cmake -B build \
        -DGGML_HIP=ON \
        -DAMDGPU_TARGETS="$AMDGPU_TARGETS" \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_COMPILER=/opt/rocm/lib/llvm/bin/clang \
        -DCMAKE_CXX_COMPILER=/opt/rocm/lib/llvm/bin/clang++ \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build omnivoice ROCm (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" \
        --target tts-server omnivoice-tts omnivoice-codec

    [[ -x "$OMNI_ROCM_DIR/build/tts-server" ]] \
        || fail "omnivoice ROCm build produced no tts-server binary"

    install_omni_prefix "$OMNI_ROCM_DIR" rocm
}

build_omnivoice_vulkan() {
    pin_repo "$OMNI_VULKAN_DIR" "$OMNI_REPO" "$OMNI_COMMIT" recursive
    cd "$OMNI_VULKAN_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean omnivoice Vulkan build dir"
        rm -rf build
    fi

    step "Configure omnivoice.cpp Vulkan"
    cmake -B build \
        -DGGML_VULKAN=ON \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build omnivoice Vulkan (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" \
        --target tts-server omnivoice-tts omnivoice-codec

    [[ -x "$OMNI_VULKAN_DIR/build/tts-server" ]] \
        || fail "omnivoice Vulkan build produced no tts-server binary"

    install_omni_prefix "$OMNI_VULKAN_DIR" vulkan
}

build_omnivoice_cuda() {
    pin_repo "$OMNI_CUDA_DIR" "$OMNI_REPO" "$OMNI_COMMIT" recursive
    cd "$OMNI_CUDA_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean omnivoice CUDA build dir"
        rm -rf build
    fi

    step "Configure omnivoice.cpp CUDA (${CUDA_ARCHS})"
    cmake -B build \
        -DGGML_CUDA=ON \
        -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCHS" \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build omnivoice CUDA (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" \
        --target tts-server omnivoice-tts omnivoice-codec

    [[ -x "$OMNI_CUDA_DIR/build/tts-server" ]] \
        || fail "omnivoice CUDA build produced no tts-server binary"

    install_omni_prefix "$OMNI_CUDA_DIR" cuda
}

build_omnivoice_cpu() {
    pin_repo "$OMNI_CPU_DIR" "$OMNI_REPO" "$OMNI_COMMIT" recursive
    cd "$OMNI_CPU_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean omnivoice CPU build dir"
        rm -rf build
    fi

    step "Configure omnivoice.cpp CPU (-march=$MARCH)"
    cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        "${ORIGIN_RPATH[@]}" \
        -DCMAKE_C_FLAGS="-march=$MARCH -O3" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH -O3"

    step "Build omnivoice CPU (-j${JOBS})"
    cmake --build build --config Release -j"$JOBS" \
        --target tts-server omnivoice-tts omnivoice-codec

    [[ -x "$OMNI_CPU_DIR/build/tts-server" ]] \
        || fail "omnivoice CPU build produced no tts-server binary"

    install_omni_prefix "$OMNI_CPU_DIR" cpu
}

# -- vLLM ROCm build (from source, python3.12 venv) ---------------------------
# The venv is not a bin/lib prefix, so the "install" is a wrapper that pins
# the runtime env, plus the piper-style stable symlink ($BIN_DIR/vllm).
install_vllm_wrapper() {
    local pytorch_arch="$1"
    local dir="$BIN_DIR/vllm-rocm"
    step "Install vllm wrapper -> ${dir}/vllm"
    mkdir -p "$dir"
    cat > "$dir/vllm" <<EOF
#!/bin/sh
# vLLM (ROCm source build) launcher -- see ENVIRONMENT.md next to this file
export PYTORCH_ROCM_ARCH="\${PYTORCH_ROCM_ARCH:-${pytorch_arch}}"
export ROCBLAS_USE_HIPBLASLT="\${ROCBLAS_USE_HIPBLASLT:-1}"
exec "${VLLM_VENV}/bin/vllm" "\$@"
EOF
    chmod +x "$dir/vllm"
    cat > "$dir/ENVIRONMENT.md" <<EOF
# vLLM ROCm runtime environment

Source tree: ${VLLM_SRC_DIR} (vanilla vllm-project/vllm, synced by
\`build_tools.sh update\`). Venv: ${VLLM_VENV}. Launch via the \`vllm\`
wrapper in this directory (symlinked as ${BIN_DIR}/vllm).

Env vars worth knowing:

- \`PYTORCH_ROCM_ARCH\` — gfx target(s) the HIP kernels were built for
  (the wrapper defaults to ${pytorch_arch}).
- \`ROCBLAS_USE_HIPBLASLT=1\` — hipBLASLt GEMMs, measurably faster on
  gfx12xx; the wrapper sets it by default.
- \`HIP_VISIBLE_DEVICES\` — pick GPUs by HIP index (the ROCm analogue of
  CUDA_VISIBLE_DEVICES; use this one on multi-GPU ROCm boxes).
- \`VLLM_ROCM_USE_AITER\` — ineffective on gfx1201 (no AITER kernels for
  RDNA4); leave unset.
- \`VLLM_SERVER_DEV_MODE=1\` + \`--enable-sleep-mode\` — enables the
  /sleep and /wake_up endpoints (offload weights to free VRAM between
  requests).
EOF
    install_link "$dir/vllm"
}

build_vllm_rocm() {
    step "Pre-flight (vllm ROCm)"
    # torch ROCm wheels target <= python 3.13 -- the venv MUST be 3.12, not
    # the distro's newer system python
    command -v python3.12 >/dev/null 2>&1 \
        || fail "python3.12 not found -- $(pkg_hint python312 python3.12)"
    command -v uv     >/dev/null 2>&1 \
        || fail "uv not found -- $(pkg_hint uv uv)  (or: curl -LsSf https://astral.sh/uv/install.sh | sh)"
    command -v cmake  >/dev/null 2>&1 || fail "cmake not found"
    command -v ninja  >/dev/null 2>&1 \
        || fail "ninja not found (vllm HIP build) -- $(pkg_hint ninja ninja-build)"
    command -v protoc >/dev/null 2>&1 \
        || fail "protoc not found (vllm build) -- $(pkg_hint protobuf protobuf)"

    # vLLM's frontend is Rust (setuptools-rust) -- no cargo, no build. rustup
    # is the supported non-interactive install; a previous rustup in $HOME is
    # picked up via its env file.
    [[ -f "$HOME/.cargo/env" ]] && source "$HOME/.cargo/env"
    if ! command -v cargo >/dev/null 2>&1; then
        command -v curl >/dev/null 2>&1 \
            || fail "cargo not found and curl missing -- install rust manually: https://rustup.rs"
        step "Install rustup (vllm has a Rust frontend)"
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \
            || fail "rustup install failed -- install rust manually: https://rustup.rs"
        source "$HOME/.cargo/env"
    fi
    command -v cargo >/dev/null 2>&1 \
        || fail "cargo still not found after rustup install"
    ok "cargo present: $(cargo --version 2>/dev/null)"

    check_clone "$VLLM_SRC_DIR" "https://github.com/vllm-project/vllm"
    sync_repo "$VLLM_SRC_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean vllm venv + build dir"
        rm -rf "$VLLM_VENV" "$VLLM_SRC_DIR/build"
    fi

    # Same moved-venv probe as piper/llm-tools: a stale pip shebang means
    # recreate, not just "exists"
    if ! "$VLLM_VENV/bin/pip" --version >/dev/null 2>&1; then
        step "Create python3.12 venv -> ${VLLM_VENV}"
        rm -rf "$VLLM_VENV"
        python3.12 -m venv "$VLLM_VENV"
    fi

    # ROCm torch FIRST: the editable install below runs with
    # --no-build-isolation, so whatever torch is in the venv is what the HIP
    # kernels compile against -- a CPU torch here silently breaks the build.
    step "Install ROCm torch wheels (${TORCH_INDEX_URL})"
    uv pip install --python "$VLLM_VENV/bin/python" \
        torch torchvision torchaudio --index-url "$TORCH_INDEX_URL"

    # vllm's build reqs pin an amdsmi version incompatible with ROCm 7.2's
    # calendar versioning -- never let uv resolve it. The ROCm tree ships the
    # matching amdsmi python package; install a COPY because the root-owned
    # original breaks the build cache.
    step "Install amdsmi from /opt/rocm"
    if [[ -d /opt/rocm/share/amd_smi ]]; then
        local amdsmi_tmp
        amdsmi_tmp="$(mktemp -d)"
        cp -r /opt/rocm/share/amd_smi "$amdsmi_tmp/amd_smi"
        uv pip install --python "$VLLM_VENV/bin/python" "$amdsmi_tmp/amd_smi"
        rm -rf "$amdsmi_tmp"
    else
        warn "/opt/rocm/share/amd_smi not found -- skipping amdsmi (vllm GPU metrics will be limited)"
    fi

    # Build deps from the cloned tree (tracks upstream), minus the amdsmi and
    # torch pins -- those would clobber the amdsmi copy / ROCm wheels above.
    # Also filtered: the AMD --extra-index-url (repo.amd.com 403s for
    # non-AMD clients, and uv treats the 403 as fatal for the WHOLE resolve)
    # and the AMD-only triton +git pin (not on PyPI; the rocm torch wheel
    # already pulled its matching pytorch-triton-rocm).
    # The filtered copy lives NEXT TO the original so its `-r ../common.txt`
    # include keeps resolving; it's untracked, so sync_repo ignores it.
    step "Install vllm build deps (requirements/build/rocm.txt, amdsmi/torch/triton pins + AMD index filtered)"
    local build_reqs="$VLLM_SRC_DIR/requirements/build/rocm.txt"
    local filtered_reqs="$VLLM_SRC_DIR/requirements/build/rocm.jaynet.txt"
    [[ -f "$build_reqs" ]] || fail "not found: $build_reqs (upstream layout changed?)"
    grep -vE '^(--(extra-)?index-url|(amdsmi|torch|torchvision|torchaudio|triton)==)' \
        "$build_reqs" > "$filtered_reqs"
    uv pip install --python "$VLLM_VENV/bin/python" -r "$filtered_reqs"

    # --no-build-isolation is CRITICAL (see torch step above). cmake cache in
    # vllm/build persists, so only the first build takes ~20-40 min.
    local pytorch_arch="${AMDGPU_TARGETS// /;}"
    step "Build + install vllm (editable, --no-build-isolation; first build ~20-40 min)"
    cd "$VLLM_SRC_DIR"
    PYTORCH_ROCM_ARCH="$pytorch_arch" \
        uv pip install --python "$VLLM_VENV/bin/python" -e . --no-build-isolation

    [[ -x "$VLLM_VENV/bin/vllm" ]] \
        || fail "vllm install produced no vllm binary in $VLLM_VENV/bin"

    install_vllm_wrapper "$pytorch_arch"
}

# -- Strata HIP engine build (ROCm-only) --------------------------------------
# Versioned prefix like the other served engines, but the binary lands in
# build-hip/ ROOT (not build/bin) -- same shape as install_omni_prefix. The
# engine is a single self-contained binary (system ROCm libs resolve through
# the default linker path); `--target strata` produces no sibling artifacts
# (the vision readers ship only with upstream's packaged engines).
install_strata_prefix() {
    local src_dir="$1" backend="$2"
    local tag dest link
    tag="$(git -C "$src_dir" describe --tags --always 2>/dev/null || echo unknown)"
    dest="$BIN_DIR/strata.${backend}-${tag}"
    link="$BIN_DIR/strata.${backend}"
    step "Install strata (${backend}) -> ${dest}"
    rm -rf "$dest"
    mkdir -p "$dest/bin"
    [[ -x "$src_dir/build-hip/strata" ]] \
        || fail "strata build produced no strata binary in $src_dir/build-hip"
    cp "$src_dir/build-hip/strata" "$dest/bin/strata"
    # An old stub-copy install leaves a REAL directory at the link path.
    [[ -d "$link" && ! -L "$link" ]] && rm -rf "$link"
    ln -sfn "$dest" "$link"
    cat > "$dest/NOTES.md" <<EOF
# Strata (HIP engine, ROCm)

Strata (https://github.com/Niko1221/Strata) serves Qwen3.8-Flash-Next
(125B MoE) on a consumer PC: the GPU holds the hot experts, system RAM/SSD
the rest. Source tree: ${src_dir} (vanilla, synced by
\`build_tools.sh update\`). Engine: \`bin/strata\` in this prefix (stable
symlink ${link}).

- \`strata --serve ...\` speaks the engine's INTERNAL token protocol. The
  OpenAI-compatible layer is upstream's python module
  (\`python -m serve.server\` in the source tree) -- not installed here.
- The ~60-70 GB model pack (GGUF shards + PLE table) is NOT part of this
  build; see docs/AMD_HIP.md in the source tree for packs and starting args.
- Upstream's \`./setup.sh --backend hip\` does the full install (TheRock
  wheels, model download, server) if that is wanted instead.
- Build/validation details for AMD cards:
  https://github.com/Niko1221/Strata/blob/main/docs/AMD_HIP.md
EOF
    ok "Installed strata ${backend}: $link/bin/strata (see ${dest}/NOTES.md)"
}

build_strata_rocm() {
    step "Pre-flight (strata ROCm)"
    command -v hipcc >/dev/null 2>&1 \
        || fail "hipcc not found -- install ROCm (Arch: rocm-hip-sdk, Ubuntu: AMD amdgpu-install)"
    command -v cmake >/dev/null 2>&1 || fail "cmake not found"
    # upstream wants CMake >= 3.24 (cmake_minimum_required)
    local cmake_ver
    cmake_ver="$(cmake --version | head -1 | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)"
    [[ -n "$cmake_ver" ]] || fail "cannot parse cmake version ($(cmake --version | head -1))"
    # sort -VC: sorted ascending iff 3.24 <= cmake_ver
    printf '3.24\n%s\n' "$cmake_ver" | sort -VC \
        || fail "cmake ${cmake_ver} too old -- Strata needs >= 3.24 ($(pkg_hint cmake cmake))"
    # C++20 host compiler (CMAKE_CXX_STANDARD 20); gcc >= 10 / clang >= 10
    command -v g++ >/dev/null 2>&1 || command -v clang++ >/dev/null 2>&1 \
        || fail "no C++ compiler -- $(pkg_hint gcc base-devel)"
    # hipBLAS dev files are REQUIRED by upstream cmake (find_package hipblas);
    # hipBLASLt is QUIET there (tuned dense prefill) -- warn only.
    if [[ -f /opt/rocm/lib/cmake/hipblas/hipblas-config.cmake ]]; then
        ok "hipBLAS dev files present"
    else
        fail "hipBLAS dev files not found under /opt/rocm -- $(pkg_hint hipblas hipblas-dev)"
    fi
    if [[ -f /opt/rocm/lib/cmake/hipblaslt/hipblaslt-config.cmake ]]; then
        ok "hipBLASLt dev files present (tuned dense prefill)"
    else
        warn "hipBLASLt dev files not found under /opt/rocm -- engine falls back to plain hipBLAS prefill"
        warn "  Install with: $(pkg_hint hipblaslt hipblaslt-dev)"
    fi

    check_clone "$STRATA_ROCM_DIR" "https://github.com/Niko1221/Strata"
    sync_repo "$STRATA_ROCM_DIR"
    cd "$STRATA_ROCM_DIR"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean strata HIP build dir"
        rm -rf build-hip
    fi

    step "Configure Strata HIP (${AMDGPU_TARGETS})"
    # Flags mirror upstream setup.py's engine-only build (docs/AMD_HIP.md):
    # PREFILL_MMQ is what setup validates on gfx1201; the explicit HIP
    # compiler + prefix path keep cmake on the system ROCm at /opt/rocm.
    # STRATA_GGML_DIR stays empty: cmake FetchContent's the llama.cpp revision
    # pinned by the repo (network at configure; cached in build-hip after).
    cmake -S . -B build-hip \
        -DCMAKE_BUILD_TYPE=Release \
        -DSTRATA_ENABLE_HIP=ON \
        -DSTRATA_ENABLE_CUDA=OFF \
        -DSTRATA_BUILD_TESTS=OFF \
        -DSTRATA_PREFILL_MMQ=ON \
        -DCMAKE_HIP_ARCHITECTURES="$AMDGPU_TARGETS" \
        -DCMAKE_HIP_COMPILER=/opt/rocm/lib/llvm/bin/clang++ \
        -DCMAKE_HIP_COMPILER_ROCM_ROOT=/opt/rocm \
        -DCMAKE_PREFIX_PATH=/opt/rocm

    step "Build strata HIP (-j${JOBS})"
    cmake --build build-hip --target strata -j"$JOBS"

    [[ -x "$STRATA_ROCM_DIR/build-hip/strata" ]] \
        || fail "strata HIP build produced no strata binary"

    install_strata_prefix "$STRATA_ROCM_DIR" rocm
}

# -- radiance build (ROCm-only, RDNA4) -----------------------------------------
# Versioned prefix via cmake --install: the prefix must be named at CONFIGURE
# time (the plugin search path is compiled in), so the tag is resolved before
# configuring. Tests are NOT run here: upstream's images/videos units need an
# ffmpeg build (RAD_WITH_FFMPEG) and fail without it; the LLM path is covered
# by the remaining fast tier (ctest --test-dir build -L fast).
install_radiance_prefix() {
    local src_dir="$1" backend="$2"
    local tag dest link
    tag="$(git -C "$src_dir" describe --tags --always 2>/dev/null || echo unknown)"
    dest="$BIN_DIR/radiance.${backend}-${tag}"
    link="$BIN_DIR/radiance.${backend}"
    step "Install radiance (${backend}) -> ${dest}"
    rm -rf "$dest"
    cmake --install "$src_dir/build"
    [[ -x "$dest/bin/radiance" ]] \
        || fail "cmake --install produced no radiance binary in $dest/bin"
    [[ -d "$dest/share/radiance" ]] \
        || fail "cmake --install produced no plugin tree in $dest/share/radiance"
    # An old stub-copy install leaves a REAL directory at the link path.
    [[ -d "$link" && ! -L "$link" ]] && rm -rf "$link"
    ln -sfn "$dest" "$link"
    cat > "$dest/NOTES.md" <<EOF
# radiance (C++/HIP inference server, ROCm)

radiance (https://codeberg.org/StillDeadcode/radiance) serves language,
image, video, speech and music models on AMD RDNA4 (gfx1200/1201) over
OpenAI's chat/completions/Responses/images/audio APIs, Anthropic's Messages
API and llama-server's native one. Source tree: ${src_dir} (vanilla, synced
by \`build_tools.sh update\`). Engine: \`bin/radiance\` in this prefix
(stable symlink ${link}); the plugin tree (kernels/architectures/
quantizers) sits in \`share/radiance\` and is found automatically.

- Models are .rad containers (weights + tokenizer + chat template +
  drafter), e.g.:
    curl -LC - -o /srv/models/rad/qwen3.8-27b-mxfp4.rad \\
      https://huggingface.co/StillDeadcode/qwen3.8-27b-mxfp4/resolve/main/qwen3.8-27b-mxfp4.rad
  \`--model\` also takes a Hugging Face repo id (fetched on startup).
- Serve (flags per the model's compose file in the source tree,
  deploy/compose/*.yaml), e.g. Qwen3.8-27B MXFP4 on two cards:
    ${link}/bin/radiance --model /srv/models/rad/qwen3.8-27b-mxfp4.rad \\
      --host 127.0.0.1 --port 8079 \\
      --tp 2 --max-num-seqs 32 --max-model-len 200000 --kv-cache-dtype fp8
  Add \`--api-key KEY\` to require a bearer key.
- JayNet integration: adopt the running server as a REMOTE preset
  (Admin -> Models -> Presets, backend openai) -- no launcher needed.
- This build has NO ffmpeg (RAD_WITH_FFMPEG off): image/video media-decode
  containers are untested here; LLM/image-gen containers work. Rebuild with
  ffmpeg installed to enable them.
EOF
    ok "Installed radiance ${backend}: $link/bin/radiance (see ${dest}/NOTES.md)"
}

build_radiance_rocm() {
    step "Pre-flight (radiance ROCm)"
    command -v hipcc >/dev/null 2>&1 \
        || fail "hipcc not found -- install ROCm (Arch: rocm-hip-sdk, Ubuntu: AMD amdgpu-install)"
    command -v ninja >/dev/null 2>&1 || fail "ninja not found"
    command -v cmake >/dev/null 2>&1 || fail "cmake not found"

    check_clone "$RADIANCE_ROCM_DIR" "https://codeberg.org/StillDeadcode/radiance.git"
    sync_repo "$RADIANCE_ROCM_DIR"
    cd "$RADIANCE_ROCM_DIR"

    local tag dest
    tag="$(git describe --tags --always 2>/dev/null || echo unknown)"
    dest="$BIN_DIR/radiance.rocm-${tag}"

    if [[ $CLEAN -eq 1 ]]; then
        step "Clean radiance build dir"
        rm -rf build
    fi
    # The plugin path is compiled in from the configure-time prefix: when the
    # tag moved (update), the cached prefix is stale and a plain re-configure
    # may keep serving it. Wipe the build dir on mismatch.
    if [[ -f build/CMakeCache.txt ]] \
            && ! grep -q "^CMAKE_INSTALL_PREFIX:PATH=${dest}\$" build/CMakeCache.txt; then
        step "Prefix changed (tag ${tag}) -- clean radiance build dir"
        rm -rf build
    fi

    step "Configure radiance (${AMDGPU_TARGETS}) -> prefix ${dest}"
    # CMAKE_INSTALL_PREFIX at configure time: the compiled-in plugin path
    # (<prefix>/share/radiance) comes from this first configure.
    cmake -S . -B build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DRAD_GPU_TARGETS="$AMDGPU_TARGETS" \
        -DCMAKE_INSTALL_PREFIX="$dest"

    step "Build radiance (-j${JOBS})"
    cmake --build build -j"$JOBS"

    [[ -x "$RADIANCE_ROCM_DIR/build/bin/radiance" ]] \
        || fail "radiance build produced no radiance binary in build/bin"

    install_radiance_prefix "$RADIANCE_ROCM_DIR" rocm
}

# -- Sanity: toolchain ------------------------------------------------------
preflight() {
    step "Pre-flight checks"

    command -v cmake >/dev/null 2>&1 || fail "cmake not found"
    command -v git   >/dev/null 2>&1 || fail "git not found"

    # Ninja required for SD ROCm build
    if [[ " ${JOBS_LIST[*]} " == *" sd-rocm "* ]]; then
        command -v ninja >/dev/null 2>&1 \
            || fail "ninja not found (required for SD ROCm build) -- $(pkg_hint ninja ninja-build)"
    fi

    # piper + tools need python3 (venvs); piper also needs curl (voice download)
    if [[ " ${JOBS_LIST[*]} " == *" piper "* || " ${JOBS_LIST[*]} " == *" tools "* ]]; then
        command -v python3 >/dev/null 2>&1 || fail "python3 not found (required for piper/tools)"
        python3 -m venv --help >/dev/null 2>&1 \
            || fail "python3 venv module missing -- $(pkg_hint python python3-venv)"
    fi
    if [[ " ${JOBS_LIST[*]} " == *" piper "* ]]; then
        command -v curl >/dev/null 2>&1 \
            || fail "curl not found (required for piper voice download)"
    fi
    if [[ " ${JOBS_LIST[*]} " == *" k2horizon-"* ]]; then
        command -v curl >/dev/null 2>&1 \
            || fail "curl not found (required for K2-Horizon patch download)"
    fi

    # vllm: python3.12 venv (torch ROCm wheels target <=3.13) + ROCm at
    # /opt/rocm (torch wheels, amdsmi source copy)
    if [[ " ${JOBS_LIST[*]} " == *" vllm-rocm "* ]]; then
        command -v python3.12 >/dev/null 2>&1 \
            || fail "python3.12 not found (required for vllm) -- $(pkg_hint python312 python3.12)"
        python3.12 -m venv --help >/dev/null 2>&1 \
            || fail "python3.12 venv module missing -- $(pkg_hint python312 python3.12-venv)"
        ok "python3.12 present: $(python3.12 --version 2>&1)"
        command -v uv >/dev/null 2>&1 \
            || fail "uv not found (required for vllm) -- $(pkg_hint uv uv)"
        if [[ -r /opt/rocm/.info/version ]]; then
            ok "ROCm $(head -1 /opt/rocm/.info/version) at /opt/rocm"
        else
            warn "/opt/rocm/.info/version not readable -- vllm ROCm build expects ROCm at /opt/rocm"
        fi
        if command -v cargo >/dev/null 2>&1 || [[ -f "$HOME/.cargo/env" ]]; then
            ok "cargo present (vllm Rust frontend)"
        else
            warn "cargo not found -- rustup will be installed non-interactively at build time"
        fi
        # The ROCm torch wheel's cmake link interface hard-requires
        # roc::hipsparselt (its libtorch_hip.so NEEDs the bundled lib); a
        # ROCm install without the hipsparselt package fails at cmake
        # generate with "target roc::hipsparselt was not found" (hit on the
        # first gfx1201 build, 2026-10-19).
        if compgen -G "/opt/rocm/lib/cmake/hipsparselt/hipsparselt-config.cmake" > /dev/null \
                || compgen -G "/opt/rocm/lib/cmake/hipsparselt*/hipsparselt-config.cmake" > /dev/null; then
            ok "hipsparselt cmake config present (torch wheel link interface)"
        else
            fail "hipsparselt not found under /opt/rocm -- the ROCm torch wheel's cmake targets link roc::hipsparselt, so the vllm build cannot configure without it. $(pkg_hint hipsparselt hipsparselt)"
        fi
    fi

    # strata: system ROCm with HIP dev files (hipcc, hipBLAS/hipBLASLt) +
    # cmake >= 3.24; the engine build itself re-checks in the same style
    if [[ " ${JOBS_LIST[*]} " == *" strata-rocm "* ]]; then
        command -v hipcc >/dev/null 2>&1 \
            || fail "hipcc not found (required for strata) -- install ROCm (Arch: rocm-hip-sdk, Ubuntu: AMD amdgpu-install)"
        ok "hipcc present: $(hipcc --version 2>/dev/null | head -1)"
        if [[ -r /opt/rocm/.info/version ]]; then
            ok "ROCm $(head -1 /opt/rocm/.info/version) at /opt/rocm"
        else
            warn "/opt/rocm/.info/version not readable -- strata HIP build expects ROCm at /opt/rocm"
        fi
        local cmake_ver
        cmake_ver="$(cmake --version | head -1 | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)"
        if [[ -n "$cmake_ver" ]] && printf '3.24\n%s\n' "$cmake_ver" | sort -VC 2>/dev/null; then
            ok "cmake ${cmake_ver} (>= 3.24)"
        else
            fail "cmake ${cmake_ver:-unknown} too old -- Strata needs >= 3.24 ($(pkg_hint cmake cmake))"
        fi
    fi

    # radiance: system ROCm (hipcc, ROCm 10.1 or 7.2), cmake >= 3.21, ninja,
    # C++20 compiler; the engine build re-checks in the same style
    if [[ " ${JOBS_LIST[*]} " == *" radiance-rocm "* ]]; then
        command -v hipcc >/dev/null 2>&1 \
            || fail "hipcc not found (required for radiance) -- install ROCm (Arch: rocm-hip-sdk, Ubuntu: AMD amdgpu-install)"
        ok "hipcc present: $(hipcc --version 2>/dev/null | head -1)"
        command -v ninja >/dev/null 2>&1 \
            || fail "ninja not found (required for radiance) -- $(pkg_hint ninja ninja-build)"
        command -v g++ >/dev/null 2>&1 || command -v clang++ >/dev/null 2>&1 \
            || fail "no C++20 compiler -- $(pkg_hint gcc base-devel)"
        local cmake_ver
        cmake_ver="$(cmake --version | head -1 | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)"
        if [[ -n "$cmake_ver" ]] && printf '3.21\n%s\n' "$cmake_ver" | sort -VC 2>/dev/null; then
            ok "cmake ${cmake_ver} (>= 3.21)"
        else
            fail "cmake ${cmake_ver:-unknown} too old -- radiance needs >= 3.21 ($(pkg_hint cmake cmake))"
        fi
    fi

    # pnpm/npm required for SD web UI -- warn once if any SD backend is built
    if [[ " ${JOBS_LIST[*]} " == *" sd-"* ]]; then
        if command -v pnpm >/dev/null 2>&1; then
            ok "pnpm present for web UI build"
        elif command -v npm >/dev/null 2>&1; then
            warn "pnpm not found -- web UI will build with npm (slower)"
            warn "  Recommended: $(pkg_hint pnpm pnpm)"
        else
            warn "Neither pnpm nor npm found -- web UI will be skipped"
            warn "  Install with: $(pkg_hint pnpm pnpm)  (or nodejs/npm via your distro)"
        fi
    fi

    # Dedupe backends across projects so we don't print the same warning twice
    local checked_rocm=0 checked_vulkan=0 checked_cuda=0 checked_sycl=0
    for job in "${JOBS_LIST[@]}"; do
        local backend="${job#*-}"

        if [[ "$backend" == "rocm" && $checked_rocm -eq 0 ]]; then
            checked_rocm=1
            command -v hipcc >/dev/null 2>&1 || fail "hipcc not found -- install ROCm (Arch: rocm-hip-sdk, Ubuntu: AMD amdgpu-install)"
            if ! command -v rocminfo >/dev/null 2>&1; then
                warn "rocminfo not found -- $(pkg_hint rocminfo rocminfo) (skipping device check)"
            else
                local n
                # grep -c reads ALL of rocminfo's output — a `grep -q` here
                # exits on the first match, rocminfo dies with SIGPIPE, and
                # pipefail turns the pipeline into a false "not found".
                # ^  Name anchors on agent blocks (skips the amdgcn target
                # lines, which would double-count each GPU).
                n=$(rocminfo 2>/dev/null | grep -cE "^  Name:.*${AMDGPU_TARGETS}" || true)
                if [[ ${n:-0} -gt 0 ]]; then
                    ok "rocminfo sees ${n} ${AMDGPU_TARGETS} device(s)"
                else
                    warn "rocminfo doesn't list ${AMDGPU_TARGETS}. Check:"
                    warn "  1. Your installed ROCm runtime supports this GPU"
                    warn "  2. User in render+video groups: groups | grep -E 'render|video'"
                    warn "  3. amdgpu kernel module loaded: lsmod | grep amdgpu"
                    warn "  4. Wrong target picked? Re-run with AMDGPU_TARGETS=<your gfx>"
                fi
            fi
            # rocWMMA only matters for llama.cpp (SD has it off intentionally)
            if [[ " ${JOBS_LIST[*]} " == *" llama-rocm "* ]]; then
                if [[ ! -f /opt/rocm/include/rocwmma/rocwmma-version.hpp \
                   && ! -f /usr/include/rocwmma/rocwmma-version.hpp ]]; then
                    warn "rocwmma headers missing -- $(pkg_hint rocwmma rocwmma-dev) (optional but recommended for llama.cpp)"
                fi
            fi
        fi

        if [[ "$backend" == "vulkan" && $checked_vulkan -eq 0 ]]; then
            checked_vulkan=1
            if [[ ! -f /usr/include/vulkan/vulkan.h ]]; then
                warn "vulkan headers missing -- $(pkg_hint vulkan-headers vulkan-headers)"
            else
                ok "vulkan headers present"
            fi
            if ! ls /usr/{lib,share}/cmake/SPIRV-Headers/*.cmake >/dev/null 2>&1 \
               && ! ls /usr/{lib,share}/cmake/spirv-headers/*.cmake >/dev/null 2>&1; then
                warn "SPIRV-Headers CMake config missing -- $(pkg_hint "spirv-headers spirv-tools" "spirv-headers spirv-tools")"
            fi
            # same SIGPIPE+pipefail trap as the rocminfo check above: -q
            # exits early, the producer dies, the pipeline reports failure.
            local nv
            nv=$(ldconfig -p 2>/dev/null | grep -c "libvulkan.so" || true)
            [[ ${nv:-0} -eq 0 ]] \
                && warn "libvulkan.so not found -- $(pkg_hint vulkan-icd-loader libvulkan-dev)"
            command -v glslc >/dev/null 2>&1 \
                || warn "glslc not found -- $(pkg_hint shaderc glslc)"
            if ! ls /usr/share/vulkan/icd.d/radeon_icd*.json >/dev/null 2>&1; then
                warn "RADV driver not installed -- $(pkg_hint vulkan-radeon mesa-vulkan-drivers)"
            fi
            if command -v vulkaninfo >/dev/null 2>&1; then
                local n
                n=$(vulkaninfo --summary 2>/dev/null | grep -c "deviceName.*Radeon" || true)
                ok "vulkaninfo sees ${n} Radeon device(s)"
            else
                warn "vulkaninfo not found -- $(pkg_hint vulkan-tools vulkan-tools) (optional)"
            fi
        fi

        if [[ "$backend" == "cuda" && $checked_cuda -eq 0 ]]; then
            checked_cuda=1
            command -v nvcc >/dev/null 2>&1 \
                || fail "nvcc not found -- install the CUDA toolkit (Arch: cuda, Ubuntu: nvidia-cuda-toolkit)"
            if command -v nvidia-smi >/dev/null 2>&1; then
                local n
                n=$(nvidia-smi --list-gpus 2>/dev/null | grep -c "^GPU" || true)
                ok "nvidia-smi sees ${n} NVIDIA device(s)"
            else
                warn "nvidia-smi not found -- skipping device check"
            fi
        fi

        if [[ "$backend" == "sycl" && $checked_sycl -eq 0 ]]; then
            checked_sycl=1
            if command -v icpx >/dev/null 2>&1; then
                ok "icpx present: $(icpx --version 2>/dev/null | head -1)"
            elif [[ -f /opt/intel/oneapi/setvars.sh ]]; then
                ok "oneAPI setvars.sh found (will be sourced at build time)"
            else
                fail "icpx not found and no /opt/intel/oneapi/setvars.sh -- install Intel oneAPI (AUR: intel-oneapi-compiler-dpcpp-cpp)"
            fi
        fi
    done
}

# -- Main -------------------------------------------------------------------
main() {
    echo -e "${BOLD}llama.cpp + stable-diffusion.cpp + whisper.cpp build${NC}"
    echo -e "${DIM}Projects: ${PROJECTS[*]} | Backends: ${BACKENDS[*]} | Clean: ${CLEAN} | Jobs: ${JOBS}${NC}"
    echo -e "${DIM}Install:  ${BIN_DIR}${NC}"
    echo -e "${DIM}Plan:     ${JOBS_LIST[*]}${NC}"

    preflight

    local t0
    t0=$(date +%s)

    for job in "${JOBS_LIST[@]}"; do
        case "$job" in
            llama-rocm)     build_llama_rocm ;;
            llama-vulkan)   build_llama_vulkan ;;
            llama-cuda)     build_llama_cuda ;;
            llama-sycl)     build_llama_sycl ;;
            sd-rocm)        build_sd_rocm ;;
            sd-vulkan)      build_sd_vulkan ;;
            sd-cuda)        build_sd_cuda ;;
            sd-sycl)        build_sd_sycl ;;
            whisper-rocm)   build_whisper_rocm ;;
            whisper-vulkan) build_whisper_vulkan ;;
            whisper-cuda)   build_whisper_cuda ;;
            whisper-sycl)   build_whisper_sycl ;;
            llama-cpu)      build_llama_cpu ;;
            k2horizon-rocm)   build_k2horizon_rocm ;;
            k2horizon-vulkan) build_k2horizon_vulkan ;;
            k2horizon-cuda)   build_k2horizon_cuda ;;
            k2horizon-cpu)    build_k2horizon_cpu ;;
            spark-rocm)       build_spark_rocm ;;
            spark-cpu)        build_spark_cpu ;;
            bonsai-rocm)      build_bonsai_rocm ;;
            bonsai-cpu)       build_bonsai_cpu ;;
            omnivoice-rocm)   build_omnivoice_rocm ;;
            omnivoice-vulkan) build_omnivoice_vulkan ;;
            omnivoice-cuda)   build_omnivoice_cuda ;;
            omnivoice-cpu)    build_omnivoice_cpu ;;
            vllm-rocm)        build_vllm_rocm ;;
            strata-rocm)      build_strata_rocm ;;
            radiance-rocm)    build_radiance_rocm ;;
            sd-cpu)         build_sd_cpu ;;
            whisper-cpu)    build_whisper_cpu ;;
            piper)          build_piper ;;
            tools)          build_llmtools ;;
        esac
    done

    local elapsed=$(( $(date +%s) - t0 ))
    echo
    echo -e "${GREEN}${BOLD}Done${NC} in ${elapsed}s"
    echo -e "Binaries installed to ${BOLD}${BIN_DIR}${NC} (add to PATH to use directly)"
    echo
    for job in "${JOBS_LIST[@]}"; do
        case "$job" in
            llama-rocm)     echo -e "  llama   ROCm:   ${BIN_DIR}/llama.cpp.rocm/bin/llama-server" ;;
            llama-vulkan)   echo -e "  llama   Vulkan: ${BIN_DIR}/llama.cpp.vulkan/bin/llama-server" ;;
            llama-cuda)     echo -e "  llama   CUDA:   ${BIN_DIR}/llama.cpp.cuda/bin/llama-server" ;;
            llama-sycl)     echo -e "  llama   SYCL:   ${BIN_DIR}/llama.cpp.sycl/bin/llama-server" ;;
            sd-rocm)        echo -e "  sd      ROCm:   ${SD_ROCM_DIR}/build/bin/{sd-cli,sd-server}" ;;
            sd-vulkan)      echo -e "  sd      Vulkan: ${SD_VULKAN_DIR}/build/bin/{sd-cli,sd-server}" ;;
            sd-cuda)        echo -e "  sd      CUDA:   ${SD_CUDA_DIR}/build/bin/{sd-cli,sd-server}" ;;
            sd-sycl)        echo -e "  sd      SYCL:   ${SD_SYCL_DIR}/build/bin/{sd-cli,sd-server}" ;;
            whisper-rocm)   echo -e "  whisper ROCm:   ${BIN_DIR}/whisper.cpp.rocm/bin/whisper-server" ;;
            whisper-vulkan) echo -e "  whisper Vulkan: ${BIN_DIR}/whisper.cpp.vulkan/bin/whisper-server" ;;
            whisper-cuda)   echo -e "  whisper CUDA:   ${BIN_DIR}/whisper.cpp.cuda/bin/whisper-server" ;;
            whisper-sycl)   echo -e "  whisper SYCL:   ${BIN_DIR}/whisper.cpp.sycl/bin/whisper-server" ;;
            llama-cpu)      echo -e "  llama   CPU:    ${BIN_DIR}/llama.cpp.cpu/bin/llama-server" ;;
            k2horizon-rocm)   echo -e "  k2horizon ROCm:   ${BIN_DIR}/llama.cpp-k2horizon.rocm/bin/llama-server" ;;
            k2horizon-vulkan) echo -e "  k2horizon Vulkan: ${BIN_DIR}/llama.cpp-k2horizon.vulkan/bin/llama-server" ;;
            k2horizon-cuda)   echo -e "  k2horizon CUDA:   ${BIN_DIR}/llama.cpp-k2horizon.cuda/bin/llama-server" ;;
            k2horizon-cpu)    echo -e "  k2horizon CPU:    ${BIN_DIR}/llama.cpp-k2horizon.cpu/bin/llama-server" ;;
            bonsai-rocm)      echo -e "  bonsai  ROCm:   ${BIN_DIR}/llama.cpp-bonsai.rocm/bin/llama-server" ;;
            bonsai-cpu)       echo -e "  bonsai  CPU:    ${BIN_DIR}/llama.cpp-bonsai.cpu/bin/llama-server" ;;
            omnivoice-rocm)   echo -e "  omnivoice ROCm:   ${BIN_DIR}/omnivoice.cpp.rocm/bin/tts-server" ;;
            omnivoice-vulkan) echo -e "  omnivoice Vulkan: ${BIN_DIR}/omnivoice.cpp.vulkan/bin/tts-server" ;;
            omnivoice-cuda)   echo -e "  omnivoice CUDA:   ${BIN_DIR}/omnivoice.cpp.cuda/bin/tts-server" ;;
            omnivoice-cpu)    echo -e "  omnivoice CPU:    ${BIN_DIR}/omnivoice.cpp.cpu/bin/tts-server" ;;
            vllm-rocm)        echo -e "  vllm    ROCm:   ${BIN_DIR}/vllm (wrapper; venv ${VLLM_VENV}; see ${BIN_DIR}/vllm-rocm/ENVIRONMENT.md)" ;;
            strata-rocm)      echo -e "  strata  ROCm:   ${BIN_DIR}/strata.rocm/bin/strata (see ${BIN_DIR}/strata.rocm/NOTES.md)" ;;
            radiance-rocm)    echo -e "  radiance ROCm:  ${BIN_DIR}/radiance.rocm/bin/radiance (see ${BIN_DIR}/radiance.rocm/NOTES.md)" ;;
            sd-cpu)         echo -e "  sd      CPU:    ${SD_CPU_DIR}/build/bin/{sd-cli,sd-server}" ;;
            whisper-cpu)    echo -e "  whisper CPU:    ${BIN_DIR}/whisper.cpp.cpu/bin/whisper-server" ;;
            piper)          echo -e "  piper   TTS:    ${PIPER_VENV}/bin/piper (voice: ${PIPER_VOICE_DIR}/${PIPER_VOICE}.onnx)" ;;
            tools)          echo -e "  tools   venv:   ${TOOLS_VENV}/bin/hf (hf download CLI)" ;;
        esac
    done
    if [[ -f "$SD_FRONTEND_HTML" ]]; then
        echo -e "  sd     WebUI:  ${SD_FRONTEND_HTML}"
        echo
        echo -e "${DIM}Launch web UI with:${NC}"
        echo -e "  sd-server --serve-html-path ${SD_FRONTEND_HTML} \\"
        echo -e "    --diffusion-model ... --vae ... --llm ... \\"
        echo -e "    -l 0.0.0.0 --listen-port 1234 --diffusion-fa -v"
    fi
    echo
    # if/then (not `[[ ]] && echo`): a false trailing &&-list would make
    # main() — and the whole script — exit 1 after a successful build
    echo -e "${DIM}Quick tests:${NC}"
    if [[ " ${JOBS_LIST[*]} " == *" llama-rocm "* ]]; then
        echo -e "  ${BIN_DIR}/llama.cpp.rocm/bin/llama-server --list-devices"
    fi
    if [[ " ${JOBS_LIST[*]} " == *" sd-rocm "* ]]; then
        echo -e "  ${SD_ROCM_DIR}/build/bin/sd-cli --help | head -30"
    fi
    if [[ " ${JOBS_LIST[*]} " == *" whisper-rocm "* ]]; then
        echo -e "  ${BIN_DIR}/whisper.cpp.rocm/bin/whisper-server -m ~/jaynet-models/whisper/ggml-small.bin --port 8097"
    fi
    if [[ " ${JOBS_LIST[*]} " == *" omnivoice-rocm "* ]]; then
        echo -e "  echo 'hello world' | ${BIN_DIR}/omnivoice.cpp.rocm/bin/omnivoice-tts --model <omnivoice-base-Q8_0.gguf> --codec <omnivoice-tokenizer-F32.gguf> --lang English -o /tmp/ov.wav"
    fi
    if [[ " ${JOBS_LIST[*]} " == *" vllm-rocm "* ]]; then
        echo -e "  ${BIN_DIR}/vllm --help"
    fi
    if [[ " ${JOBS_LIST[*]} " == *" strata-rocm "* ]]; then
        echo -e "  ${BIN_DIR}/strata.rocm/bin/strata --version"
    fi
    if [[ " ${JOBS_LIST[*]} " == *" radiance-rocm "* ]]; then
        echo -e "  ${BIN_DIR}/radiance.rocm/bin/radiance --help | head -20"
    fi
    if [[ " ${JOBS_LIST[*]} " == *" piper "* ]]; then
        echo -e "  echo 'hello world' | ${PIPER_VENV}/bin/piper --model ${PIPER_VOICE_DIR}/${PIPER_VOICE}.onnx --output_file /tmp/piper-test.wav"
    fi
    if [[ " ${JOBS_LIST[*]} " == *" tools "* ]]; then
        echo -e "  ${TOOLS_VENV}/bin/hf --version"
    fi
}

# BUILD_TOOLS_NO_MAIN=1: print the resolved plan and stop (testing/dry-run)
if [[ "${BUILD_TOOLS_NO_MAIN:-}" == "1" ]]; then
    echo "PLAN: ${JOBS_LIST[*]}"
else
    main
fi
