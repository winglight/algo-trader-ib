#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIDDLE_DIR="${ROOT_DIR}/middle"
APP_URL="${ATI_APP_URL:-http://127.0.0.1:${FRONTEND_PORT:-5173}}"
ADAPTERS_COMMIT="69aaa90750f92e0d7d05c8eba21783fe57d5d681"
ADAPTERS_ARCHIVE_SHA256="e9481d3a411e5907d51204beeb85426cfb758c4587fc894c8661f8979d6b174e"
ADAPTERS_ARCHIVE_URL="https://github.com/winglight/algo-trader-broker-adapters/archive/${ADAPTERS_COMMIT}.tar.gz"
ALPACA_PY_VERSION="0.43.5"
ALPACA_PY_WHEEL_SHA256="0b4cac9b743851310f19f6a9aa84f57ddf95ae75b601350395746a893f54a2da"

# shellcheck source=scripts/installer_lib.sh
source "${ROOT_DIR}/scripts/installer_lib.sh"

NON_INTERACTIVE=0
DRY_RUN=0
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

usage() {
  cat <<'EOF'
Usage: setup_and_run.sh [options]

  --non-interactive
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
  local existing ib_default alpaca_default
  existing="$(read_env_value "${ROOT_DIR}/.env" BROKER_RUNNER_ENABLED_ADAPTERS)"
  [ -n "$existing" ] || existing="$(legacy_enabled_adapters)"
  ib_default=no; alpaca_default=no
  contains_profile "$existing" ibkr_paper && ib_default=yes
  contains_profile "$existing" alpaca_paper && alpaca_default=yes
  ENABLED_ADAPTERS=sim
  echo "Sim Adapter is always enabled."
  if prompt_yes_no "Configure IBKR Paper Adapter?" "$ib_default"; then
    ENABLED_ADAPTERS="${ENABLED_ADAPTERS},ibkr_paper"
  fi
  if prompt_yes_no "Configure Alpaca Paper Adapter?" "$alpaca_default"; then
    ENABLED_ADAPTERS="${ENABLED_ADAPTERS},alpaca_paper"
  fi
}

