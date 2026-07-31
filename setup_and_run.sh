#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIDDLE_DIR="${ROOT_DIR}/middle"
APP_URL="${ATI_APP_URL:-}"
ADAPTERS_COMMIT="69aaa90750f92e0d7d05c8eba21783fe57d5d681"
ADAPTERS_ARCHIVE_SHA256="e9481d3a411e5907d51204beeb85426cfb758c4587fc894c8661f8979d6b174e"
ADAPTERS_ARCHIVE_URL="https://github.com/winglight/algo-trader-broker-adapters/archive/${ADAPTERS_COMMIT}.tar.gz"
# shellcheck source=scripts/installer_lib.sh
source "${ROOT_DIR}/scripts/installer_lib.sh"

NON_INTERACTIVE=0
DRY_RUN=0
UPDATE_MODE=0
ENABLED_ADAPTERS=""
INITIAL_ADAPTER=""
ALPACA_DATA_FEED=""
REDIS_PASSWORD_FILE="${ATI_REDIS_PASSWORD_FILE:-}"
MARIADB_PASSWORD_FILE="${ATI_MARIADB_PASSWORD_FILE:-}"
ADMIN_PASSWORD_FILE="${ATI_ADMIN_PASSWORD_FILE:-}"
IBKR_USERNAME_FILE="${ATI_IBKR_USERNAME_FILE:-}"
IBKR_PASSWORD_FILE="${ATI_IBKR_PASSWORD_FILE:-}"
IBKR_VNC_PASSWORD_FILE="${ATI_IBKR_VNC_PASSWORD_FILE:-}"
ALPACA_API_KEY_ID_FILE="${ATI_ALPACA_API_KEY_ID_FILE:-}"
ALPACA_SECRET_KEY_FILE="${ATI_ALPACA_SECRET_KEY_FILE:-}"
PREPARED_PLUGIN_DIR=""
PREPARED_PLUGIN_BUILD_DIR=""
PLUGIN_BACKUP_DIR=""
PLUGIN_ACTIVATED=0

usage() {
  cat <<'EOF'
Usage: setup_and_run.sh [options]

  --non-interactive
  --update
  --enabled-adapters sim[,ibkr_paper][,alpaca_paper]
  --initial-adapter sim|ibkr_paper|alpaca_paper
  --alpaca-data-feed iex|sip
  --redis-password-file PATH
  --mariadb-password-file PATH
  --admin-password-file PATH
  --ibkr-username-file PATH
  --ibkr-password-file PATH
  --ibkr-vnc-password-file PATH
  --alpaca-api-key-id-file PATH
  --alpaca-secret-key-file PATH
  --dry-run

Secret values are accepted only through prompts or permission-controlled files.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --update) UPDATE_MODE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --enabled-adapters) [ "$#" -ge 2 ] || { echo "Missing value for $1" >&2; exit 2; }; ENABLED_ADAPTERS="$2"; shift 2 ;;
    --initial-adapter) [ "$#" -ge 2 ] || { echo "Missing value for $1" >&2; exit 2; }; INITIAL_ADAPTER="$2"; shift 2 ;;
    --alpaca-data-feed) [ "$#" -ge 2 ] || { echo "Missing value for $1" >&2; exit 2; }; ALPACA_DATA_FEED="$2"; shift 2 ;;
    --redis-password-file) REDIS_PASSWORD_FILE="$2"; shift 2 ;;
    --mariadb-password-file) MARIADB_PASSWORD_FILE="$2"; shift 2 ;;
    --admin-password-file) ADMIN_PASSWORD_FILE="$2"; shift 2 ;;
    --ibkr-username-file) IBKR_USERNAME_FILE="$2"; shift 2 ;;
    --ibkr-password-file) IBKR_PASSWORD_FILE="$2"; shift 2 ;;
    --ibkr-vnc-password-file) IBKR_VNC_PASSWORD_FILE="$2"; shift 2 ;;
    --alpaca-api-key-id-file) ALPACA_API_KEY_ID_FILE="$2"; shift 2 ;;
    --alpaca-secret-key-file) ALPACA_SECRET_KEY_FILE="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    --alpaca-secret-key|--alpaca-api-key-id|--ibkr-password|--ibkr-username)
      echo "Plaintext credential arguments are forbidden; use the corresponding --*-file option." >&2
      exit 2
      ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

has_cmd() { command -v "$1" >/dev/null 2>&1; }

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

ensure_docker() {
  if has_cmd docker && docker compose version >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    return 0
  fi
  echo "Docker is not ready. Running public/scripts/install_docker.sh ..."
  bash "${ROOT_DIR}/scripts/install_docker.sh"
  docker compose version >/dev/null 2>&1 && docker info >/dev/null 2>&1 || {
    echo "Docker is installed but not ready." >&2
    return 1
  }
}

ensure_ib_gateway_settings_permissions() {
  local settings_dir="${MIDDLE_DIR}/data/ib-gateway/tws_settings"
  mkdir -p "$settings_dir"
  if [ "$(stat -c '%u:%g' "$settings_dir")" = "1000:1000" ]; then
    return 0
  fi
  echo "Setting IB Gateway settings ownership to 1000:1000 ..."
  if [ "$(id -u)" = "0" ]; then
    chown -R 1000:1000 "$settings_dir"
  elif has_cmd sudo; then
    sudo chown -R 1000:1000 "$settings_dir"
  else
    echo "IB Gateway settings require ownership 1000:1000; rerun as root or install sudo." >&2
    return 1
  fi
}

current_or_example() {
  local real="$1" example="$2" key="$3" value
  value="$(read_env_value "$real" "$key")"
  if [ -n "$value" ]; then printf '%s' "$value"; else read_env_value "$example" "$key"; fi
}

legacy_enabled_adapters() {
  local mode
  mode="$(read_env_value "${ROOT_DIR}/.env" BROKER_ADAPTER_MODE)"
  case "$mode" in
    ib) printf 'sim,ibkr_paper' ;;
    *) printf 'sim' ;;
  esac
}

legacy_initial_adapter() {
  local mode
  mode="$(read_env_value "${ROOT_DIR}/.env" BROKER_ADAPTER_MODE)"
  case "$mode" in ib) printf 'ibkr_paper' ;; *) printf 'sim' ;; esac
}

