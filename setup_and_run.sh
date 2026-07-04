#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIDDLE_DIR="${ROOT_DIR}/middle"
APP_URL="http://127.0.0.1:5173"

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

copy_if_missing() {
  local src="$1" dst="$2"
  if [ ! -f "$dst" ]; then
    cp "$src" "$dst"
  fi
}

ensure_cmd() {
  if ! has_cmd "$1"; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

ensure_docker() {
  if has_cmd docker && docker compose version >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    return 0
  fi

  echo "Docker is not ready. Running public/scripts/install_docker.sh ..."
  bash "${ROOT_DIR}/scripts/install_docker.sh"

  if ! has_cmd docker || ! docker compose version >/dev/null 2>&1; then
    echo "Docker Compose plugin is still unavailable after installation." >&2
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "Docker is installed but not reachable yet. Start Docker, then rerun this installer." >&2
    exit 1
  fi
}

prompt_value() {
  local prompt="$1" default_value="${2:-}" secret="${3:-0}" value
  if [ "$secret" = "1" ]; then
    if [ -n "$default_value" ]; then
      read -r -s -p "$prompt [keep existing]: " value
    else
      read -r -s -p "$prompt: " value
    fi
    printf '\n' >&2
  else
    if [ -n "$default_value" ]; then
      read -r -p "$prompt [$default_value]: " value
    else
      read -r -p "$prompt: " value
    fi
  fi
  if [ -z "$value" ]; then
    value="$default_value"
  fi
  printf '%s' "$value"
}

read_env_value() {
  local file="$1" key="$2"
  if [ -f "$file" ]; then
    grep -E "^${key}=" "$file" | tail -n 1 | cut -d'=' -f2-
  fi
}

placeholder_or_empty() {
  local value="$1"
  case "$value" in
    ""|"change_me"|"change-this-even-stronger"|"ChangeThisUserPassword")
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

get_or_prompt_from_file() {
  local file="$1" example="$2" key="$3" prompt="$4" secret="${5:-1}"
  local current default
  current="$(read_env_value "$file" "$key")"
  default="$(read_env_value "$example" "$key")"
  if ! placeholder_or_empty "$current" && [ "$current" != "$default" ]; then
    prompt_value "$prompt" "$current" "$secret"
  else
    prompt_value "$prompt" "" "$secret"
  fi
}

choose_adapter() {
  local current choice
  current="$(read_env_value "${ROOT_DIR}/.env" BROKER_ADAPTER_MODE)"
  case "$current" in
    ib|sim) ;;
    *) current="sim" ;;
  esac
  while true; do
    choice="$(prompt_value "Broker adapter: sim or ib" "$current" 0)"
    case "$(printf '%s' "$choice" | tr '[:upper:]' '[:lower:]')" in
      sim|s)
        printf 'sim'
        return 0
        ;;
      ib|i)
        printf 'ib'
        return 0
        ;;
      *)
        echo "Please input sim or ib." >&2
        ;;
    esac
  done
}

sed_escape_repl_pipe() {
  printf '%s' "$1" | sed -e 's/[|&]/\\&/g'
}

update_env_line() {
  local file="$1" key="$2" value="$3" escaped_value
  escaped_value="$(sed_escape_repl_pipe "$value")"
  if grep -q "^${key}=" "$file"; then
    sed -i.bak -e "s|^${key}=.*|${key}=${escaped_value}|" "$file"
    rm -f "${file}.bak"
  else
    printf '\n%s=%s\n' "$key" "$value" >>"$file"
  fi
}

sh_escape_dq() {
  printf '%s' "$1" | sed -e 's/[\\$"]/\\&/g'
}

mysql_escape_sq() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e "s/'/\\'/g"
}

open_browser() {
  if has_cmd open; then
    open "$APP_URL" >/dev/null 2>&1 || true
  elif has_cmd xdg-open; then
    xdg-open "$APP_URL" >/dev/null 2>&1 || true
  elif has_cmd powershell.exe; then
    powershell.exe -NoProfile -Command "Start-Process '$APP_URL'" >/dev/null 2>&1 || true
  fi
}

wait_for_http() {
  local url="$1" tries="${2:-90}"
  while [ "$tries" -gt 0 ]; do
    if has_cmd curl && curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
    tries=$((tries - 1))
  done
  return 1
}

ensure_cmd sed
ensure_docker

copy_if_missing "${MIDDLE_DIR}/.env.example" "${MIDDLE_DIR}/.env"
copy_if_missing "${ROOT_DIR}/.env.example" "${ROOT_DIR}/.env"

