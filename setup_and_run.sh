#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIDDLE_DIR="${ROOT_DIR}/middle"

copy_if_missing() {
  local src="$1" dst="$2"
  if [ ! -f "$dst" ]; then
    cp "$src" "$dst"
  fi
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

if ! command -v docker >/dev/null 2>&1; then
  if [ "$(uname -s)" = "Darwin" ]; then
    echo "未检测到 Docker。请在 macOS 安装 Docker Desktop：https://docs.docker.com/desktop/install/mac/" >&2
    exit 1
  else
    echo "Docker not found. Attempting to install..."
    INSTALL_SCRIPT="${ROOT_DIR}/scripts/install_docker.sh"
    if [ -f "$INSTALL_SCRIPT" ]; then
      chmod +x "$INSTALL_SCRIPT"
      "$INSTALL_SCRIPT"
      if ! command -v docker >/dev/null 2>&1; then
        echo "Docker installation failed or docker command still not found." >&2
        exit 1
      fi
    else
      echo "Docker is missing and install script not found at $INSTALL_SCRIPT" >&2
      exit 1
    fi
  fi
fi

require_cmd sed

# Determine cross-platform sed in-place flag and docker compose command
if [ "$(uname -s)" = "Darwin" ]; then
  # BSD sed (macOS) requires an explicit backup suffix argument for -i
  SED_I=(-i '')
else
  # GNU sed (Linux) supports -i without a backup suffix argument
  SED_I=(-i)
fi

resolve_compose() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD=(docker-compose)
  else
    echo "Missing docker compose/docker-compose command." >&2
    exit 1
  fi
}
resolve_compose

# Ensure current user can talk to Docker daemon; print actionable guidance otherwise
ensure_docker_access() {
  if docker info >/dev/null 2>&1; then
    return 0
  fi
  local err
  err="$(docker info 2>&1 || true)"
  if printf '%s' "$err" | grep -qi 'permission denied'; then
    echo "Docker 权限不足：当前用户无法访问 /var/run/docker.sock。" >&2
    echo "临时方案：使用 sudo 运行本脚本，例如：sudo \"$0\"" >&2
    echo "长期方案：将用户加入 docker 组并重新登录（或 newgrp docker）。" >&2
    echo "例如：sudo usermod -aG docker $USER；然后退出并重新登录后再运行。" >&2
  else
    echo "无法连接到 Docker 守护进程：请确认 Docker 已启动（Docker Desktop 或服务）。" >&2
    echo "可运行 \`docker info\` 观察具体错误；若使用 Docker Desktop，请先打开并启动。" >&2
  fi
  exit 1
}
ensure_docker_access

# 1) middle/.env from example
copy_if_missing "${MIDDLE_DIR}/.env.example" "${MIDDLE_DIR}/.env"

# 2) public/.env from example
copy_if_missing "${ROOT_DIR}/.env.example" "${ROOT_DIR}/.env"

prompt_secret() {
  local var="$1" prompt="$2"
  local value
  read -r -p "$prompt: " value
  printf '%s' "$value"
}

# Read key value from an env file (after first '=')
read_env_value() {
  local file="$1" key="$2"
  if [ -f "$file" ]; then
    grep -E "^${key}=" "$file" | tail -n 1 | cut -d'=' -f2-
  fi
}

# Use current middle/.env value if set and not default, else prompt
get_or_prompt() {
  local key="$1" prompt="$2"
  local current default
  current="$(read_env_value "${MIDDLE_DIR}/.env" "$key")"
  default="$(read_env_value "${MIDDLE_DIR}/.env.example" "$key")"
  if [ -n "$current" ] && [ "$current" != "$default" ]; then
    printf '%s' "$current"
  else
    prompt_secret "$key" "$prompt"
  fi
}