choose_interactive_adapters() {
  local existing choice default_index=1
  existing="$(read_env_value "${ROOT_DIR}/.env" BROKER_RUNNER_ENABLED_ADAPTERS)"
  [ -n "$existing" ] || existing="$(legacy_enabled_adapters)"
  case "$existing" in
    sim) default_index=1 ;;
    sim,ibkr_paper) default_index=2 ;;
    sim,alpaca_paper) default_index=3 ;;
    sim,ibkr_paper,alpaca_paper) default_index=4 ;;
  esac
  echo "Select an adapter configuration (Sim is always enabled):"
  echo "  1) Sim"
  echo "  2) Sim + IBKR Paper"
  echo "  3) Sim + Alpaca Paper"
  echo "  4) Sim + IBKR Paper + Alpaca Paper"
  while true; do
    choice="$(prompt_value "Selection" "$default_index" 0)"
    case "$choice" in
      1) ENABLED_ADAPTERS=sim; return ;;
      2) ENABLED_ADAPTERS=sim,ibkr_paper; return ;;
      3) ENABLED_ADAPTERS=sim,alpaca_paper; return ;;
      4) ENABLED_ADAPTERS=sim,ibkr_paper,alpaca_paper; return ;;
      *) echo "Choose a listed number." >&2 ;;
    esac
  done
}

choose_interactive_initial() {
  local choices=(sim) choice default_index=1 index=1 existing
  contains_profile "$ENABLED_ADAPTERS" ibkr_paper && choices+=(ibkr_paper)
  contains_profile "$ENABLED_ADAPTERS" alpaca_paper && choices+=(alpaca_paper)
  if [ "${#choices[@]}" -eq 1 ]; then
    INITIAL_ADAPTER=sim
    echo "Initial adapter: Sim (the only enabled adapter)."
    return
  fi
  existing="$(read_env_value "${ROOT_DIR}/.env" BROKER_RUNNER_DEFAULT_ADAPTER_ID)"
  [ -n "$existing" ] || existing="$(legacy_initial_adapter)"
  echo "Choose initial adapter:"
  for choice in "${choices[@]}"; do
    [ "$choice" = "$existing" ] && default_index="$index"
    case "$choice" in
      sim) echo "  ${index}) Sim Adapter" ;;
      ibkr_paper) echo "  ${index}) IBKR Paper Adapter" ;;
      alpaca_paper) echo "  ${index}) Alpaca Paper Adapter" ;;
    esac
    index=$((index + 1))
  done
  while true; do
    choice="$(prompt_value "Selection" "$default_index" 0)"
    case "$choice" in
      ''|*[!0-9]*) echo "Choose a listed number." >&2 ;;
      *)
        if [ "$choice" -ge 1 ] && [ "$choice" -le "${#choices[@]}" ]; then
          INITIAL_ADAPTER="${choices[$((choice - 1))]}"
          return
        fi
        echo "Choose a listed number." >&2
        ;;
    esac
  done
}

resolve_secret() {
  local current="$1" file="$2" label="$3" prompt="$4"
  if [ "$NON_INTERACTIVE" = "1" ]; then
    configured_existing_or_file "$current" "$file" "$label"
  elif [ -n "$file" ]; then
    read_secret_file "$file" "$label"
  else
    prompt_value "$prompt" "$current" 1
  fi
}

generate_secret() {
  od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
}

resolve_managed_secret() {
  local env_file="$1" key="$2" file="$3" label="$4" existing
  if [ -n "$file" ]; then
    read_secret_file "$file" "$label"
  else
    existing="$(read_env_value "$env_file" "$key")"
    if [ -n "$existing" ]; then
      printf '%s' "$existing"
    else
      generate_secret
    fi
  fi
}

sha256_file() {
  if has_cmd sha256sum; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi
}

prepare_selected_adapter_plugins() {
  local broker_runner_image="$1" archive extract_root wheelhouse lock_file runtime_arch
  local line_arch package version filename checksum url target actual selected_count
  PREPARED_PLUGIN_DIR="$(mktemp -d "${ROOT_DIR}/.broker-plugins.candidate.XXXXXX")"
  if [ "$ENABLED_ADAPTERS" = "sim" ]; then
    return 0
  fi

  PREPARED_PLUGIN_BUILD_DIR="$(mktemp -d "${ROOT_DIR}/.adapter-plugin-build.XXXXXX")"
  archive="${PREPARED_PLUGIN_BUILD_DIR}/adapters.tar.gz"
  extract_root="${PREPARED_PLUGIN_BUILD_DIR}/source"
  wheelhouse="${PREPARED_PLUGIN_BUILD_DIR}/wheelhouse"
  lock_file="${ROOT_DIR}/docker/alpaca-runtime-wheels.lock"

  curl -fsSL --retry 3 --retry-all-errors --connect-timeout 20 "$ADAPTERS_ARCHIVE_URL" -o "$archive"
  actual="$(sha256_file "$archive")"
  [ "$actual" = "$ADAPTERS_ARCHIVE_SHA256" ] || {
    echo "Adapter source checksum verification failed: expected ${ADAPTERS_ARCHIVE_SHA256}, got ${actual}" >&2
    return 1
  }
  mkdir -p "$extract_root"
  tar -xzf "$archive" --strip-components=1 -C "$extract_root"

  if contains_profile "$ENABLED_ADAPTERS" alpaca_paper; then
    runtime_arch="$(docker info --format '{{.Architecture}}')"
    case "$runtime_arch" in
      amd64|x86_64) runtime_arch=amd64 ;;
      arm64|aarch64) runtime_arch=arm64 ;;
      *) echo "Unsupported Docker architecture for Alpaca Adapter: ${runtime_arch:-unknown}" >&2; return 1 ;;
    esac
    [ -f "$lock_file" ] || { echo "Alpaca runtime wheel lock is missing." >&2; return 1; }
    mkdir -p "$wheelhouse"
    : >"${wheelhouse}/SHA256SUMS"
    selected_count=0
    while IFS='|' read -r line_arch package version filename checksum url; do
      case "$line_arch" in ''|'#'*) continue ;; esac
      [ "$line_arch" = "any" ] || [ "$line_arch" = "$runtime_arch" ] || continue
      target="${wheelhouse}/${filename}"
      echo "Downloading locked Adapter dependency: ${package}==${version} (${line_arch})"
      curl -fsSL --retry 3 --retry-all-errors --connect-timeout 20 "$url" -o "$target"
      actual="$(sha256_file "$target")"
      [ "$actual" = "$checksum" ] || {
        echo "Wheel checksum verification failed for ${package}==${version}." >&2
        return 1
      }
      printf '%s  %s\n' "$checksum" "$filename" >>"${wheelhouse}/SHA256SUMS"
      selected_count=$((selected_count + 1))
    done <"$lock_file"
    [ "$selected_count" -eq 7 ] || {
      echo "Alpaca Adapter dependency lock must select exactly seven artifacts for ${runtime_arch}." >&2
      return 1
    }
  fi

  if contains_profile "$ENABLED_ADAPTERS" ibkr_paper; then
    echo "Installing selected Adapter plugin: ibkr_paper"
    docker run --rm --pull=never --user "$(id -u):$(id -g)" -e HOME=/tmp \
      -v "${PREPARED_PLUGIN_BUILD_DIR}:/plugin-build" \
      -v "${PREPARED_PLUGIN_DIR}:/plugins" \
      --entrypoint python "$broker_runner_image" -m pip install \
      --target /plugins --no-cache-dir --no-index --no-deps --no-build-isolation \
      /plugin-build/source/packages/ibkr-paper
  fi
  if contains_profile "$ENABLED_ADAPTERS" alpaca_paper; then
    echo "Installing selected Adapter plugin: alpaca_paper"
    docker run --rm --pull=never --user "$(id -u):$(id -g)" -e HOME=/tmp \
      -v "${PREPARED_PLUGIN_BUILD_DIR}:/plugin-build" \
      -v "${PREPARED_PLUGIN_DIR}:/plugins" \
      --entrypoint sh "$broker_runner_image" -lc \
      'cd /plugin-build/wheelhouse && sha256sum -c SHA256SUMS && python -m pip install --target /plugins --no-cache-dir --no-index --no-deps ./*.whl && python -m pip install --target /plugins --no-cache-dir --no-index --no-deps --no-build-isolation /plugin-build/source/packages/alpaca-paper'
    mkdir -p "${PREPARED_PLUGIN_DIR}/.metadata"
    cp "${ROOT_DIR}/docker/alpaca-paper.spdx.json" "${PREPARED_PLUGIN_DIR}/.metadata/alpaca-paper.spdx.json"
  fi
}