choose_interactive_initial() {
  local choices=(sim) choice default_index=1 index=1 existing
  contains_profile "$ENABLED_ADAPTERS" ibkr_paper && choices+=(ibkr_paper)
  contains_profile "$ENABLED_ADAPTERS" alpaca_paper && choices+=(alpaca_paper)
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

sha256_file() {
  if has_cmd sha256sum; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi
}

prepare_alpaca_image() {
  local candidate_env="$1" build_root archive extract_root downloads wheelhouse lock_file runtime_arch
  local line_arch package version filename checksum url cached actual selected_count base_image local_image tag
  build_root="${ROOT_DIR}/.ati-adapter-build"
  archive="${build_root}/adapters-${ADAPTERS_COMMIT}.tar.gz"
  extract_root="${build_root}/source"
  downloads="${build_root}/downloads"
  wheelhouse="${build_root}/wheelhouse"
  lock_file="${ROOT_DIR}/docker/alpaca-runtime-wheels.lock"
  mkdir -p "$build_root"
  chmod 700 "$build_root"
  if [ ! -f "$archive" ] || [ "$(sha256_file "$archive" 2>/dev/null || true)" != "$ADAPTERS_ARCHIVE_SHA256" ]; then
    actual="${archive}.download"
    curl -fsSL "$ADAPTERS_ARCHIVE_URL" -o "$actual"
    [ "$(sha256_file "$actual")" = "$ADAPTERS_ARCHIVE_SHA256" ] || {
      echo "Adapter source checksum verification failed." >&2
      return 1
    }
    mv "$actual" "$archive"
  fi
  runtime_arch="$(docker info --format '{{.Architecture}}')"
  case "$runtime_arch" in
    amd64|x86_64) runtime_arch=amd64 ;;
    arm64|aarch64) runtime_arch=arm64 ;;
    *) echo "Unsupported Docker architecture for Alpaca adapter: ${runtime_arch:-unknown}" >&2; return 1 ;;
  esac
  [ -f "$lock_file" ] || { echo "Alpaca runtime wheel lock is missing." >&2; return 1; }
  mkdir -p "$downloads"
  rm -rf "$wheelhouse"
  mkdir -p "$wheelhouse"
  : >"${wheelhouse}/SHA256SUMS"
  selected_count=0
  while IFS='|' read -r line_arch package version filename checksum url; do
    case "$line_arch" in ''|'#'*) continue ;; esac
    [ "$line_arch" = "any" ] || [ "$line_arch" = "$runtime_arch" ] || continue
    cached="${downloads}/${filename}"
    if [ ! -f "$cached" ] || [ "$(sha256_file "$cached" 2>/dev/null || true)" != "$checksum" ]; then
      actual="${cached}.download"
      curl -fsSL "$url" -o "$actual"
      [ "$(sha256_file "$actual")" = "$checksum" ] || {
        echo "Wheel checksum verification failed for ${package}==${version}." >&2
        return 1
      }
      mv "$actual" "$cached"
    fi
    cp "$cached" "${wheelhouse}/${filename}"
    printf '%s  %s\n' "$checksum" "$filename" >>"${wheelhouse}/SHA256SUMS"
    selected_count=$((selected_count + 1))
  done <"$lock_file"
  [ "$selected_count" -eq 7 ] || {
    echo "Alpaca runtime wheel lock did not select exactly seven artifacts for ${runtime_arch}." >&2
    return 1
  }
  rm -rf "$extract_root"
  mkdir -p "$extract_root"
  tar -xzf "$archive" --strip-components=1 -C "$extract_root"
  tag="$(read_env_value "$candidate_env" ATI_IMAGE_TAG)"; tag="${tag:-latest}"
  base_image="ghcr.io/winglight/algo-trader/broker-runner-service:${tag}"
  local_image="ati-local/broker-runner:${tag}-alpaca-${ADAPTERS_COMMIT:0:12}"
  docker build \
    --build-arg "BASE_IMAGE=${base_image}" \
    --label "org.opencontainers.image.revision=${ADAPTERS_COMMIT}" \
    --label "com.broyustudio.ati.alpaca-py.version=${ALPACA_PY_VERSION}" \
    --label "com.broyustudio.ati.alpaca-py.sha256=${ALPACA_PY_WHEEL_SHA256}" \
    --label "com.broyustudio.ati.adapters.archive.sha256=${ADAPTERS_ARCHIVE_SHA256}" \
    --label "com.broyustudio.ati.sbom.path=/app/sbom/alpaca-paper.spdx.json" \
    -f "${ROOT_DIR}/docker/Dockerfile.broker_runner_adapters" \
    -t "$local_image" \
    "$ROOT_DIR"
  env_set "$candidate_env" BROKER_RUNNER_IMAGE "$local_image"
  docker run --rm --env-file "$candidate_env" --entrypoint python "$local_image" -c \
    'from alpaca.data.historical import StockHistoricalDataClient; from alpaca.trading.client import TradingClient; from importlib.metadata import version; from pathlib import Path; from src.broker_runner.settings import BrokerRunnerSettings; from src.broker_runner.profile_registry import AdapterProfileRegistry; import os; s=BrokerRunnerSettings.from_env(); r=AdapterProfileRegistry(s.enabled_adapter_ids, os.environ); assert r.state("alpaca_paper").installed; assert version("alpaca-py") == "0.43.5"; assert version("pandas") == "2.2.3"; assert Path("/app/sbom/alpaca-paper.spdx.json").is_file()'
}

validate_candidates() {
  local root_env="$1" middle_env="$2" base_image tag
  validate_enabled_adapters "$ENABLED_ADAPTERS"
  validate_initial_adapter "$ENABLED_ADAPTERS" "$INITIAL_ADAPTER"
  case "$ALPACA_DATA_FEED" in iex|sip) ;; *) echo "Alpaca data feed must be iex or sip." >&2; return 1 ;; esac
  docker compose --env-file "$middle_env" -f "${MIDDLE_DIR}/docker-compose.yml" config -q
  docker compose --env-file "$root_env" -f "${ROOT_DIR}/docker-compose.yml" config -q
  tag="$(read_env_value "$root_env" ATI_IMAGE_TAG)"; tag="${tag:-latest}"
  base_image="ghcr.io/winglight/algo-trader/broker-runner-service:${tag}"
  docker run --rm --env-file "$root_env" --entrypoint python "$base_image" -c \
    'from src.broker_runner.settings import BrokerRunnerSettings; import os; s=BrokerRunnerSettings.from_env(); [s.profile_settings(p, os.environ) for p in s.enabled_adapter_ids]'
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

ensure_docker
has_cmd curl || { echo "curl is required." >&2; exit 1; }
has_cmd tar || { echo "tar is required." >&2; exit 1; }

ROOT_BASE="${ROOT_DIR}/.env"
MIDDLE_BASE="${MIDDLE_DIR}/.env"
[ -f "$ROOT_BASE" ] || ROOT_BASE="${ROOT_DIR}/.env.example"
[ -f "$MIDDLE_BASE" ] || MIDDLE_BASE="${MIDDLE_DIR}/.env.example"