TWS_USERID="$(get_or_prompt TWS_USERID "Please input TWS_USERID")"
TWS_PASSWORD="$(get_or_prompt TWS_PASSWORD "Please input TWS_PASSWORD")"
VNC_SERVER_PASSWORD="$(get_or_prompt VNC_SERVER_PASSWORD "Please input VNC_SERVER_PASSWORD")"
REDIS_PASSWORD="$(get_or_prompt REDIS_PASSWORD "Please input REDIS_PASSWORD")"
MARIADB_PASSWORD="$(get_or_prompt MARIADB_PASSWORD "Please input MARIADB_PASSWORD")"

# Fixed MariaDB identifiers (per project defaults)
MARIADB_DATABASE="algo_trader"
MARIADB_USER="algo_trader"

# 4) update middle/.env (escape sed replacement to handle '&' and delimiter)
sed_escape_repl_pipe() {
  # Escape characters that have special meaning in sed replacement with '|' delimiter
  # Specifically: '&' (expands to match) and '|' (our delimiter)
  printf '%s' "$1" | sed -e 's/[|&]/\\&/g'
}

# Escape for double-quoted shell context inside container commands
# Escapes: \  $  " to avoid inner-shell expansion/capture
sh_escape_dq() {
  printf '%s' "$1" | sed -e 's/[\\$"]/\\&/g'
}

# Escape for SQL single-quoted string literal
# Escapes: backslash and single quote for MySQL default backslash-escape mode
mysql_escape_sq() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e "s/'/\\'/g"
}

SAFE_TWS_USERID="$(sed_escape_repl_pipe "$TWS_USERID")"
SAFE_TWS_PASSWORD="$(sed_escape_repl_pipe "$TWS_PASSWORD")"
SAFE_VNC_SERVER_PASSWORD="$(sed_escape_repl_pipe "$VNC_SERVER_PASSWORD")"
SAFE_REDIS_PASSWORD="$(sed_escape_repl_pipe "$REDIS_PASSWORD")"
SAFE_MARIADB_PASSWORD="$(sed_escape_repl_pipe "$MARIADB_PASSWORD")"

sed "${SED_I[@]}" \
  -e "s|^TWS_USERID=.*|TWS_USERID=${SAFE_TWS_USERID}|" \
  -e "s|^TWS_PASSWORD=.*|TWS_PASSWORD=${SAFE_TWS_PASSWORD}|" \
  -e "s|^VNC_SERVER_PASSWORD=.*|VNC_SERVER_PASSWORD=${SAFE_VNC_SERVER_PASSWORD}|" \
  -e "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=${SAFE_REDIS_PASSWORD}|" \
  -e "s|^MARIADB_PASSWORD=.*|MARIADB_PASSWORD=${SAFE_MARIADB_PASSWORD}|" "${MIDDLE_DIR}/.env"

# 5) start infra
(
  cd "${MIDDLE_DIR}"
  "${COMPOSE_CMD[@]}" up -d
)

# 6) init mariadb user/db and import SQL
wait_for_mariadb() {
  local tries=60
  local HEALTHCHECK_CNF="/var/lib/mysql/.my-healthcheck.cnf"
  while [ $tries -gt 0 ]; do
    # 1) Use healthcheck credentials if available (preferred)
    if "${COMPOSE_CMD[@]}" -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -c "[ -f '${HEALTHCHECK_CNF}' ] && mariadb --defaults-extra-file='${HEALTHCHECK_CNF}' -h 127.0.0.1 -N -e 'SELECT 1' >/dev/null" >/dev/null 2>&1; then
      return 0
    fi
    # 2) Try root user with provided password
    local DQ_MARIADB_PASSWORD
    DQ_MARIADB_PASSWORD="$(sh_escape_dq "${MARIADB_PASSWORD}")"
    if "${COMPOSE_CMD[@]}" -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -c "mariadb -uroot -p\"${DQ_MARIADB_PASSWORD}\" -h 127.0.0.1 -N -e 'SELECT 1' >/dev/null" >/dev/null 2>&1; then
      return 0
    fi
    # 3) Try app user
    if "${COMPOSE_CMD[@]}" -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -c "mariadb -u\"${MARIADB_USER}\" -p\"${DQ_MARIADB_PASSWORD}\" -h 127.0.0.1 -N -e 'SELECT 1' >/dev/null" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
    tries=$((tries-1))
  done
  return 1
}