validate_candidates() {
  local root_env="$1" middle_env="$2" broker_runner_image="$3" plugin_dir="$4"
  validate_enabled_adapters "$ENABLED_ADAPTERS"
  validate_initial_adapter "$ENABLED_ADAPTERS" "$INITIAL_ADAPTER"
  case "$ALPACA_DATA_FEED" in iex|sip) ;; *) echo "Alpaca data feed must be iex or sip." >&2; return 1 ;; esac
  docker compose --env-file "$middle_env" -f "${MIDDLE_DIR}/docker-compose.yml" config -q
  docker compose --env-file "$root_env" -f "${ROOT_DIR}/docker-compose.yml" config -q
  docker run --rm --pull=never --env-file "$root_env" \
    -e PYTHONPATH=/plugins:/app/packages/ati-shared-sdk/src:/app/src \
    -v "${plugin_dir}:/plugins:ro" \
    --entrypoint python "$broker_runner_image" -c \
    'from importlib import metadata; from src.broker_runner.settings import BrokerRunnerSettings; from src.broker_runner.profile_registry import AdapterProfileRegistry, ENTRY_POINT_GROUP; import os; s=BrokerRunnerSettings.from_env(); [s.profile_settings(p, os.environ) for p in s.enabled_adapter_ids]; entries={e.name:e for e in metadata.entry_points().select(group=ENTRY_POINT_GROUP)}; selected=[p for p in s.enabled_adapter_ids if p != "sim"]; missing_entries=[p for p in selected if p not in entries]; assert not missing_entries, f"Selected Adapter plugin entry points were not installed: {missing_entries}"; [entries[p].load() for p in selected]; r=AdapterProfileRegistry(s.enabled_adapter_ids, os.environ); missing=[p for p in s.enabled_adapter_ids if not r.state(p).installed]; assert not missing, f"Selected Adapter plugins were not installed: {missing}"'
}

activate_prepared_plugins() {
  local plugin_dir="${ROOT_DIR}/data/broker-plugins"
  PLUGIN_BACKUP_DIR="${ROOT_DIR}/data/.broker-plugins.previous.$$"
  run_as_root mkdir -p "${ROOT_DIR}/data"
  if [ -e "$plugin_dir" ]; then
    run_as_root mv "$plugin_dir" "$PLUGIN_BACKUP_DIR"
  fi
  PLUGIN_ACTIVATED=1
  run_as_root mv "$PREPARED_PLUGIN_DIR" "$plugin_dir"
  PREPARED_PLUGIN_DIR=""
}

validate_public_image_reference() {
  local image="$1" repository="$2"
  case "$image" in
    "ghcr.io/winglight/algo-trader/${repository}:"*|"ghcr.io/winglight/algo-trader/${repository}@sha256:"*)
      ;;
    *)
      echo "Public ${repository} image must come from ghcr.io/winglight/algo-trader: ${image}" >&2
      return 1
      ;;
  esac
}

wait_for_http() {
  local url="$1" tries="${2:-90}"
  while [ "$tries" -gt 0 ]; do
    curl -fsS "$url" >/dev/null 2>&1 && return 0
    sleep 2
    tries=$((tries - 1))
  done
  return 1
}

open_browser() {
  if has_cmd open; then open "$APP_URL" >/dev/null 2>&1 || true
  elif has_cmd xdg-open; then xdg-open "$APP_URL" >/dev/null 2>&1 || true
  fi
}

pull_application_images() {
  local services=(
    backend
    account-service
    orders-service
    market-data-service
    risk-service
    simulation-service
    strategy-spec-service
    strategy-service
    service-watchdog
    frontend
  )
  services+=(broker-runner-service)
  (cd "$ROOT_DIR" && docker compose -f docker-compose.yml pull "${services[@]}")
}

