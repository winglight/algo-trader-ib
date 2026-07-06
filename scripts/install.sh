#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

REPO_URL="${ATI_PUBLIC_REPO_URL:-https://github.com/winglight/algo-trader-ib.git}"
ARCHIVE_URL="${ATI_PUBLIC_ARCHIVE_URL:-https://github.com/winglight/algo-trader-ib/archive/refs/heads/main.tar.gz}"
INSTALL_DIR="${ATI_INSTALL_DIR:-$HOME/ati-local-runtime}"

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

dir_has_entries() {
  [ -d "$1" ] && [ -n "$(find "$1" -mindepth 1 -maxdepth 1 2>/dev/null | head -n 1)" ]
}

confirm_remove_install_dir() {
  local reason="$1" reply
  echo "$reason"
  echo "Install directory: $INSTALL_DIR"
  read -r -p "Delete all files in this directory and reinstall? [y/N]: " reply
  case "$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')" in
    y|yes)
      ;;
    *)
      echo "Installation aborted. Please move, commit, or back up the existing files, then rerun this installer." >&2
      exit 1
      ;;
  esac

  case "$INSTALL_DIR" in
    ""|"/"|"$HOME")
      echo "Refusing to remove unsafe install directory: $INSTALL_DIR" >&2
      exit 1
      ;;
  esac

  rm -rf "$INSTALL_DIR"
}

prompt_install_dir() {
  local value
  read -r -p "Install directory [${INSTALL_DIR}]: " value
  if [ -n "$value" ]; then
    INSTALL_DIR="$value"
  fi
}

download_with_git() {
  if [ -e "$INSTALL_DIR" ] && [ ! -d "$INSTALL_DIR" ]; then
    confirm_remove_install_dir "Install path already exists and is not a directory."
    git clone "$REPO_URL" "$INSTALL_DIR"
  elif [ -d "${INSTALL_DIR}/.git" ]; then
    if [ -n "$(git -C "$INSTALL_DIR" status --porcelain)" ]; then
      confirm_remove_install_dir "Existing installation has local changes that would block an update."
      git clone "$REPO_URL" "$INSTALL_DIR"
    else
      git -C "$INSTALL_DIR" pull --ff-only
    fi
  elif [ ! -e "$INSTALL_DIR" ] || ! dir_has_entries "$INSTALL_DIR"; then
    git clone "$REPO_URL" "$INSTALL_DIR"
  else
    confirm_remove_install_dir "Existing install directory is not empty and is not a Git checkout."
    git clone "$REPO_URL" "$INSTALL_DIR"
  fi
}

download_with_archive() {
  local tmp
  if [ -e "$INSTALL_DIR" ] && [ ! -d "$INSTALL_DIR" ]; then
    confirm_remove_install_dir "Install path already exists and is not a directory."
  elif dir_has_entries "$INSTALL_DIR"; then
    confirm_remove_install_dir "Existing install directory is not empty."
  fi
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