CURRENT_REDIS="$(current_or_example "${MIDDLE_DIR}/.env" "${MIDDLE_DIR}/.env.example" REDIS_PASSWORD)"
CURRENT_MARIADB="$(current_or_example "${MIDDLE_DIR}/.env" "${MIDDLE_DIR}/.env.example" MARIADB_PASSWORD)"
CURRENT_ADMIN="$(current_or_example "${ROOT_DIR}/.env" "${ROOT_DIR}/.env.example" ADMIN_PASSWORD)"
CURRENT_IB_USER="$(current_or_example "${MIDDLE_DIR}/.env" "${MIDDLE_DIR}/.env.example" TWS_USERID)"
CURRENT_IB_PASSWORD="$(current_or_example "${MIDDLE_DIR}/.env" "${MIDDLE_DIR}/.env.example" TWS_PASSWORD)"
CURRENT_IB_VNC="$(current_or_example "${MIDDLE_DIR}/.env" "${MIDDLE_DIR}/.env.example" VNC_SERVER_PASSWORD)"
CURRENT_ALPACA_KEY="$(current_or_example "${ROOT_DIR}/.env" "${ROOT_DIR}/.env.example" BROKER_RUNNER_ALPACA_API_KEY_ID)"
CURRENT_ALPACA_SECRET="$(current_or_example "${ROOT_DIR}/.env" "${ROOT_DIR}/.env.example" BROKER_RUNNER_ALPACA_SECRET_KEY)"

REDIS_PASSWORD="$(resolve_secret "$CURRENT_REDIS" "$REDIS_PASSWORD_FILE" "Redis password" "Redis password")"
MARIADB_PASSWORD="$(resolve_secret "$CURRENT_MARIADB" "$MARIADB_PASSWORD_FILE" "MariaDB password" "MariaDB password")"
ADMIN_PASSWORD="$(resolve_secret "$CURRENT_ADMIN" "$ADMIN_PASSWORD_FILE" "ATI web password" "ATI web password")"

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
    IB_USER="$(prompt_value "IBKR Paper username" "$CURRENT_IB_USER" 1)"
  fi
  IB_PASSWORD="$(resolve_secret "$CURRENT_IB_PASSWORD" "$IBKR_PASSWORD_FILE" "IBKR password" "IBKR Paper password")"
  IB_VNC="$(resolve_secret "$CURRENT_IB_VNC" "$IBKR_VNC_PASSWORD_FILE" "IB Gateway VNC password" "IB Gateway VNC password")"
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
    ALPACA_KEY="$(prompt_value "Alpaca Paper API key ID" "$CURRENT_ALPACA_KEY" 1)"
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
echo "  Credentials: configured (values hidden)"
if [ -f "${ROOT_DIR}/.env" ]; then echo "  Existing Redis active selection will be preserved."; fi
if [ "$NON_INTERACTIVE" = "0" ] && ! prompt_yes_no "Continue installation?" no; then
  echo "Installation cancelled; no configuration was changed."
  exit 0
fi

ROOT_CANDIDATE="$(mktemp "${ROOT_DIR}/.env.candidate.XXXXXX")"
MIDDLE_CANDIDATE="$(mktemp "${MIDDLE_DIR}/.env.candidate.XXXXXX")"
chmod 600 "$ROOT_CANDIDATE" "$MIDDLE_CANDIDATE"
cp "$ROOT_BASE" "$ROOT_CANDIDATE"
cp "$MIDDLE_BASE" "$MIDDLE_CANDIDATE"
cleanup_candidates() { rm -f "${ROOT_CANDIDATE:-}" "${MIDDLE_CANDIDATE:-}"; }
trap cleanup_candidates EXIT

JWT_SECRET="$(read_env_value "$ROOT_CANDIDATE" JWT_SECRET)"
if placeholder_or_empty "$JWT_SECRET"; then JWT_SECRET="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"; fi

env_set "$MIDDLE_CANDIDATE" REDIS_PASSWORD "$REDIS_PASSWORD"
env_set "$MIDDLE_CANDIDATE" MARIADB_DATABASE algo_trader
env_set "$MIDDLE_CANDIDATE" MARIADB_USER algo_trader
env_set "$MIDDLE_CANDIDATE" MARIADB_PASSWORD "$MARIADB_PASSWORD"
env_set "$MIDDLE_CANDIDATE" TWS_USERID "$IB_USER"
env_set "$MIDDLE_CANDIDATE" TWS_PASSWORD "$IB_PASSWORD"
env_set "$MIDDLE_CANDIDATE" VNC_SERVER_PASSWORD "$IB_VNC"