backup_database_for_update() {
  local backup_root backup_dir dump_tmp dump_file compose_file middle_env container_id
  [ "$UPDATE_MODE" = "1" ] || return 0
  compose_file="${MIDDLE_DIR}/docker-compose.yml"
  middle_env="${MIDDLE_DIR}/.env"
  [ -f "$middle_env" ] || {
    echo "Update mode requires the existing middle/.env file." >&2
    return 1
  }
  container_id="$(docker compose --env-file "$middle_env" -f "$compose_file" ps -q mariadb)"
  [ -n "$container_id" ] || {
    echo "Update mode requires the existing MariaDB container to be running." >&2
    return 1
  }
  backup_root="${ATI_BACKUP_DIR:-${ROOT_DIR%/}-backups}"
  backup_dir="${backup_root}/update-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$backup_dir"
  chmod 700 "$backup_root" "$backup_dir"
  dump_tmp="${backup_dir}/all-databases.sql.partial"
  dump_file="${backup_dir}/all-databases.sql"
  echo "Backing up MariaDB before the container update..."
  if ! { printf '%s\n' "$MARIADB_PASSWORD" | docker compose --env-file "$middle_env" -f "$compose_file" exec -T mariadb sh -c \
    'IFS= read -r MYSQL_PWD; export MYSQL_PWD; exec mariadb-dump -uroot -h 127.0.0.1 --all-databases --single-transaction --routines --events --triggers'; } >"$dump_tmp"; then
    rm -f "$dump_tmp"
    echo "MariaDB backup failed; no containers were updated." >&2
    return 1
  fi
  [ -s "$dump_tmp" ] || {
    rm -f "$dump_tmp"
    echo "MariaDB backup was empty; no containers were updated." >&2
    return 1
  }
  mv "$dump_tmp" "$dump_file"
  chmod 600 "$dump_file"
  docker compose --env-file "$middle_env" -f "$compose_file" config >"${backup_dir}/compose-config.yml"
  chmod 600 "${backup_dir}/compose-config.yml"
  printf '%s\n' "$dump_file" >"${backup_dir}/BACKUP_COMPLETE"
  chmod 600 "${backup_dir}/BACKUP_COMPLETE"
  echo "Database backup completed: ${dump_file}"
}

ensure_docker
has_cmd curl || { echo "curl is required." >&2; exit 1; }
has_cmd tar || { echo "tar is required." >&2; exit 1; }

ROOT_BASE="${ROOT_DIR}/.env"
MIDDLE_BASE="${MIDDLE_DIR}/.env"
[ -f "$ROOT_BASE" ] || ROOT_BASE="${ROOT_DIR}/.env.example"
[ -f "$MIDDLE_BASE" ] || MIDDLE_BASE="${MIDDLE_DIR}/.env.example"

CURRENT_IB_USER="$(current_or_example "${MIDDLE_DIR}/.env" "${MIDDLE_DIR}/.env.example" TWS_USERID)"
CURRENT_IB_PASSWORD="$(current_or_example "${MIDDLE_DIR}/.env" "${MIDDLE_DIR}/.env.example" TWS_PASSWORD)"
CURRENT_IB_VNC="$(current_or_example "${MIDDLE_DIR}/.env" "${MIDDLE_DIR}/.env.example" VNC_SERVER_PASSWORD)"
CURRENT_ALPACA_KEY="$(current_or_example "${ROOT_DIR}/.env" "${ROOT_DIR}/.env.example" BROKER_RUNNER_ALPACA_API_KEY_ID)"
CURRENT_ALPACA_SECRET="$(current_or_example "${ROOT_DIR}/.env" "${ROOT_DIR}/.env.example" BROKER_RUNNER_ALPACA_SECRET_KEY)"
CURRENT_ADMIN_USERNAME="$(read_env_value "${ROOT_DIR}/.env" ADMIN_USERNAME)"
CURRENT_ADMIN_USERNAME="${CURRENT_ADMIN_USERNAME:-ati-local-user}"

REDIS_PASSWORD="$(resolve_managed_secret "${MIDDLE_DIR}/.env" REDIS_PASSWORD "$REDIS_PASSWORD_FILE" "Redis password")"
MARIADB_PASSWORD="$(resolve_managed_secret "${MIDDLE_DIR}/.env" MARIADB_PASSWORD "$MARIADB_PASSWORD_FILE" "MariaDB password")"
ADMIN_PASSWORD="$(resolve_managed_secret "${ROOT_DIR}/.env" ADMIN_PASSWORD "$ADMIN_PASSWORD_FILE" "ATI web password")"

if [ "$NON_INTERACTIVE" = "1" ]; then
  if [ -z "$ENABLED_ADAPTERS" ]; then ENABLED_ADAPTERS="$(read_env_value "${ROOT_DIR}/.env" BROKER_RUNNER_ENABLED_ADAPTERS)"; fi
  if [ -z "$INITIAL_ADAPTER" ]; then INITIAL_ADAPTER="$(read_env_value "${ROOT_DIR}/.env" BROKER_RUNNER_DEFAULT_ADAPTER_ID)"; fi
  [ -n "$ENABLED_ADAPTERS" ] || { echo "--enabled-adapters is required for a new non-interactive install." >&2; exit 2; }
  [ -n "$INITIAL_ADAPTER" ] || { echo "--initial-adapter is required for a new non-interactive install." >&2; exit 2; }
else
  choose_interactive_adapters
  choose_interactive_initial
fi
validate_enabled_adapters "$ENABLED_ADAPTERS"
validate_initial_adapter "$ENABLED_ADAPTERS" "$INITIAL_ADAPTER"

IB_USER="$CURRENT_IB_USER"; IB_PASSWORD="$CURRENT_IB_PASSWORD"; IB_VNC="$CURRENT_IB_VNC"
if contains_profile "$ENABLED_ADAPTERS" ibkr_paper; then
  if [ "$NON_INTERACTIVE" = "1" ]; then
    IB_USER="$(configured_existing_or_file "$CURRENT_IB_USER" "$IBKR_USERNAME_FILE" "IBKR username")"
  elif [ -n "$IBKR_USERNAME_FILE" ]; then
    IB_USER="$(read_secret_file "$IBKR_USERNAME_FILE" "IBKR username")"
  else
    IB_USER="$(prompt_masked_value "IBKR Paper username" "$CURRENT_IB_USER")"
  fi
  IB_PASSWORD="$(resolve_secret "$CURRENT_IB_PASSWORD" "$IBKR_PASSWORD_FILE" "IBKR password" "IBKR Paper password")"
  IB_VNC="$(resolve_managed_secret "${MIDDLE_DIR}/.env" VNC_SERVER_PASSWORD "$IBKR_VNC_PASSWORD_FILE" "IB Gateway VNC password")"
fi

