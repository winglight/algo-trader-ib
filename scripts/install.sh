#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

ARCHIVE_URL="${ATI_PUBLIC_ARCHIVE_URL:-https://github.com/winglight/algo-trader-ib/archive/refs/heads/main.zip}"
INSTALL_DIR="${ATI_INSTALL_DIR:-$HOME/ati-local-runtime}"
NON_INTERACTIVE=0
UPDATE_MODE=0
for arg in "$@"; do
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
    echo "This installer operation requires root privileges or sudo." >&2
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
  read -r -p "Download the latest public release and keep local configuration? [Y/n]: " reply
  case "$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')" in
    ""|y|yes)
      ;;
    *)
      echo "Update cancelled; the existing installation was not replaced." >&2
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

preserve_runtime_paths() {
  local backup_dir="$1" rel_path
  [ -d "$INSTALL_DIR" ] || return 0
  for rel_path in data logs strategies middle/data; do
    if [ -e "${INSTALL_DIR}/${rel_path}" ]; then
      mkdir -p "${backup_dir}/$(dirname "$rel_path")"
      run_as_root mv "${INSTALL_DIR}/${rel_path}" "${backup_dir}/${rel_path}"
    fi
  done
}

restore_runtime_paths() {
  local backup_dir="$1" rel_path
  for rel_path in data logs middle/data; do
    if [ -e "${backup_dir}/${rel_path}" ]; then
      run_as_root rm -rf "${INSTALL_DIR:?}/${rel_path}"
      mkdir -p "${INSTALL_DIR}/$(dirname "$rel_path")"
      run_as_root mv "${backup_dir}/${rel_path}" "${INSTALL_DIR}/${rel_path}"
    fi
  done
}

restore_preserved_strategies_exact() {
  local backup_dir="$1" preserved_dir="${backup_dir}/strategies"
  [ -e "$preserved_dir" ] || return 0
  run_as_root rm -rf "${INSTALL_DIR:?}/strategies"
  mkdir -p "$INSTALL_DIR"
  run_as_root mv "$preserved_dir" "${INSTALL_DIR}/strategies"
}

merge_preserved_strategies() {
  local backup_dir="$1"
  local preserved_dir="${backup_dir}/strategies"
  local release_dir="${INSTALL_DIR}/strategies"
  local backup_stamp conflict_dir source rel_path destination backup_destination
  local conflict_count=0
  [ -e "$preserved_dir" ] || return 0

  backup_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  conflict_dir="${INSTALL_DIR}/data/strategy-backups/update-${backup_stamp}"
  mkdir -p "$release_dir"

  while IFS= read -r -d '' source; do
    rel_path="${source#${preserved_dir}/}"
    case "$rel_path" in
      __pycache__/*|*/__pycache__/*|*.pyc)
        continue
        ;;
    esac
    destination="${release_dir}/${rel_path}"
    mkdir -p "$(dirname "$destination")"
    if [ ! -e "$destination" ]; then
      run_as_root cp -p "$source" "$destination"
      continue
    fi
    if cmp -s "$source" "$destination"; then
      continue
    fi
    backup_destination="${conflict_dir}/${rel_path}"
    mkdir -p "$(dirname "$backup_destination")"
    run_as_root cp -p "$source" "$backup_destination"
    conflict_count=$((conflict_count + 1))
  done < <(find "$preserved_dir" -type f -print0)

  run_as_root rm -rf "$preserved_dir"
  if [ "$conflict_count" -gt 0 ]; then
    echo "Refreshed official strategies; preserved ${conflict_count} replaced local file(s) in: ${conflict_dir}"
  fi
}

quiesce_runtime_for_update() {
  [ "$UPDATE_MODE" = "1" ] || return 0
  if [ -f "${INSTALL_DIR}/docker-compose.yml" ]; then
    (cd "$INSTALL_DIR" && docker compose -f docker-compose.yml down)
  fi
  if [ -f "${INSTALL_DIR}/middle/docker-compose.yml" ]; then
    if [ -f "${INSTALL_DIR}/middle/.env" ]; then
      docker compose --env-file "${INSTALL_DIR}/middle/.env" \
        -f "${INSTALL_DIR}/middle/docker-compose.yml" \
        --profile ib stop ib-gateway
    else
      docker compose \
        -f "${INSTALL_DIR}/middle/docker-compose.yml" \
        --profile ib stop ib-gateway
    fi
  fi
}

replace_install_dir_contents() {
  local extracted="$1" env_backup="$2" env_manifest="$3" runtime_backup="$4"
  if [ -d "$INSTALL_DIR" ]; then
    preserve_runtime_paths "$runtime_backup"
    if ! find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +; then
      restore_runtime_paths "$runtime_backup"
      restore_preserved_strategies_exact "$runtime_backup"
      return 1
    fi
  elif [ -e "$INSTALL_DIR" ]; then
    rm -f "$INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"
  else
    mkdir -p "$INSTALL_DIR"
  fi
  if ! cp -R "${extracted}/." "$INSTALL_DIR/"; then
    restore_runtime_paths "$runtime_backup"
    restore_preserved_strategies_exact "$runtime_backup"
    return 1
  fi
  restore_runtime_paths "$runtime_backup"
  merge_preserved_strategies "$runtime_backup"
  restore_env_files "$env_backup" "$env_manifest"
}

download_with_zip() {
  local tmp env_backup env_manifest runtime_backup extracted
  if [ "$UPDATE_MODE" = "1" ] && ! dir_has_entries "$INSTALL_DIR"; then
    echo "Update mode requires an existing installation: $INSTALL_DIR" >&2
    exit 1
  fi
  if [ -e "$INSTALL_DIR" ] && [ ! -d "$INSTALL_DIR" ]; then
    confirm_update_install_dir "Install path already exists and is not a directory."
  elif dir_has_entries "$INSTALL_DIR"; then
    [ "$UPDATE_MODE" = "1" ] || {
      echo "Existing installation detected." >&2
      echo "Rerun the one-line installer with this suffix appended: installer --update" >&2
      echo 'bash -c "$(curl -fsSL https://raw.githubusercontent.com/winglight/algo-trader-ib/main/scripts/install.sh)" installer --update' >&2
      exit 1
    }
    confirm_update_install_dir "Existing installation detected; update mode will replace application files."
  fi
  tmp="$(mktemp -d)"
  env_backup="${tmp}/env-backup"
  env_manifest="${tmp}/env-files.txt"
  runtime_backup="$(mktemp -d "${INSTALL_DIR}.runtime-preserve.XXXXXX")"
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
  quiesce_runtime_for_update
  replace_install_dir_contents "$extracted" "$env_backup" "$env_manifest" "$runtime_backup"
  find "$runtime_backup" -depth -type d -empty -delete
  if [ -e "$runtime_backup" ]; then
    echo "Runtime preservation directory is unexpectedly non-empty: $runtime_backup" >&2
    exit 1
  fi
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
exec bash ./setup_and_run.sh "$@"