env_set "$ROOT_CANDIDATE" REDIS_URL "redis://:${REDIS_PASSWORD}@redis:6379/0"
env_set "$ROOT_CANDIDATE" BACKTEST_REDIS_URL "redis://:${REDIS_PASSWORD}@redis:6379/8"
env_set "$ROOT_CANDIDATE" MARIADB_URL "mariadb://algo_trader:${MARIADB_PASSWORD}@mariadb:3306/algo_trader"
env_set "$ROOT_CANDIDATE" BACKTEST_MARIADB_URL "mariadb://algo_trader_backtest:${MARIADB_PASSWORD}@mariadb:3306/algo_trader_backtest"
env_set "$ROOT_CANDIDATE" ADMIN_USERNAME ati-guest
env_set "$ROOT_CANDIDATE" ADMIN_PASSWORD "$ADMIN_PASSWORD"
env_set "$ROOT_CANDIDATE" JWT_SECRET "$JWT_SECRET"
env_set "$ROOT_CANDIDATE" ALLOW_ANONYMOUS_ACCESS false
env_set "$ROOT_CANDIDATE" VITE_ALLOW_ANONYMOUS_ACCESS false
env_set "$ROOT_CANDIDATE" ATI_NETWORK_NAME "${ATI_NETWORK_NAME:-$(read_env_value "$ROOT_CANDIDATE" ATI_NETWORK_NAME)}"
env_set "$ROOT_CANDIDATE" ATI_NETWORK_SUBNET "${ATI_NETWORK_SUBNET:-$(read_env_value "$ROOT_CANDIDATE" ATI_NETWORK_SUBNET)}"
env_set "$ROOT_CANDIDATE" ATI_NETWORK_PREFIX "${ATI_NETWORK_PREFIX:-$(read_env_value "$ROOT_CANDIDATE" ATI_NETWORK_PREFIX)}"
env_set "$ROOT_CANDIDATE" ATI_CONTAINER_PREFIX "${ATI_CONTAINER_PREFIX:-$(read_env_value "$ROOT_CANDIDATE" ATI_CONTAINER_PREFIX)}"
env_set "$ROOT_CANDIDATE" FRONTEND_PORT "${FRONTEND_PORT:-$(read_env_value "$ROOT_CANDIDATE" FRONTEND_PORT)}"
env_set "$ROOT_CANDIDATE" SERVICE_WATCHDOG_PORT "${SERVICE_WATCHDOG_PORT:-$(read_env_value "$ROOT_CANDIDATE" SERVICE_WATCHDOG_PORT)}"
env_set "$ROOT_CANDIDATE" ATI_IB_GATEWAY_CONTAINER_NAME "${ATI_IB_GATEWAY_CONTAINER_NAME:-$(read_env_value "$ROOT_CANDIDATE" ATI_IB_GATEWAY_CONTAINER_NAME)}"
if [ -n "${COMPOSE_PROJECT_NAME:-}" ]; then env_set "$ROOT_CANDIDATE" COMPOSE_PROJECT_NAME "$COMPOSE_PROJECT_NAME"; fi
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_PROFILE_REGISTRY_ENABLED true
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_ENABLED_ADAPTERS "$ENABLED_ADAPTERS"
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_DEFAULT_ADAPTER_ID "$INITIAL_ADAPTER"
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_ACTIVE_ADAPTER_REDIS_KEY broker_runner:active_adapter_id
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_IBKR_PAPER_PROVIDER core
env_set "$ROOT_CANDIDATE" BROKER_ADAPTER_SWITCH_ENABLED true
env_set "$ROOT_CANDIDATE" BROKER_ADAPTER_SWITCH_GATE_ENABLED true
env_set "$ROOT_CANDIDATE" BROKER_ADAPTER_SWITCH_POSITION_OVERRIDE_ENABLED false
env_set "$ROOT_CANDIDATE" VITE_BROKER_ADAPTER_SWITCH_UI_ENABLED true
env_set "$ROOT_CANDIDATE" BROKER_ASSET_CAPABILITY_GATE_ENABLED "$(contains_profile "$ENABLED_ADAPTERS" alpaca_paper && echo true || echo false)"
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_ALPACA_API_KEY_ID "$ALPACA_KEY"
env_set "$ROOT_CANDIDATE" BROKER_RUNNER_ALPACA_SECRET_KEY "$ALPACA_SECRET"
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
env_set "$ROOT_CANDIDATE" SERVICE_WATCHDOG_IB_GATEWAY_CONTAINER "${ATI_IB_GATEWAY_CONTAINER_NAME:-ib-gateway}"
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
if [ -n "${ATI_FRONTEND_IMAGE_OVERRIDE:-}" ]; then
  env_set "$ROOT_CANDIDATE" FRONTEND_IMAGE "$ATI_FRONTEND_IMAGE_OVERRIDE"