ALPACA_KEY="$CURRENT_ALPACA_KEY"; ALPACA_SECRET="$CURRENT_ALPACA_SECRET"
ALPACA_DATA_FEED="${ALPACA_DATA_FEED:-$(current_or_example "${ROOT_DIR}/.env" "${ROOT_DIR}/.env.example" BROKER_RUNNER_ALPACA_DATA_FEED)}"
ALPACA_DATA_FEED="${ALPACA_DATA_FEED:-iex}"
if contains_profile "$ENABLED_ADAPTERS" alpaca_paper; then
  if [ "$NON_INTERACTIVE" = "1" ]; then
    ALPACA_KEY="$(configured_existing_or_file "$CURRENT_ALPACA_KEY" "$ALPACA_API_KEY_ID_FILE" "Alpaca API key ID")"
  elif [ -n "$ALPACA_API_KEY_ID_FILE" ]; then
    ALPACA_KEY="$(read_secret_file "$ALPACA_API_KEY_ID_FILE" "Alpaca API key ID")"
  else
    ALPACA_KEY="$(prompt_masked_value "Alpaca Paper API key ID" "$CURRENT_ALPACA_KEY")"
  fi
  ALPACA_SECRET="$(resolve_secret "$CURRENT_ALPACA_SECRET" "$ALPACA_SECRET_KEY_FILE" "Alpaca secret key" "Alpaca Paper secret key")"
  if [ "$NON_INTERACTIVE" = "0" ]; then
    ALPACA_DATA_FEED="$(prompt_value "Alpaca market data feed (iex/sip)" "$ALPACA_DATA_FEED" 0)"
  fi
fi
case "$ALPACA_DATA_FEED" in iex|sip) ;; *) echo "Alpaca data feed must be iex or sip." >&2; exit 2 ;; esac

echo "Configuration summary:"
echo "  Enabled adapters: ${ENABLED_ADAPTERS}"
echo "  Initial adapter: ${INITIAL_ADAPTER}"
if contains_profile "$ENABLED_ADAPTERS" ibkr_paper; then echo "  IB Gateway: enabled"; else echo "  IB Gateway: disabled"; fi
if contains_profile "$ENABLED_ADAPTERS" alpaca_paper; then echo "  Alpaca feed: ${ALPACA_DATA_FEED}"; fi
if [ "$ENABLED_ADAPTERS" = "sim" ]; then
  echo "  Adapter credentials: not required"
else
  echo "  Adapter credentials: configured (values hidden)"
fi
echo "  Service passwords: generated automatically or preserved from the existing configuration"
echo "  Environment file permissions: 0600"
if [ -f "${ROOT_DIR}/.env" ]; then echo "  Redis active selection will be updated to the selected initial adapter."; fi
if [ "$UPDATE_MODE" = "1" ]; then
  echo "  Image channel: latest (GHCR)"
fi
if [ "$NON_INTERACTIVE" = "1" ] && [ "$UPDATE_MODE" = "1" ] && [ "${ATI_ALLOW_UPDATE:-0}" != "1" ]; then
  echo "Non-interactive update requires ATI_ALLOW_UPDATE=1." >&2
  exit 1
fi
CONFIRM_PROMPT="Continue installation?"
CONFIRM_DEFAULT=no
if [ "$UPDATE_MODE" = "1" ]; then
  echo "Update actions:"
  echo "  1. Back up MariaDB before changing the running installation."
  echo "  2. Pull the latest GHCR container images."
  echo "  3. Install only the selected Adapter plugins into persistent local storage."
  echo "  4. Recreate the local containers with the updated images and plugins."
  CONFIRM_PROMPT="Proceed with the MariaDB backup, plugin installation, and container update?"
  CONFIRM_DEFAULT=yes
fi
if [ "$NON_INTERACTIVE" = "0" ] && ! prompt_yes_no "$CONFIRM_PROMPT" "$CONFIRM_DEFAULT"; then
  echo "Installation cancelled; no configuration was changed."
  exit 0
fi

ROOT_CANDIDATE="$(mktemp "${ROOT_DIR}/.env.candidate.XXXXXX")"
MIDDLE_CANDIDATE="$(mktemp "${MIDDLE_DIR}/.env.candidate.XXXXXX")"
chmod 600 "$ROOT_CANDIDATE" "$MIDDLE_CANDIDATE"
cp "$ROOT_BASE" "$ROOT_CANDIDATE"
cp "$MIDDLE_BASE" "$MIDDLE_CANDIDATE"
if [ "$UPDATE_MODE" = "1" ]; then
  env_set "$ROOT_CANDIDATE" ATI_IMAGE_TAG latest
fi
cleanup_candidates() {
  rm -f "${ROOT_CANDIDATE:-}" "${MIDDLE_CANDIDATE:-}"
  if [ -n "${PREPARED_PLUGIN_DIR:-}" ] && [ -e "$PREPARED_PLUGIN_DIR" ]; then
    run_as_root rm -rf "$PREPARED_PLUGIN_DIR"
  fi
  if [ -n "${PREPARED_PLUGIN_BUILD_DIR:-}" ] && [ -e "$PREPARED_PLUGIN_BUILD_DIR" ]; then
    run_as_root rm -rf "$PREPARED_PLUGIN_BUILD_DIR"
  fi
}
trap cleanup_candidates EXIT

JWT_SECRET="$(read_env_value "$ROOT_CANDIDATE" JWT_SECRET)"
if placeholder_or_empty "$JWT_SECRET"; then JWT_SECRET="$(generate_secret)"; fi
WATCHDOG_MAINTENANCE_TOKEN="$(read_env_value "$ROOT_CANDIDATE" SERVICE_WATCHDOG_MAINTENANCE_TOKEN)"
if placeholder_or_empty "$WATCHDOG_MAINTENANCE_TOKEN"; then WATCHDOG_MAINTENANCE_TOKEN="$(generate_secret)"; fi

env_set_quoted "$MIDDLE_CANDIDATE" REDIS_PASSWORD "$REDIS_PASSWORD"
env_set "$MIDDLE_CANDIDATE" MARIADB_DATABASE algo_trader
env_set "$MIDDLE_CANDIDATE" MARIADB_USER algo_trader
env_set_quoted "$MIDDLE_CANDIDATE" MARIADB_PASSWORD "$MARIADB_PASSWORD"
env_set_quoted "$MIDDLE_CANDIDATE" TWS_USERID "$IB_USER"
env_set_quoted "$MIDDLE_CANDIDATE" TWS_PASSWORD "$IB_PASSWORD"
env_set_quoted "$MIDDLE_CANDIDATE" VNC_SERVER_PASSWORD "$IB_VNC"