REDIS_PASSWORD="$(get_or_prompt_from_file "${MIDDLE_DIR}/.env" "${MIDDLE_DIR}/.env.example" REDIS_PASSWORD "Redis password" 1)"
MARIADB_PASSWORD="$(get_or_prompt_from_file "${MIDDLE_DIR}/.env" "${MIDDLE_DIR}/.env.example" MARIADB_PASSWORD "MariaDB password" 1)"
ADMIN_PASSWORD="$(get_or_prompt_from_file "${ROOT_DIR}/.env" "${ROOT_DIR}/.env.example" ADMIN_PASSWORD "ATI web password for ati-guest" 1)"
BROKER_ADAPTER_MODE="$(choose_adapter)"

JWT_SECRET="$(read_env_value "${ROOT_DIR}/.env" JWT_SECRET)"
if [ -z "$JWT_SECRET" ] || [ "$JWT_SECRET" = "change_me" ]; then
  JWT_SECRET="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
fi

MARIADB_DATABASE="algo_trader"
MARIADB_USER="algo_trader"

update_env_line "${MIDDLE_DIR}/.env" REDIS_PASSWORD "$REDIS_PASSWORD"
update_env_line "${MIDDLE_DIR}/.env" MARIADB_DATABASE "$MARIADB_DATABASE"
update_env_line "${MIDDLE_DIR}/.env" MARIADB_USER "$MARIADB_USER"
update_env_line "${MIDDLE_DIR}/.env" MARIADB_PASSWORD "$MARIADB_PASSWORD"

MIDDLE_PROFILE_ARGS=()
if [ "$BROKER_ADAPTER_MODE" = "ib" ]; then
  TWS_USERID="$(get_or_prompt_from_file "${MIDDLE_DIR}/.env" "${MIDDLE_DIR}/.env.example" TWS_USERID "IBKR paper username" 0)"
  TWS_PASSWORD="$(get_or_prompt_from_file "${MIDDLE_DIR}/.env" "${MIDDLE_DIR}/.env.example" TWS_PASSWORD "IBKR paper password" 1)"
  VNC_SERVER_PASSWORD="$(get_or_prompt_from_file "${MIDDLE_DIR}/.env" "${MIDDLE_DIR}/.env.example" VNC_SERVER_PASSWORD "IB Gateway VNC password" 1)"
  update_env_line "${MIDDLE_DIR}/.env" TWS_USERID "$TWS_USERID"
  update_env_line "${MIDDLE_DIR}/.env" TWS_PASSWORD "$TWS_PASSWORD"
  update_env_line "${MIDDLE_DIR}/.env" VNC_SERVER_PASSWORD "$VNC_SERVER_PASSWORD"
  MIDDLE_PROFILE_ARGS=(--profile ib)
fi

(
  cd "${MIDDLE_DIR}"
  docker compose "${MIDDLE_PROFILE_ARGS[@]}" up -d
)

wait_for_mariadb() {
  local tries=60 healthcheck_cnf="/var/lib/mysql/.my-healthcheck.cnf" dq_password
  dq_password="$(sh_escape_dq "${MARIADB_PASSWORD}")"
  while [ "$tries" -gt 0 ]; do
    if docker compose -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -c "[ -f '${healthcheck_cnf}' ] && mariadb --defaults-extra-file='${healthcheck_cnf}' -h 127.0.0.1 -N -e 'SELECT 1' >/dev/null" >/dev/null 2>&1; then
      return 0
    fi
    if docker compose -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -c "mariadb -uroot -p\"${dq_password}\" -h 127.0.0.1 -N -e 'SELECT 1' >/dev/null" >/dev/null 2>&1; then
      return 0
    fi
    if docker compose -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -c "mariadb -u\"${MARIADB_USER}\" -p\"${dq_password}\" -h 127.0.0.1 -N -e 'SELECT 1' >/dev/null" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
    tries=$((tries - 1))
  done
  return 1
}

wait_for_mariadb || { echo "MariaDB is not ready." >&2; exit 1; }

DQ_MARIADB_PASSWORD="$(sh_escape_dq "${MARIADB_PASSWORD}")"
SQL_MARIADB_PASSWORD="$(mysql_escape_sq "${MARIADB_PASSWORD}")"

if docker compose -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -lc "mariadb -uroot -p\"${DQ_MARIADB_PASSWORD}\" -h 127.0.0.1 -N -e \"CREATE DATABASE IF NOT EXISTS algo_trader; CREATE DATABASE IF NOT EXISTS algo_trader_backtest; CREATE USER IF NOT EXISTS 'algo_trader'@'%' IDENTIFIED BY '${SQL_MARIADB_PASSWORD}'; CREATE USER IF NOT EXISTS 'algo_trader_backtest'@'%' IDENTIFIED BY '${SQL_MARIADB_PASSWORD}'; GRANT ALL PRIVILEGES ON algo_trader.* TO 'algo_trader'@'%'; GRANT ALL PRIVILEGES ON algo_trader_backtest.* TO 'algo_trader_backtest'@'%'; FLUSH PRIVILEGES;\"" >/dev/null 2>&1; then
  :