fi

validate_candidates "$ROOT_CANDIDATE" "$MIDDLE_CANDIDATE"
if contains_profile "$ENABLED_ADAPTERS" alpaca_paper; then
  prepare_alpaca_image "$ROOT_CANDIDATE"
else
  tag="$(read_env_value "$ROOT_CANDIDATE" ATI_IMAGE_TAG)"; tag="${tag:-latest}"
  env_set "$ROOT_CANDIDATE" BROKER_RUNNER_IMAGE "ghcr.io/winglight/algo-trader/broker-runner-service:${tag}"
fi

echo "Validated configuration changes:"
for key in BROKER_RUNNER_ENABLED_ADAPTERS BROKER_RUNNER_DEFAULT_ADAPTER_ID BROKER_RUNNER_PROFILE_REGISTRY_ENABLED BROKER_ADAPTER_SWITCH_ENABLED BROKER_ASSET_CAPABILITY_GATE_ENABLED VITE_BROKER_ADAPTER_SWITCH_UI_ENABLED BROKER_RUNNER_IMAGE; do
  echo "  ${key}=$(read_env_value "$ROOT_CANDIDATE" "$key")"
done
echo "  credential fields=<redacted>"

if [ "$DRY_RUN" = "1" ]; then
  echo "Dry run completed; no configuration or containers were changed."
  exit 0
fi

BACKUP_DIR="$(mktemp -d "${ROOT_DIR}/.installer-backup.XXXXXX")"
ROOT_EXISTED=0; MIDDLE_EXISTED=0
[ -f "${ROOT_DIR}/.env" ] && { cp "${ROOT_DIR}/.env" "${BACKUP_DIR}/root.env"; ROOT_EXISTED=1; }
[ -f "${MIDDLE_DIR}/.env" ] && { cp "${MIDDLE_DIR}/.env" "${BACKUP_DIR}/middle.env"; MIDDLE_EXISTED=1; }
chmod 700 "$BACKUP_DIR"
chmod 600 "${BACKUP_DIR}"/*.env 2>/dev/null || true
PREVIOUS_IB_ENABLED="$(read_env_value "${ROOT_DIR}/.env" SERVICE_WATCHDOG_IB_GATEWAY_ENABLED)"
COMMITTED=1
rollback() {
  local rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ] && [ "${COMMITTED:-0}" = "1" ]; then
    echo "Installation failed; restoring previous environment files." >&2
    set +e
    if [ "$ROOT_EXISTED" = "1" ]; then cp "${BACKUP_DIR}/root.env" "${ROOT_DIR}/.env"; else rm -f "${ROOT_DIR}/.env"; fi
    if [ "$MIDDLE_EXISTED" = "1" ]; then cp "${BACKUP_DIR}/middle.env" "${MIDDLE_DIR}/.env"; else rm -f "${MIDDLE_DIR}/.env"; fi
    chmod 600 "${ROOT_DIR}/.env" "${MIDDLE_DIR}/.env" 2>/dev/null || true
    if [ "$PREVIOUS_IB_ENABLED" = "1" ]; then
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

if contains_profile "$ENABLED_ADAPTERS" ibkr_paper; then
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

if [ -f "${ROOT_DIR}/algo_trader.sql" ]; then
  { printf '%s\n' "$MARIADB_PASSWORD"; cat "${ROOT_DIR}/algo_trader.sql"; } | docker compose -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -c 'IFS= read -r MYSQL_PWD; export MYSQL_PWD; exec mariadb -uroot -h 127.0.0.1 algo_trader' >/dev/null
fi

(cd "$ROOT_DIR" && docker compose -f docker-compose.yml pull --ignore-pull-failures && docker compose -f docker-compose.yml up -d)

COMMITTED=0
trap cleanup_candidates EXIT
rm -rf "$BACKUP_DIR"
if wait_for_http "$APP_URL" 90; then open_browser; fi
echo "Done. Open ${APP_URL} and log in with:"
echo "  username: ati-guest"
echo "  password: the ATI web password you entered"