env_set_quoted "$ROOT_CANDIDATE" REDIS_URL "redis://:${REDIS_PASSWORD}@redis:6379/0"
env_set_quoted "$ROOT_CANDIDATE" BACKTEST_REDIS_URL "redis://:${REDIS_PASSWORD}@redis:6379/8"
env_set_quoted "$ROOT_CANDIDATE" MARIADB_URL "mariadb://algo_trader:${MARIADB_PASSWORD}@mariadb:3306/algo_trader"
env_set_quoted "$ROOT_CANDIDATE" BACKTEST_MARIADB_URL "mariadb://algo_trader_backtest:${MARIADB_PASSWORD}@mariadb:3306/algo_trader_backtest"
env_set_quoted "$ROOT_CANDIDATE" ADMIN_USERNAME "$CURRENT_ADMIN_USERNAME"
env_set_quoted "$ROOT_CANDIDATE" ADMIN_PASSWORD "$ADMIN_PASSWORD"
env_set_quoted "$ROOT_CANDIDATE" JWT_SECRET "$JWT_SECRET"
env_set_quoted "$ROOT_CANDIDATE" SERVICE_WATCHDOG_MAINTENANCE_TOKEN "$WATCHDOG_MAINTENANCE_TOKEN"
if [ "$UPDATE_MODE" = "1" ]; then
  # A legacy Orders schema can make the new Orders image exit before the
  # watchdog performs its maintenance preflight. Enable the deliberately
  # narrow offline recovery path for updates; it still requires the complete
  # legacy schema, zero active orders, and all other trading-safety checks.
  env_set "$ROOT_CANDIDATE" SERVICE_WATCHDOG_PHASE1_OFFLINE_ORDERS_PREFLIGHT 1
else
  env_set "$ROOT_CANDIDATE" SERVICE_WATCHDOG_PHASE1_OFFLINE_ORDERS_PREFLIGHT 0
fi
env_set "$ROOT_CANDIDATE" ALLOW_ANONYMOUS_ACCESS false
env_set "$ROOT_CANDIDATE" VITE_ALLOW_ANONYMOUS_ACCESS false
env_set "$ROOT_CANDIDATE" ATI_NETWORK_NAME "${ATI_NETWORK_NAME:-$(read_env_value "$ROOT_CANDIDATE" ATI_NETWORK_NAME)}"
env_set "$ROOT_CANDIDATE" ATI_NETWORK_SUBNET "${ATI_NETWORK_SUBNET:-$(read_env_value "$ROOT_CANDIDATE" ATI_NETWORK_SUBNET)}"
env_set "$ROOT_CANDIDATE" ATI_NETWORK_PREFIX "${ATI_NETWORK_PREFIX:-$(read_env_value "$ROOT_CANDIDATE" ATI_NETWORK_PREFIX)}"
env_set "$ROOT_CANDIDATE" ATI_CONTAINER_PREFIX "${ATI_CONTAINER_PREFIX:-$(read_env_value "$ROOT_CANDIDATE" ATI_CONTAINER_PREFIX)}"
env_set "$ROOT_CANDIDATE" FRONTEND_PORT "${FRONTEND_PORT:-$(read_env_value "$ROOT_CANDIDATE" FRONTEND_PORT)}"
if [ -z "$APP_URL" ]; then
  APP_URL="http://127.0.0.1:$(read_env_value "$ROOT_CANDIDATE" FRONTEND_PORT)"
fi
env_set "$ROOT_CANDIDATE" SERVICE_WATCHDOG_PORT "${SERVICE_WATCHDOG_PORT:-$(read_env_value "$ROOT_CANDIDATE" SERVICE_WATCHDOG_PORT)}"
env_set "$ROOT_CANDIDATE" ATI_IB_GATEWAY_CONTAINER_NAME "${ATI_IB_GATEWAY_CONTAINER_NAME:-$(read_env_value "$ROOT_CANDIDATE" ATI_IB_GATEWAY_CONTAINER_NAME)}"
if [ -n "${COMPOSE_PROJECT_NAME:-}" ]; then env_set "$ROOT_CANDIDATE" COMPOSE_PROJECT_NAME "$COMPOSE_PROJECT_NAME"; fi
for key in ATI_NETWORK_NAME ATI_NETWORK_SUBNET ATI_NETWORK_PREFIX ATI_IB_GATEWAY_CONTAINER_NAME; do
  env_set "$MIDDLE_CANDIDATE" "$key" "$(read_env_value "$ROOT_CANDIDATE" "$key")"
done
if [ -n "$(read_env_value "$ROOT_CANDIDATE" COMPOSE_PROJECT_NAME)" ]; then
  env_set "$MIDDLE_CANDIDATE" COMPOSE_PROJECT_NAME "$(read_env_value "$ROOT_CANDIDATE" COMPOSE_PROJECT_NAME)"
