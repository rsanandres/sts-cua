#!/bin/bash
# Install llama.cpp's llama-server with CUDA inside WSL, into ~/llama.cpp, for
# `python -m sts_cua.serve` and the app's Model server card. Safe to run again: it pulls
# and rebuilds (or re-downloads) in place. Prints the llama-server path at the end.
#
#   tools/install_llama_cpp.sh              # build from source (default)
#   tools/install_llama_cpp.sh --prebuilt   # the newest release's Ubuntu CUDA 12.8 binaries
#   tools/install_llama_cpp.sh --prebuilt b11900
#
# Build from source (docs/build.md in ggml-org/llama.cpp, "CUDA"):
#   cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89 && cmake --build build --config Release -j
#   89 is the RTX 4090's compute capability (8.9; listed in that doc). Override with CUDA_ARCH=...
# This needs the CUDA toolkit (nvcc) inside WSL. NVIDIA's CUDA on WSL guide
# (https://docs.nvidia.com/cuda/wsl-user-guide/index.html): the Windows driver is the only
# driver; never install a Linux NVIDIA driver in WSL, and install only the
# `cuda-toolkit-12-x` package (WSL-Ubuntu repo), not the `cuda`, `cuda-12-x` or `cuda-drivers`
# meta-packages, which pull a Linux driver in.
#
# Prebuilt: the release page lists llama-<tag>-bin-ubuntu-cuda-12.8-x64.tar.gz plus a
# cudart-... archive with the CUDA runtime (seen 2026-10-26 for b11900; a cuda-13.4 pair too),
# so no toolkit is needed; the Windows driver must support CUDA 12.8 (Raph's reports 13.4).
# The b<N> releases are marked pre-release, so the newest tag comes from the releases feed. The archive layout is not checked on
# the PC yet: the script finds llama-server inside it and checks its libraries with ldd.
set -euo pipefail

DEST="${LLAMA_CPP_DIR:-$HOME/llama.cpp}"
BIN="$DEST/build/bin/llama-server"
CUDA_ARCH="${CUDA_ARCH:-89}"
REPO_URL="https://github.com/ggml-org/llama.cpp"
log() { echo "[install_llama_cpp] $*"; }

build_from_source() {
  for tool in git cmake g++; do
    command -v "$tool" >/dev/null || { log "missing $tool: sudo apt-get install -y git cmake build-essential"; exit 1; }
  done
  local nvcc
  nvcc=$(command -v nvcc || true)
  [ -z "$nvcc" ] && [ -x /usr/local/cuda/bin/nvcc ] && nvcc=/usr/local/cuda/bin/nvcc
  if [ -z "$nvcc" ]; then
    log "nvcc not found. Install the CUDA toolkit for WSL-Ubuntu (cuda-toolkit-12-x only, no driver):"
    log "  https://developer.nvidia.com/cuda-downloads (Linux > x86_64 > WSL-Ubuntu)"
    log "or run this script with --prebuilt, which needs no toolkit."
    exit 1
  fi
  log "nvcc: $nvcc ($("$nvcc" --version | tail -n 1))"
  if [ -d "$DEST/.git" ]; then
    log "updating $DEST"
    git -C "$DEST" pull --ff-only
  else
    [ -e "$DEST" ] && { log "$DEST exists and isn't a git checkout; move it or set LLAMA_CPP_DIR"; exit 1; }
    log "cloning $REPO_URL into $DEST"
    git clone --depth 1 "$REPO_URL" "$DEST"
  fi
  cd "$DEST"
  cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCH" -DCMAKE_CUDA_COMPILER="$nvcc" \
    -DCMAKE_BUILD_TYPE=Release
  cmake --build build --config Release -j "$(nproc)" --target llama-server
}

install_prebuilt() {
  local tags=${1:-}
  if [ -z "$tags" ]; then
    # The b<N> builds are all marked pre-release, so releases/latest points at an old v0.x
    # release; the releases feed lists the builds newest first. The newest may not have its
    # archives uploaded yet, so try the first few.
    tags=$(curl -fsSL "$REPO_URL/releases.atom" | grep -oE '/releases/tag/b[0-9]+' | sed 's#.*/tag/##' | head -n 5 || true)
  fi
  [ -n "$tags" ] || { log "couldn't read the release list; pass a tag, e.g. --prebuilt b11900"; exit 1; }
  local tmp tag="" t name
  tmp=$(mktemp -d)
  trap "rm -rf '$tmp'" EXIT  # expanded now: $tmp is local and gone by exit
  for t in $tags; do
    [[ "$t" =~ ^b[0-9]+$ ]] || { log "not a build tag: '$t'; pass one like b11900"; exit 1; }
    rm -rf "$tmp"/*
    mkdir -p "$tmp/x"
    for name in "llama-$t-bin-ubuntu-cuda-12.8-x64.tar.gz" "cudart-llama-$t-bin-ubuntu-cuda-12.8-x64.tar.gz"; do
      log "downloading $t/$name"
      curl -fL --retry 3 -o "$tmp/$name" "$REPO_URL/releases/download/$t/$name" && tar -xzf "$tmp/$name" -C "$tmp/x" \
        || { log "$t: couldn't fetch $name"; continue 2; }
    done
    tag=$t
    break
  done
  [ -n "$tag" ] || { log "no release among: $tags had both archives; see $REPO_URL/releases"; exit 1; }
  local found
  found=$(find "$tmp/x" -type f -name llama-server | head -n 1)
  [ -n "$found" ] || { log "no llama-server in the archives; see $REPO_URL/releases/tag/$tag"; exit 1; }
  mkdir -p "$DEST/build/bin"
  cp -a "$(dirname "$found")"/. "$DEST/build/bin/"
  # The CUDA runtime archive may unpack somewhere else: put its libraries next to the binary.
  find "$tmp/x" -name 'libcu*.so*' -exec cp -a {} "$DEST/build/bin/" \;
  echo "$tag" > "$DEST/build/bin/RELEASE"
  if ldd "$BIN" | grep -q "not found"; then
    log "missing libraries (try LD_LIBRARY_PATH=$DEST/build/bin):"
    ldd "$BIN" | grep "not found"
  fi
}

case "${1:-}" in
  --prebuilt) install_prebuilt "${2:-}" ;;
  ""|--build) build_from_source ;;
  *) echo "usage: $0 [--build | --prebuilt [tag]]"; exit 2 ;;
esac

[ -x "$BIN" ] || { log "build finished but $BIN is missing"; exit 1; }
"$BIN" --version 2>&1 | head -n 4 || true
log "llama-server: $BIN"
log "python -m sts_cua.serve finds it there (or set LLAMA_SERVER=$BIN)."
