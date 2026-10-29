#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

ARCHIVE_URL="${ATI_PUBLIC_ARCHIVE_URL:-https://github.com/winglight/algo-trader-ib/archive/refs/heads/main.zip}"
INSTALL_DIR="${ATI_INSTALL_DIR:-$HOME/ati-local-runtime}"
NON_INTERACTIVE=0
UPDATE_MODE=0
REQUESTED_COMPONENTS=""
expect_component=0
for arg in "$@"; do
  if [ "$expect_component" = 1 ]; then REQUESTED_COMPONENTS="$arg"; expect_component=0; continue; fi
  [ "$arg" != "--components" ] || expect_component=1
  [ "$arg" = "--non-interactive" ] && NON_INTERACTIVE=1
  [ "$arg" = "--update" ] && UPDATE_MODE=1
done
[ "$expect_component" = 0 ] || { echo 'Missing --components value.' >&2; exit 2; }
case "$REQUESTED_COMPONENTS" in ''|research|trading|all) ;; *) echo 'Invalid --components value.' >&2; exit 2 ;; esac

# Decide update scope from installed metadata before replacing any application
# files. A research-only operation must not stop or query trading services.
resolve_update_components() {
  local previous=''
  if [ -f "$INSTALL_DIR/.components.env" ]; then
    previous="$(sed -n 's/^ATI_INSTALLED_COMPONENTS=//p' "$INSTALL_DIR/.components.env")"
    case "$previous" in research|trading|all) ;; *) echo 'Invalid component metadata.' >&2; return 1 ;; esac
  elif [ -f "$INSTALL_DIR/.research.env" ] || [ -f "$INSTALL_DIR/.local-tools.env" ]; then
    previous=research
    [ ! -f "$INSTALL_DIR/.env" ] || previous=all
  elif [ -f "$INSTALL_DIR/.env" ]; then
    previous=trading
  elif [ "$UPDATE_MODE" = 1 ] && [ -f "$INSTALL_DIR/docker-compose.yml" ] && [ -z "$REQUESTED_COMPONENTS" ]; then
    echo "Installed component scope is unknown; select --components explicitly. No service was stopped." >&2
    return 1
  fi
  UPDATE_COMPONENTS="${REQUESTED_COMPONENTS:-${previous:-all}}"
}

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

run_install_fs() {
  # Renaming an ubuntu-owned installation does not require privilege even if
  # it contains root-owned files. Escalate only filesystem operations that do.
  "$@" || run_as_root "$@"
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
  validate_install_dir
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

}