fi
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_PROFILE_REGISTRY_ENABLED true
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_ENABLED_ADAPTERS "$ENABLED_ADAPTERS"
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_DEFAULT_ADAPTER_ID "$INITIAL_ADAPTER"
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_ACTIVE_ADAPTER_REDIS_KEY broker_runner:active_adapter_id
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_IBKR_PAPER_PROVIDER "$(contains_profile "$ENABLED_ADAPTERS" ibkr_paper && echo package || echo core)"
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_PLUGIN_PATH /app/data/broker-plugins
env_set "$ROOT_CANDIDATE" BROKER_ADAPTER_SWITCH_ENABLED true
env_set "$ROOT_CANDIDATE" BROKER_ADAPTER_SWITCH_GATE_ENABLED true
env_set "$ROOT_CANDIDATE" BROKER_ADAPTER_SWITCH_POSITION_OVERRIDE_ENABLED false
env_set "$ROOT_CANDIDATE" VITE_BROKER_ADAPTER_SWITCH_UI_ENABLED true
env_set "$ROOT_CANDIDATE" BROKER_ASSET_CAPABILITY_GATE_ENABLED "$(contains_profile "$ENABLED_ADAPTERS" alpaca_paper && echo true || echo false)"
env_set_quoted "$ROOT_CANDIDATE" BROKER_RUNNER_ALPACA_API_KEY_ID "$ALPACA_KEY"
env_set_quoted "$ROOT_CANDIDATE" BROKER_RUNNER_ALPACA_SECRET_KEY "$ALPACA_SECRET"
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_ALPACA_DATA_FEED "$ALPACA_DATA_FEED"
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_ALPACA_REQUEST_TIMEOUT_SECONDS 15.0
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_ALPACA_RECONCILE_LOOKBACK_HOURS 72
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_ALPACA_MAX_CONCURRENCY 8
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_ALPACA_STREAM_QUEUE_SIZE 512
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_IB_GATEWAY_HOST ib-gateway
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_IB_GATEWAY_PORT 4004
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_IB_CLIENT_ID 40
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_IB_READ_ONLY false
env_set "$ROOT_CANDIDATE" SERVICE_WATCHDOG_IB_GATEWAY_ENABLED "$(contains_profile "$ENABLED_ADAPTERS" ibkr_paper && echo 1 || echo 0)"
env_set "$ROOT_CANDIDATE" SERVICE_WATCHDOG_IB_GATEWAY_CONTAINER "$(read_env_value "$ROOT_CANDIDATE" ATI_IB_GATEWAY_CONTAINER_NAME)"
env_set "$ROOT_CANDIDATE" SERVICE_WATCHDOG_IB_GATEWAY_DOCKER_HOST unix:///var/run/docker.sock
env_set "$ROOT_CANDIDATE" MARKET_DATA_IB_RESTART_URL "$(contains_profile "$ENABLED_ADAPTERS" ibkr_paper && echo http://backend:8000/runtime/ib-gateway/restart || true)"
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_URL http://broker-runner-service:8115
env_set "$ROOT_CANDIDATE" ACCOUNT_BROKER_RUNNER_URL http://broker-runner-service:8115
env_set "$ROOT_CANDIDATE" ORDERS_BROKER_RUNNER_URL http://broker-runner-service:8115
env_set "$ROOT_CANDIDATE" MARKET_DATA_BROKER_RUNNER_URL http://broker-runner-service:8115
env_set "$ROOT_CANDIDATE" SERVICE_WATCHDOG_URL http://service-watchdog:8110
env_set "$ROOT_CANDIDATE" SERVICE_WATCHDOG_ENABLED 1
env_set "$ROOT_CANDIDATE" APP_DOCS_URL ""
env_set "$ROOT_CANDIDATE" APP_REDOC_URL ""
env_set "$ROOT_CANDIDATE" APP_OPENAPI_URL ""
tag="$(read_env_value "$ROOT_CANDIDATE" ATI_IMAGE_TAG)"; tag="${tag:-latest}"
broker_runner_image="ghcr.io/winglight/algo-trader/broker-runner-service:${tag}"
frontend_image="${ATI_FRONTEND_IMAGE_OVERRIDE:-ghcr.io/winglight/algo-trader/frontend:${tag}}"
validate_public_image_reference "$broker_runner_image" broker-runner-service
validate_public_image_reference "$frontend_image" frontend
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_IMAGE "$broker_runner_image"
env_set "$ROOT_CANDIDATE" FRONTEND_IMAGE "$frontend_image"
echo "Pulling the official Broker Runner base image: ${broker_runner_image}"
docker pull "$broker_runner_image"
prepare_selected_adapter_plugins "$broker_runner_image"
validate_candidates "$ROOT_CANDIDATE" "$MIDDLE_CANDIDATE" "$broker_runner_image" "$PREPARED_PLUGIN_DIR"

echo "Validated configuration changes:"
for key in BROKER_RUNNER_ENABLED_ADAPTERS BROKER_RUNNER_DEFAULT_ADAPTER_ID BROKER_RUNNER_PROFILE_REGISTRY_ENABLED BROKER_RUNNER_IBKR_PAPER_PROVIDER BROKER_RUNNER_PLUGIN_PATH BROKER_ADAPTER_SWITCH_ENABLED BROKER_ASSET_CAPABILITY_GATE_ENABLED VITE_BROKER_ADAPTER_SWITCH_UI_ENABLED BROKER_RUNNER_IMAGE FRONTEND_IMAGE; do
  echo "  ${key}=$(read_env_value "$ROOT_CANDIDATE" "$key")"
done
echo "  credential fields=<redacted>"

if [ "$DRY_RUN" = "1" ]; then
  echo "Dry run completed; no configuration or containers were changed."
  exit 0
fi

backup_database_for_update

