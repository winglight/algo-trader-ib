#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

REPO_URL="${ATI_PUBLIC_REPO_URL:-https://github.com/winglight/algo-trader-ib.git}"
ARCHIVE_URL="${ATI_PUBLIC_ARCHIVE_URL:-https://github.com/winglight/algo-trader-ib/archive/refs/heads/main.tar.gz}"
INSTALL_DIR="${ATI_INSTALL_DIR:-$HOME/ati-local-runtime}"

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

prompt_install_dir() {
  local value
  read -r -p "Install directory [${INSTALL_DIR}]: " value
  if [ -n "$value" ]; then
    INSTALL_DIR="$value"
  fi
}

download_with_git() {
  if [ -d "${INSTALL_DIR}/.git" ]; then
    git -C "$INSTALL_DIR" pull --ff-only
  elif [ ! -e "$INSTALL_DIR" ] || [ -z "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 2>/dev/null | head -n 1)" ]; then
    git clone "$REPO_URL" "$INSTALL_DIR"
  else
    download_with_archive
  fi
}

download_with_archive() {
  local tmp
  tmp="$(mktemp -d)"
  curl -fsSL "$ARCHIVE_URL" -o "${tmp}/ati-local-runtime.tar.gz"
  mkdir -p "$INSTALL_DIR"
  tar -xzf "${tmp}/ati-local-runtime.tar.gz" -C "$tmp"
  local extracted
  extracted="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d -name 'algo-trader-ib-*' | head -n 1)"
  if [ -z "$extracted" ]; then
    echo "Unable to locate downloaded installer files." >&2
    exit 1
  fi
  cp -R "${extracted}/." "$INSTALL_DIR/"
  rm -rf "$tmp"
}

prompt_install_dir
mkdir -p "$(dirname "$INSTALL_DIR")"

if has_cmd git; then
  download_with_git
else
  if ! has_cmd curl || ! has_cmd tar; then
    echo "This installer needs git, or curl and tar." >&2
    exit 1
  fi
  download_with_archive
fi

chmod +x "${INSTALL_DIR}/setup_and_run.sh" "${INSTALL_DIR}/scripts/install_docker.sh" || true
cd "$INSTALL_DIR"
exec bash ./setup_and_run.sh