else
  echo "Warning: root database initialization was skipped, likely because the persisted root password differs." >&2
fi

SQL_FILE="${ROOT_DIR}/algo_trader.sql"
if [ -f "${SQL_FILE}" ]; then
  if docker compose -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -lc "mariadb -uroot -p\"${DQ_MARIADB_PASSWORD}\" -h 127.0.0.1 algo_trader -N -e 'SELECT 1'" >/dev/null 2>&1; then
    docker compose -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -lc "mariadb -uroot -p\"${DQ_MARIADB_PASSWORD}\" -h 127.0.0.1 algo_trader" <"${SQL_FILE}" || echo "Warning: root SQL import failed." >&2
  else
    docker compose -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -lc "mariadb -u\"${MARIADB_USER}\" -p\"${DQ_MARIADB_PASSWORD}\" -h 127.0.0.1 algo_trader" <"${SQL_FILE}" || echo "Warning: app-user SQL import failed." >&2
  fi
else
  echo "SQL file not found: ${SQL_FILE}; skipping import."
fi

REDIS_URL="redis://:${REDIS_PASSWORD}@redis:6379/0"
BACKTEST_REDIS_URL="redis://:${REDIS_PASSWORD}@redis:6379/8"
MARIADB_URL="mariadb://algo_trader:${MARIADB_PASSWORD}@mariadb:3306/algo_trader"
BACKTEST_MARIADB_URL="mariadb://algo_trader_backtest:${MARIADB_PASSWORD}@mariadb:3306/algo_trader_backtest"

update_env_line "${ROOT_DIR}/.env" REDIS_URL "$REDIS_URL"
update_env_line "${ROOT_DIR}/.env" MARIADB_URL "$MARIADB_URL"
update_env_line "${ROOT_DIR}/.env" BACKTEST_REDIS_URL "$BACKTEST_REDIS_URL"
update_env_line "${ROOT_DIR}/.env" BACKTEST_MARIADB_URL "$BACKTEST_MARIADB_URL"
update_env_line "${ROOT_DIR}/.env" ADMIN_USERNAME "ati-guest"
update_env_line "${ROOT_DIR}/.env" ADMIN_PASSWORD "$ADMIN_PASSWORD"
update_env_line "${ROOT_DIR}/.env" JWT_SECRET "$JWT_SECRET"
update_env_line "${ROOT_DIR}/.env" ALLOW_ANONYMOUS_ACCESS "false"
update_env_line "${ROOT_DIR}/.env" BROKER_ADAPTER_MODE "$BROKER_ADAPTER_MODE"
update_env_line "${ROOT_DIR}/.env" BROKER_RUNNER_URL "http://broker-runner-service:8115"
update_env_line "${ROOT_DIR}/.env" ACCOUNT_BROKER_RUNNER_URL "http://broker-runner-service:8115"
update_env_line "${ROOT_DIR}/.env" ORDERS_BROKER_RUNNER_URL "http://broker-runner-service:8115"
update_env_line "${ROOT_DIR}/.env" MARKET_DATA_BROKER_RUNNER_URL "http://broker-runner-service:8115"
update_env_line "${ROOT_DIR}/.env" APP_DOCS_URL ""
update_env_line "${ROOT_DIR}/.env" APP_REDOC_URL ""
update_env_line "${ROOT_DIR}/.env" APP_OPENAPI_URL ""

if [ "$BROKER_ADAPTER_MODE" = "ib" ]; then
  update_env_line "${ROOT_DIR}/.env" BROKER_RUNNER_ADAPTER_ENTRYPOINT "src.broker_adapters.ibkr_paper:create_adapter"
  update_env_line "${ROOT_DIR}/.env" BROKER_RUNNER_IB_GATEWAY_HOST "ib-gateway"
  update_env_line "${ROOT_DIR}/.env" BROKER_RUNNER_IB_GATEWAY_PORT "4004"
  update_env_line "${ROOT_DIR}/.env" BROKER_RUNNER_IB_CLIENT_ID "40"
  update_env_line "${ROOT_DIR}/.env" BROKER_RUNNER_IB_READ_ONLY "false"
else
  update_env_line "${ROOT_DIR}/.env" BROKER_RUNNER_ADAPTER_ENTRYPOINT "src.broker_adapters.sim:create_adapter"
fi

(
  cd "${ROOT_DIR}"
  docker compose -f docker-compose.yml pull
  docker compose -f docker-compose.yml up -d
)

if wait_for_http "$APP_URL" 90; then
  open_browser
  echo "Done. Open ${APP_URL} and log in with:"
else
  echo "Services started, but the frontend did not become ready yet. Open ${APP_URL} after a minute."
  echo "Login:"
fi
echo "  username: ati-guest"
echo "  password: the ATI web password you entered"