BACKUP_DIR="$(mktemp -d "${ROOT_DIR}/.installer-backup.XXXXXX")"
ROOT_EXISTED=0; MIDDLE_EXISTED=0
[ -f "${ROOT_DIR}/.env" ] && { cp "${ROOT_DIR}/.env" "${BACKUP_DIR}/root.env"; ROOT_EXISTED=1; }
[ -f "${MIDDLE_DIR}/.env" ] && { cp "${MIDDLE_DIR}/.env" "${BACKUP_DIR}/middle.env"; MIDDLE_EXISTED=1; }
chmod 700 "$BACKUP_DIR"
chmod 600 "${BACKUP_DIR}"/*.env 2>/dev/null || true
PREVIOUS_IB_ENABLED="$(read_env_value "${ROOT_DIR}/.env" SERVICE_WATCHDOG_IB_GATEWAY_ENABLED)"
ACTIVE_ADAPTER_SELECTION_CAPTURED=0
PREVIOUS_ACTIVE_ADAPTER_SELECTION=""
COMMITTED=1
rollback() {
  local rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ] && [ "${COMMITTED:-0}" = "1" ]; then
    echo "Installation failed; restoring previous environment files." >&2
    set +e
    if [ "$ROOT_EXISTED" = "1" ]; then cp "${BACKUP_DIR}/root.env" "${ROOT_DIR}/.env"; else rm -f "${ROOT_DIR}/.env"; fi
    if [ "$MIDDLE_EXISTED" = "1" ]; then cp "${BACKUP_DIR}/middle.env" "${MIDDLE_DIR}/.env"; else rm -f "${MIDDLE_DIR}/.env"; fi
    if [ "$PLUGIN_ACTIVATED" = "1" ]; then
      run_as_root rm -rf "${ROOT_DIR}/data/broker-plugins"
      if run_as_root test -e "$PLUGIN_BACKUP_DIR"; then
        run_as_root mv "$PLUGIN_BACKUP_DIR" "${ROOT_DIR}/data/broker-plugins"
      fi
      PLUGIN_ACTIVATED=0
    fi
    if [ "$ACTIVE_ADAPTER_SELECTION_CAPTURED" = "1" ]; then
      if [ -n "$PREVIOUS_ACTIVE_ADAPTER_SELECTION" ]; then
        docker compose --env-file "${MIDDLE_DIR}/.env" -f "${MIDDLE_DIR}/docker-compose.yml" exec -T \
          -e REDISCLI_AUTH="$REDIS_PASSWORD" redis redis-cli SET broker_runner:active_adapter_id \
          "$PREVIOUS_ACTIVE_ADAPTER_SELECTION" >/dev/null 2>&1 || true
      else
        docker compose --env-file "${MIDDLE_DIR}/.env" -f "${MIDDLE_DIR}/docker-compose.yml" exec -T \
          -e REDISCLI_AUTH="$REDIS_PASSWORD" redis redis-cli DEL broker_runner:active_adapter_id \
          >/dev/null 2>&1 || true
      fi
    fi
    chmod 600 "${ROOT_DIR}/.env" "${MIDDLE_DIR}/.env" 2>/dev/null || true
    if [ "$PREVIOUS_IB_ENABLED" = "1" ]; then
      ensure_ib_gateway_settings_permissions || true
      docker compose --env-file "${MIDDLE_DIR}/.env" -f "${MIDDLE_DIR}/docker-compose.yml" --profile ib up -d >/dev/null 2>&1 || true
    else
      docker compose --env-file "${MIDDLE_DIR}/.env" -f "${MIDDLE_DIR}/docker-compose.yml" --profile ib stop ib-gateway >/dev/null 2>&1 || true
      docker compose --env-file "${MIDDLE_DIR}/.env" -f "${MIDDLE_DIR}/docker-compose.yml" up -d >/dev/null 2>&1 || true
    fi
    docker compose -f "${ROOT_DIR}/docker-compose.yml" up -d >/dev/null 2>&1 || true
    set -e
  fi
  cleanup_candidates
  exit "$rc"
}
trap rollback EXIT
mv "$ROOT_CANDIDATE" "${ROOT_DIR}/.env"
mv "$MIDDLE_CANDIDATE" "${MIDDLE_DIR}/.env"
chmod 600 "${ROOT_DIR}/.env" "${MIDDLE_DIR}/.env"
activate_prepared_plugins

if contains_profile "$ENABLED_ADAPTERS" ibkr_paper; then
  ensure_ib_gateway_settings_permissions
  docker compose --env-file "${MIDDLE_DIR}/.env" -f "${MIDDLE_DIR}/docker-compose.yml" --profile ib up -d
else
  docker compose --env-file "${MIDDLE_DIR}/.env" -f "${MIDDLE_DIR}/docker-compose.yml" up -d
fi

wait_for_mariadb() {
  local tries=60
  while [ "$tries" -gt 0 ]; do
    if docker compose -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -c "[ -f /var/lib/mysql/.my-healthcheck.cnf ] && mariadb --defaults-extra-file=/var/lib/mysql/.my-healthcheck.cnf -h 127.0.0.1 -N -e 'SELECT 1' >/dev/null" >/dev/null 2>&1; then return 0; fi
    if printf '%s\n' "$MARIADB_PASSWORD" | docker compose -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -c 'IFS= read -r MYSQL_PWD; export MYSQL_PWD; mariadb -uroot -h 127.0.0.1 -N -e "SELECT 1" >/dev/null' >/dev/null 2>&1; then return 0; fi
    sleep 2; tries=$((tries - 1))
  done
  return 1
}
wait_for_mariadb || { echo "MariaDB is not ready." >&2; exit 1; }

SQL_PASSWORD="$(printf '%s' "$MARIADB_PASSWORD" | sed -e 's/\\/\\\\/g' -e "s/'/\\\\'/g")"
INIT_SQL="CREATE DATABASE IF NOT EXISTS algo_trader; CREATE DATABASE IF NOT EXISTS algo_trader_backtest; CREATE USER IF NOT EXISTS 'algo_trader'@'%' IDENTIFIED BY '${SQL_PASSWORD}'; CREATE USER IF NOT EXISTS 'algo_trader_backtest'@'%' IDENTIFIED BY '${SQL_PASSWORD}'; GRANT ALL PRIVILEGES ON algo_trader.* TO 'algo_trader'@'%'; GRANT ALL PRIVILEGES ON algo_trader_backtest.* TO 'algo_trader_backtest'@'%'; FLUSH PRIVILEGES;"
{ printf '%s\n' "$MARIADB_PASSWORD"; printf '%s\n' "$INIT_SQL"; } | docker compose -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -c 'IFS= read -r MYSQL_PWD; export MYSQL_PWD; exec mariadb -uroot -h 127.0.0.1' >/dev/null

PREVIOUS_ACTIVE_ADAPTER_SELECTION="$(
  docker compose --env-file "${MIDDLE_DIR}/.env" -f "${MIDDLE_DIR}/docker-compose.yml" exec -T \
    -e REDISCLI_AUTH="$REDIS_PASSWORD" redis redis-cli --raw GET broker_runner:active_adapter_id
)"
ACTIVE_ADAPTER_SELECTION_CAPTURED=1
docker compose --env-file "${MIDDLE_DIR}/.env" -f "${MIDDLE_DIR}/docker-compose.yml" exec -T \
  -e REDISCLI_AUTH="$REDIS_PASSWORD" redis redis-cli SET broker_runner:active_adapter_id \
  "$INITIAL_ADAPTER" >/dev/null
echo "Active adapter selection updated to: ${INITIAL_ADAPTER}"

if [ -f "${ROOT_DIR}/algo_trader.sql" ]; then
  { printf '%s\n' "$MARIADB_PASSWORD"; cat "${ROOT_DIR}/algo_trader.sql"; } | docker compose -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -c 'IFS= read -r MYSQL_PWD; export MYSQL_PWD; exec mariadb -uroot -h 127.0.0.1 algo_trader' >/dev/null
fi

pull_application_images
(cd "$ROOT_DIR" && docker compose -f docker-compose.yml up -d)

COMMITTED=0
trap cleanup_candidates EXIT
if [ -n "$PLUGIN_BACKUP_DIR" ] && run_as_root test -e "$PLUGIN_BACKUP_DIR"; then
  run_as_root rm -rf "$PLUGIN_BACKUP_DIR"
fi
rm -rf "$BACKUP_DIR"
if wait_for_http "$APP_URL" 90; then open_browser; fi
echo "Done. Open ${APP_URL} and log in with:"
echo "  username: ${CURRENT_ADMIN_USERNAME}"
echo "  password: see ADMIN_PASSWORD in ${ROOT_DIR}/.env"
echo "Generated service passwords are stored in:"
echo "  ${ROOT_DIR}/.env"
echo "  ${MIDDLE_DIR}/.env"
echo "Keep these permission-controlled files private and do not commit them to Git."