validate_install_dir() {
  local parent canonical home_canonical
  case "$INSTALL_DIR" in ''|/|"$HOME")
    echo "Refusing unsafe install directory: $INSTALL_DIR" >&2; return 1 ;;
  esac
  [ ! -L "$INSTALL_DIR" ] || { echo "Refusing install directory symlink: $INSTALL_DIR" >&2; return 1; }
  parent="$(dirname "$INSTALL_DIR")"
  if [ ! -d "$parent" ]; then
    case "/$INSTALL_DIR/" in */../*|*/./*) echo "Refusing unresolved install directory: $INSTALL_DIR" >&2; return 1 ;; esac
    return 0
  fi
  canonical="$(cd "$parent" && pwd -P)/$(basename "$INSTALL_DIR")"
  if [ -d "$INSTALL_DIR" ]; then canonical="$(cd "$INSTALL_DIR" && pwd -P)"; fi
  home_canonical="$(cd "$HOME" && pwd -P)"
  case "$canonical" in /|"$home_canonical")
    echo "Refusing unsafe install directory: $INSTALL_DIR" >&2; return 1 ;;
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

backup_env_files() (
  local backup_dir="$1" manifest="$2" relative_dir directory source rel_path
  [ -d "$INSTALL_DIR" ] || return 0
  # Only the three supported configuration directories, never their subtrees.
  # Include hidden named env files such as .research.env without recursive find.
  shopt -s nullglob
  shopt -u dotglob
  for relative_dir in . middle config; do
    directory="$INSTALL_DIR"
    [ "$relative_dir" = . ] || directory="${INSTALL_DIR}/${relative_dir}"
    if [ -L "$directory" ]; then
      echo "Refusing to follow configuration directory symlink: $directory" >&2
      return 1
    fi
    [ -e "$directory" ] || continue
    if [ ! -d "$directory" ] || [ ! -r "$directory" ] || [ ! -x "$directory" ]; then
      echo "Cannot read configuration directory: $directory" >&2
      return 1
    fi
    for source in "$directory"/.env "$directory"/*.env "$directory"/.*.env; do
      if [ -L "$source" ]; then
        echo "Refusing to follow configuration symlink: $source" >&2
        return 1
      fi
      [ -e "$source" ] || continue
      if [ ! -f "$source" ]; then
        echo "Expected a configuration file: $source" >&2
        return 1
      fi
      rel_path="${source#"${INSTALL_DIR}/"}"
      mkdir -p "${backup_dir}/$(dirname "$rel_path")" || return 1
      cp "$source" "${backup_dir}/${rel_path}" || return 1
      printf '%s\n' "$rel_path" >>"$manifest" || return 1
    done
  done
)

restore_env_files() {
  local backup_dir="$1" manifest="$2" rel_path
  if [ ! -f "$manifest" ]; then
    return 0
  fi
  while IFS= read -r rel_path; do
    mkdir -p "${INSTALL_DIR}/$(dirname "$rel_path")" || return 1
    cp -p "${backup_dir}/${rel_path}" "${INSTALL_DIR}/${rel_path}" || return 1
  done <"$manifest"
}

capture_redis_volume_name() {
  local compose_file="${INSTALL_DIR}/middle/docker-compose.yml"
  local middle_env="${INSTALL_DIR}/middle/.env"
  local container_id mounted_name
  [ -f "$compose_file" ] || return 0
  if [ -f "$middle_env" ]; then
    container_id="$(docker compose --env-file "$middle_env" -f "$compose_file" ps -q redis 2>/dev/null || true)"
  else
    container_id="$(docker compose -f "$compose_file" ps -q redis 2>/dev/null || true)"
  fi
  [ -n "$container_id" ] || return 0
  mounted_name="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$container_id" 2>/dev/null || true)"
  [ -n "$mounted_name" ] || return 0
  case "$mounted_name" in
    *[!a-zA-Z0-9_.-]*)
      echo "Existing Redis volume name contains unsupported characters: ${mounted_name}" >&2
      return 1
      ;;
  esac
  export ATI_PRESERVED_REDIS_VOLUME_NAME="$mounted_name"
  echo "Preserving the mounted Redis identity volume: ${mounted_name}"
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
  local backup_dir="$1"
  local preserved_dir="${backup_dir}/strategies"
  [ -e "$preserved_dir" ] || return 0
  run_as_root rm -rf "${INSTALL_DIR:?}/strategies"
  mkdir -p "$INSTALL_DIR"
  run_as_root mv "$preserved_dir" "${INSTALL_DIR}/strategies"
}

merge_preserved_strategies() {
  local backup_dir="$1"
  local keep_source="${2:-0}"
  local preserved_dir="${backup_dir}/strategies"
  local release_dir="${INSTALL_DIR}/strategies"
  local backup_stamp conflict_dir source rel_path destination backup_destination
  local conflict_count=0
  [ -e "$preserved_dir" ] || return 0

  backup_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  conflict_dir="${INSTALL_DIR}/data/strategy-backups/update-${backup_stamp}"
  mkdir -p "$release_dir" || return 1

  while IFS= read -r -d '' source; do
    rel_path="${source#${preserved_dir}/}"
    case "$rel_path" in
      __pycache__/*|*/__pycache__/*|*.pyc)
        continue
        ;;
    esac
    destination="${release_dir}/${rel_path}"
    mkdir -p "$(dirname "$destination")" || return 1
    if [ ! -e "$destination" ]; then
      run_as_root cp -p "$source" "$destination" || return 1
      continue
    fi
    if cmp -s "$source" "$destination"; then
      continue
    fi
    backup_destination="${conflict_dir}/${rel_path}"
    mkdir -p "$(dirname "$backup_destination")" || return 1
    run_as_root cp -p "$source" "$backup_destination" || return 1
    conflict_count=$((conflict_count + 1))
  done < <(find "$preserved_dir" -type f -print0)

  [ "$keep_source" = 1 ] || run_as_root rm -rf "$preserved_dir" || return 1
  if [ "$conflict_count" -gt 0 ]; then
    echo "Refreshed official strategies; preserved ${conflict_count} replaced local file(s) in: ${conflict_dir}"
  fi
}

quiesce_runtime_for_update() {
  ATI_UPDATE_PREVIOUS_SERVICES=''
  ATI_UPDATE_PREVIOUS_IB_SERVICE=''
  [ "$UPDATE_MODE" = "1" ] || return 0
  [ "${UPDATE_COMPONENTS:-all}" != research ] || return 0
  if [ -f "${INSTALL_DIR}/docker-compose.yml" ]; then
    ATI_UPDATE_PREVIOUS_SERVICES="$(cd "$INSTALL_DIR" && docker compose -f docker-compose.yml ps --status running --services)" || return 1
    (cd "$INSTALL_DIR" && docker compose -f docker-compose.yml down) || return 1
  fi
  if [ -f "${INSTALL_DIR}/middle/docker-compose.yml" ]; then
    if [ -f "${INSTALL_DIR}/middle/.env" ]; then
      ATI_UPDATE_PREVIOUS_IB_SERVICE="$(docker compose --env-file "${INSTALL_DIR}/middle/.env" \
        -f "${INSTALL_DIR}/middle/docker-compose.yml" --profile ib ps -q --status running ib-gateway)" || return 1
      docker compose --env-file "${INSTALL_DIR}/middle/.env" \
        -f "${INSTALL_DIR}/middle/docker-compose.yml" \
        --profile ib stop ib-gateway || return 1
    else
      ATI_UPDATE_PREVIOUS_IB_SERVICE="$(docker compose -f "${INSTALL_DIR}/middle/docker-compose.yml" \
        --profile ib ps -q --status running ib-gateway)" || return 1
      docker compose \
        -f "${INSTALL_DIR}/middle/docker-compose.yml" \
        --profile ib stop ib-gateway || return 1
    fi
  fi
}

replace_install_dir_contents() {
  local extracted="$1" env_backup="$2" env_manifest="$3" runtime_backup="$4"
  local candidate previous rel_path
  validate_install_dir || return 1
  candidate="$(mktemp -d "${INSTALL_DIR}.update-candidate.XXXXXX")" || return 1
  previous="${runtime_backup}/previous-installation"
  # Prepare the complete release and configuration before stopping anything.
  if ! cp -R "${extracted}/." "$candidate/" ||
      ! (INSTALL_DIR="$candidate"; restore_env_files "$env_backup" "$env_manifest"); then
    run_install_fs rm -rf "$candidate"
    return 1
  fi
  if ! quiesce_runtime_for_update; then
    run_install_fs rm -rf "$candidate"
    resume_runtime_after_failed_update
    return 1
  fi
  mkdir -p "$runtime_backup"
  : >"${runtime_backup}/moved-paths"
  if [ -e "$INSTALL_DIR" ]; then
    if ! run_install_fs mv "$INSTALL_DIR" "$previous"; then
      run_install_fs rm -rf "$candidate"
      resume_runtime_after_failed_update
      return 1
    fi
  fi
  if ! run_install_fs mv "$candidate" "$INSTALL_DIR"; then
    [ ! -e "$previous" ] || run_install_fs mv "$previous" "$INSTALL_DIR"
    run_install_fs rm -rf "$candidate"
    resume_runtime_after_failed_update
    return 1
  fi
  for rel_path in data logs middle/data; do
    [ -e "${previous}/${rel_path}" ] || continue
    if ! printf '%s\n' "$rel_path" >>"${runtime_backup}/moved-paths"; then
      rollback_install_dir "$previous" "$runtime_backup"
      return 1
    fi
    if ! run_install_fs rm -rf "${INSTALL_DIR:?}/${rel_path}" ||
        ! mkdir -p "${INSTALL_DIR}/$(dirname "$rel_path")" ||
        ! run_install_fs mv "${previous}/${rel_path}" "${INSTALL_DIR}/${rel_path}"; then
      rollback_install_dir "$previous" "$runtime_backup"
      return 1
    fi
  done
  if ! merge_preserved_strategies "$previous" 1; then
    rollback_install_dir "$previous" "$runtime_backup"
    return 1
  fi
  # Only the obsolete tree is removed with privilege. Cleanup failure must not
  # destroy a complete new installation or prevent the normal startup path.
  if ! run_install_fs rm -rf "$previous"; then
    echo "Updated installation prepared; retained old files for manual cleanup: $previous" >&2
  fi
  rm -f "${runtime_backup}/moved-paths"
}

rollback_install_dir() {
  local previous="$1" runtime_backup="$2" rel_path
  while IFS= read -r rel_path; do
    [ ! -e "${previous}/${rel_path}" ] || continue
    [ -e "${INSTALL_DIR}/${rel_path}" ] || continue
    mkdir -p "${previous}/$(dirname "$rel_path")" || return 1
    run_install_fs mv "${INSTALL_DIR}/${rel_path}" "${previous}/${rel_path}" || return 1
  done <"${runtime_backup}/moved-paths"
  run_install_fs rm -rf "${INSTALL_DIR:?}" || return 1
  run_install_fs mv "$previous" "$INSTALL_DIR" || return 1
  rm -f "${runtime_backup}/moved-paths"
  resume_runtime_after_failed_update
}

resume_runtime_after_failed_update() {
  local service
  [ "$UPDATE_MODE" = 1 ] || return 0
  [ "${UPDATE_COMPONENTS:-all}" != research ] || return 0
  if [ -n "${ATI_UPDATE_PREVIOUS_IB_SERVICE:-}" ]; then
    if [ -f "${INSTALL_DIR}/middle/.env" ]; then
      docker compose --env-file "${INSTALL_DIR}/middle/.env" -f "${INSTALL_DIR}/middle/docker-compose.yml" \
        --profile ib up -d --no-build --pull never ib-gateway || echo 'Could not restore the previous IB Gateway service.' >&2
    else
      docker compose -f "${INSTALL_DIR}/middle/docker-compose.yml" \
        --profile ib up -d --no-build --pull never ib-gateway || echo 'Could not restore the previous IB Gateway service.' >&2
    fi
  fi
  if [ -n "${ATI_UPDATE_PREVIOUS_SERVICES:-}" ]; then
    local -a services=()
    while IFS= read -r service; do [ -z "$service" ] || services+=("$service"); done <<<"$ATI_UPDATE_PREVIOUS_SERVICES"
    (cd "$INSTALL_DIR" && docker compose -f docker-compose.yml up -d --no-build --pull never "${services[@]}") || echo 'Could not restore all previous runtime services.' >&2
  fi
}

download_with_zip() {
  local tmp env_backup env_manifest runtime_backup extracted archive_url separator
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
  capture_redis_volume_name
  archive_url="$ARCHIVE_URL"
  case "$archive_url" in
    https://github.com/*/archive/refs/heads/*.zip*)
      case "$archive_url" in *\?*) separator='&' ;; *) separator='?' ;; esac
      archive_url="${archive_url}${separator}ati_cache_bust=$(date -u +%s)"
      ;;
  esac
  curl -fsSL "$archive_url" -o "${tmp}/ati-local-runtime.zip"
  unzip -q "${tmp}/ati-local-runtime.zip" -d "$tmp"
  extracted="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d -name 'algo-trader-ib-*' | head -n 1)"
  if [ -z "$extracted" ]; then
    echo "Unable to locate downloaded installer files." >&2
    exit 1
  fi
  if [ "$UPDATE_MODE" = "1" ] && [ "$UPDATE_COMPONENTS" != research ]; then
    # Prefer the installed guard so an older downloaded archive cannot remove
    # its own downgrade check before the current protection is stopped.
    guard_dir="${INSTALL_DIR}/scripts"
    if [ ! -f "${guard_dir}/options_upgrade_guard.sh" ]; then
      guard_dir="${extracted}/scripts"
    fi
    if [ -f "${guard_dir}/options_upgrade_guard.sh" ]; then
      bash "${guard_dir}/options_upgrade_guard.sh" "$INSTALL_DIR" "${extracted}/algo_trader.sql"
    else
      echo "OPTIONS_DOWNGRADE_FACTS_UNAVAILABLE: this update has no compatible downgrade guard; current runtime was preserved." >&2
      exit 1
    fi
  fi
  replace_install_dir_contents "$extracted" "$env_backup" "$env_manifest" "$runtime_backup"
  run_install_fs find "$runtime_backup" -depth -type d -empty -delete || echo "Retained update backup: $runtime_backup" >&2
  if [ -e "$runtime_backup" ]; then
    echo "Retained previous installation files: $runtime_backup" >&2
  fi
  rm -rf "$tmp"
}

prompt_install_dir
validate_install_dir
resolve_update_components
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