wait_for_mariadb || { echo "MariaDB 未就绪" >&2; exit 1; }

DQ_MARIADB_PASSWORD="$(sh_escape_dq "${MARIADB_PASSWORD}")"
SQL_MARIADB_PASSWORD="$(mysql_escape_sq "${MARIADB_PASSWORD}")"

# Try root-based initialization; if it fails (e.g., root password mismatch due to persisted volume), continue gracefully
if "${COMPOSE_CMD[@]}" -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -lc "mariadb -uroot -p\"${DQ_MARIADB_PASSWORD}\" -h 127.0.0.1 -N -e \"CREATE DATABASE IF NOT EXISTS algo_trader; CREATE USER IF NOT EXISTS 'algo_trader'@'%' IDENTIFIED BY '${SQL_MARIADB_PASSWORD}'; GRANT ALL PRIVILEGES ON algo_trader.* TO 'algo_trader'@'%'; FLUSH PRIVILEGES;\"" >/dev/null 2>&1; then
  : # root init succeeded
else
  echo "警告：无法以 root 完成初始化，跳过（可能因持久化卷中的 root 密码不同）。" >&2
fi

# Import SQL using available credentials: prefer root, fall back to app user
SQL_FILE="${ROOT_DIR}/mariadb_init.sql"
if [ -f "${SQL_FILE}" ]; then
  if "${COMPOSE_CMD[@]}" -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -lc "mariadb -uroot -p\"${DQ_MARIADB_PASSWORD}\" -h 127.0.0.1 algo_trader -N -e 'SELECT 1'" >/dev/null 2>&1; then
    "${COMPOSE_CMD[@]}" -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -lc "mariadb -uroot -p\"${DQ_MARIADB_PASSWORD}\" -h 127.0.0.1 algo_trader" < "${SQL_FILE}" || echo "警告：root 导入 SQL 失败，可能因权限或密码不匹配。" >&2
  else
    "${COMPOSE_CMD[@]}" -f "${MIDDLE_DIR}/docker-compose.yml" exec -T mariadb sh -lc "mariadb -u\"${MARIADB_USER}\" -p\"${DQ_MARIADB_PASSWORD}\" -h 127.0.0.1 algo_trader" < "${SQL_FILE}" || echo "警告：应用用户导入 SQL 失败，可能因用户不存在或权限不足。" >&2
  fi
else
  echo "提示：未找到 SQL 文件 ${SQL_FILE}，跳过初始数据导入。"
fi

# 6) update public .env (REDIS_URL, MARIADB_URL)
update_env_line() {
  local file="$1" key="$2" value="$3"
  local escaped_value
  escaped_value="$(printf '%s' "$value" | sed -e 's/[|&]/\\&/g')"
  if grep -q "^${key}=" "$file"; then
    sed "${SED_I[@]}" -e "s|^${key}=.*|${key}=${escaped_value}|" "$file"
  else
    printf '\n%s=%s\n' "$key" "$value" >>"$file"
  fi
}

REDIS_URL="redis://:${REDIS_PASSWORD}@redis:6379/0"
MARIADB_URL="mariadb://algo_trader:${MARIADB_PASSWORD}@mariadb:3306/algo_trader"

update_env_line "${ROOT_DIR}/.env" REDIS_URL "$REDIS_URL"
update_env_line "${ROOT_DIR}/.env" MARIADB_URL "$MARIADB_URL"

# 7) run service stack from public/docker-compose.yml
SERVICE_COMPOSE_PUBLIC="${ROOT_DIR}/docker-compose.yml"
if [ -f "${SERVICE_COMPOSE_PUBLIC}" ]; then
  (
    cd "${ROOT_DIR}"
    "${COMPOSE_CMD[@]}" -f "${SERVICE_COMPOSE_PUBLIC}" up -d
  )
else
  echo "提示：未找到 public/docker-compose.yml，跳过服务栈启动。"
fi

echo "完成：中间件与服务容器已启动"
