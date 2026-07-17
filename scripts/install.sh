#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

ARCHIVE_URL="${ATI_PUBLIC_ARCHIVE_URL:-https://github.com/winglight/algo-trader-ib/archive/refs/heads/main.zip}"
INSTALL_DIR="${ATI_INSTALL_DIR:-$HOME/ati-local-runtime}"
SETUP_ARGS=("$@")
NON_INTERACTIVE=0
UPDATE_MODE=0
for arg in "${SETUP_ARGS[@]}"; do
  [ "$arg" = "--non-interactive" ] && NON_INTERACTIVE=1
  [ "$arg" = "--update" ] && UPDATE_MODE=1
done

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

run_as_root() {
  if [ "$(id -u)" = "0" ]; then
    "$@"
  elif has_cmd sudo; then
    sudo "$@"
  else
    echo "Installing unzip requires root privileges or sudo." >&2
    return 1
  fi
}

ensure_unzip() {
  has_cmd unzip && return 0
  echo "unzip is not installed; installing it now..."
  if has_cmd apt-get; then
    run_as_root apt-get update
    run_as_root apt-get install -y unzip
  elif has_cmd dnf; then
    run_as_root dnf install -y unzip
  elif has_cmd yum; then
    run_as_root yum install -y unzip
  elif has_cmd apk; then
    run_as_root apk add --no-cache unzip
  else
    echo "Unable to install unzip automatically: no supported package manager found." >&2
    return 1
  fi
  has_cmd unzip || {
    echo "unzip installation did not provide the unzip command." >&2
    return 1
  }
}

dir_has_entries() {
  [ -d "$1" ] && [ -n "$(find "$1" -mindepth 1 -maxdepth 1 2>/dev/null | head -n 1)" ]
}

confirm_update_install_dir() {
  local reason="$1" reply
  echo "$reason"
  echo "Install directory: $INSTALL_DIR"
  if [ "$NON_INTERACTIVE" = "1" ]; then
    [ "${ATI_ALLOW_UPDATE:-0}" = "1" ] || {
      echo "Non-interactive update requires ATI_ALLOW_UPDATE=1." >&2
      exit 1
    }
    return 0
  fi
  read -r -p "Download the latest public release and keep local configuration? [y/N]: " reply
  case "$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')" in
    y|yes)
      ;;
    *)
      echo "Installation aborted. Please move or back up the existing files, then rerun this installer." >&2
      exit 1
      ;;
  esac

  case "$INSTALL_DIR" in
    ""|"/"|"$HOME")
      echo "Refusing to remove unsafe install directory: $INSTALL_DIR" >&2
      exit 1
      ;;
  esac
}

prompt_install_dir() {
  local value
  [ "$NON_INTERACTIVE" = "1" ] && return 0
  read -r -p "Install directory [${INSTALL_DIR}]: " value
  if [ -n "$value" ]; then
    INSTALL_DIR="$value"
  fi
}

backup_env_files() {
  local backup_dir="$1" manifest="$2"
  if [ ! -d "$INSTALL_DIR" ]; then
    return 0
  fi
  (
    cd "$INSTALL_DIR"
    find . -type f \( -name '.env' -o -name '*.env' \) -print
  ) | while IFS= read -r rel_path; do
    rel_path="${rel_path#./}"
    mkdir -p "${backup_dir}/$(dirname "$rel_path")"
    cp "${INSTALL_DIR}/${rel_path}" "${backup_dir}/${rel_path}"
    printf '%s\n' "$rel_path" >>"$manifest"
  done
}

restore_env_files() {
  local backup_dir="$1" manifest="$2" rel_path
  if [ ! -f "$manifest" ]; then
    return 0
  fi
  while IFS= read -r rel_path; do
    mkdir -p "${INSTALL_DIR}/$(dirname "$rel_path")"
    cp "${backup_dir}/${rel_path}" "${INSTALL_DIR}/${rel_path}"
  done <"$manifest"
}

replace_install_dir_contents() {
  local extracted="$1" env_backup="$2" env_manifest="$3"
  if [ -d "$INSTALL_DIR" ]; then
    find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  elif [ -e "$INSTALL_DIR" ]; then
    rm -f "$INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"
  else
    mkdir -p "$INSTALL_DIR"
  fi
  cp -R "${extracted}/." "$INSTALL_DIR/"
  restore_env_files "$env_backup" "$env_manifest"
}

download_with_zip() {
  local tmp env_backup env_manifest extracted
  if [ "$UPDATE_MODE" = "1" ] && ! dir_has_entries "$INSTALL_DIR"; then
    echo "Update mode requires an existing installation: $INSTALL_DIR" >&2
    exit 1
  fi
  if [ -e "$INSTALL_DIR" ] && [ ! -d "$INSTALL_DIR" ]; then
    confirm_update_install_dir "Install path already exists and is not a directory."
  elif dir_has_entries "$INSTALL_DIR"; then
    [ "$UPDATE_MODE" = "1" ] || {
      echo "Existing installation detected. Rerun with --update." >&2
      exit 1
    }
    confirm_update_install_dir "Existing installation detected; update mode will replace application files."
  fi
  tmp="$(mktemp -d)"
  env_backup="${tmp}/env-backup"
  env_manifest="${tmp}/env-files.txt"
  mkdir -p "$env_backup"
  touch "$env_manifest"
  backup_env_files "$env_backup" "$env_manifest"
  curl -fsSL "$ARCHIVE_URL" -o "${tmp}/ati-local-runtime.zip"
  unzip -q "${tmp}/ati-local-runtime.zip" -d "$tmp"
  extracted="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d -name 'algo-trader-ib-*' | head -n 1)"
  if [ -z "$extracted" ]; then
    echo "Unable to locate downloaded installer files." >&2
    exit 1
  fi
  replace_install_dir_contents "$extracted" "$env_backup" "$env_manifest"
  rm -rf "$tmp"
}

prompt_install_dir
mkdir -p "$(dirname "$INSTALL_DIR")"

if ! has_cmd curl; then
  echo "This installer needs curl." >&2
  exit 1
fi
ensure_unzip
download_with_zip

chmod +x "${INSTALL_DIR}/setup_and_run.sh" "${INSTALL_DIR}/scripts/install_docker.sh" || true
cd "$INSTALL_DIR"
exec bash ./setup_and_run.sh "${SETUP_ARGS[@]}"
