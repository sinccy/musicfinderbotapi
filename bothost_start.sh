#!/usr/bin/env bash
# Bothost без custom Dockerfile: ставим ffmpeg/nodejs/tesseract при старте, затем bot.py
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p /app/data 2>/dev/null || mkdir -p ./data

install_apt() {
  command -v apt-get >/dev/null 2>&1 || return 1
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y --no-install-recommends \
    ffmpeg nodejs tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus \
    curl ca-certificates \
    || return 1
  rm -rf /var/lib/apt/lists/*
}

install_node_tarball() {
  command -v node >/dev/null 2>&1 && return 0
  command -v nodejs >/dev/null 2>&1 && return 0
  command -v curl >/dev/null 2>&1 || return 1
  local ver=20.18.0 arch dir
  arch="$(uname -m)"
  case "$arch" in
    x86_64) dir="node-v${ver}-linux-x64" ;;
    aarch64|arm64) dir="node-v${ver}-linux-arm64" ;;
    *) return 1 ;;
  esac
  local tmp="/tmp/${dir}"
  if [[ ! -x "${tmp}/bin/node" ]]; then
    echo "Downloading Node.js ${ver} (${arch})…"
    curl -fsSL "https://nodejs.org/dist/v${ver}/${dir}.tar.xz" \
      | tar -xJ -C /tmp
  fi
  export PATH="${tmp}/bin:${PATH}"
}

need_ffmpeg=false
need_node=false
command -v ffmpeg >/dev/null 2>&1 || need_ffmpeg=true
command -v node >/dev/null 2>&1 || command -v nodejs >/dev/null 2>&1 || need_node=true

if $need_ffmpeg || $need_node || ! command -v tesseract >/dev/null 2>&1; then
  echo "Bothost: installing system deps (ffmpeg, nodejs, tesseract)…"
  install_apt || echo "apt-get skipped or failed — trying fallbacks"
fi

install_node_tarball || true

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not in PATH — installing imageio-ffmpeg via pip…"
  python -m pip install -q imageio-ffmpeg 2>/dev/null || pip install -q imageio-ffmpeg 2>/dev/null || true
fi

echo "Deps: ffmpeg=$(command -v ffmpeg || echo none) node=$(command -v node || command -v nodejs || echo none) tesseract=$(command -v tesseract || echo none)"

exec python bot.py
