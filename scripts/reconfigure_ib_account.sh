#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ROOT_ENV="${ATI_ROOT_ENV_PATH:-${ROOT_DIR}/.env}"
MIDDLE_DIR="${ATI_MIDDLE_DIR:-${ROOT_DIR}/middle}"
MIDDLE_ENV="${ATI_MIDDLE_ENV_PATH:-${MIDDLE_DIR}/.env}"
ROOT_COMPOSE="${ATI_ROOT_COMPOSE_PATH:-${ROOT_DIR}/docker-compose.yml}"
MIDDLE_COMPOSE="${ATI_MIDDLE_COMPOSE_PATH:-${MIDDLE_DIR}/docker-compose.yml}"
DRY_RUN=0
MODE=""

usage() {
  echo "Usage: scripts/maintenance/reconfigure_ib_account.sh [--paper|--live] [--dry-run]" >&2
}

while (($#)); do
  case "$1" in
    --paper) MODE=paper ;;
    --live) MODE=live ;;
    --dry-run) DRY_RUN=1 ;;
    *) usage; exit 2 ;;
  esac
  shift
done
[ -n "$MODE" ] || { usage; exit 2; }
[ -f "$ROOT_ENV" ] && [ -f "$MIDDLE_ENV" ] || {
  echo "Root and middle environment files are required." >&2; exit 1;
}

if [ "$DRY_RUN" = "1" ]; then
  echo "Dry run: would gate trading, verify no strategies/orders/positions, back up deployment env,"
  echo "recreate the single IB Gateway, verify the expected account and first fund snapshot,"
  echo "rebind the deployment-managed IB Profile, then recreate long-lived consumers."
  exit 0
fi

read -r -p "Expected IB account ID: " EXPECTED_ACCOUNT
read -r -p "IB username: " IB_USERNAME
read -r -s -p "IB password: " IB_PASSWORD; echo
read -r -s -p "IB Gateway VNC password: " IB_VNC_PASSWORD; echo
[ -n "$EXPECTED_ACCOUNT" ] && [ -n "$IB_USERNAME" ] && [ -n "$IB_PASSWORD" ] && [ -n "$IB_VNC_PASSWORD" ] || {
  echo "All account and credential prompts are required." >&2; exit 1;
}
for VALUE in "$EXPECTED_ACCOUNT" "$IB_USERNAME" "$IB_PASSWORD" "$IB_VNC_PASSWORD"; do
  case "$VALUE" in
    *$'\n'*|*$'\r'*) echo "Credential values cannot contain newlines." >&2; exit 1 ;;
  esac
done

BACKUP_DIR="$(mktemp -d "${ROOT_DIR}/.ib-account-backup.XXXXXX")"
chmod 700 "$BACKUP_DIR"
cp -p "$ROOT_ENV" "$BACKUP_DIR/root.env"
cp -p "$MIDDLE_ENV" "$BACKUP_DIR/middle.env"
COMMITTED=0
GATE_ACQUIRED=0
ENV_CHANGED=0
GATE_ID="ib-reconfigure-$(date +%s)-$$"

compose_root() { docker compose --env-file "$ROOT_ENV" -f "$ROOT_COMPOSE" "$@"; }
compose_middle() { docker compose --env-file "$MIDDLE_ENV" -f "$MIDDLE_COMPOSE" "$@"; }
redis_command() {
  compose_middle exec -T redis sh -c \
    'export REDISCLI_AUTH="${REDIS_PASSWORD:-}"; exec redis-cli "$@"' sh "$@"
}

rollback() {
  local rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ] && [ "$COMMITTED" = "0" ] && [ "$ENV_CHANGED" = "1" ]; then
    echo "IB reconfiguration failed; restoring the previous deployment configuration." >&2
    cp -p "$BACKUP_DIR/root.env" "$ROOT_ENV"
    cp -p "$BACKUP_DIR/middle.env" "$MIDDLE_ENV"
    compose_middle --profile ib up -d --force-recreate ib-gateway >/dev/null 2>&1 || true
    compose_root up -d --force-recreate broker-runner-service account-service market-data-service >/dev/null 2>&1 || true
    if compose_root exec -T backend python - <<'PY' >/dev/null 2>&1
import json
import urllib.request
from dotenv import dotenv_values

token = str(dotenv_values("/app/.env").get("BROKER_RUNNER_ADMIN_TOKEN") or "")
request = urllib.request.Request(
    "http://broker-runner-service:8115/broker/admin/bootstrap/import",
    data=json.dumps({"operation": "rebind_active_deployment_account", "profile_id": "ibkr_paper"}).encode(),
    headers={
        "Content-Type": "application/json",
        "X-Broker-Admin-Token": token,
        "X-Service-Identity": "ib-account-maintenance-rollback",
    },
    method="POST",
)
with urllib.request.urlopen(request, timeout=30):
    pass
PY
    then
      redis_command DEL broker_runner:switch:gate >/dev/null 2>&1 || true
      echo "Previous IB connection and Profile binding restored." >&2
    else
      echo "Rollback could not verify the previous account; the trading gate remains for manual recovery." >&2
    fi
  elif [ "$rc" -ne 0 ] && [ "$GATE_ACQUIRED" = "1" ]; then
    redis_command DEL broker_runner:switch:gate >/dev/null 2>&1 || true
  fi
  if [ "$COMMITTED" = "1" ]; then rm -rf "$BACKUP_DIR"; fi
  exit "$rc"
}
trap rollback EXIT

# Keep a secret-free snapshot of the current deployment Profile alongside the
# env backups so operators can audit or manually recover the previous binding.
compose_root exec -T backend python - <<'PY' > "$BACKUP_DIR/ib-profile.json"
import json
import urllib.request
from dotenv import dotenv_values

token = str(dotenv_values("/app/.env").get("BROKER_RUNNER_ADMIN_TOKEN") or "")
request = urllib.request.Request(
    "http://broker-runner-service:8115/broker/admin/profiles/ibkr_paper",
    headers={
        "X-Broker-Admin-Token": token,
        "X-Service-Identity": "ib-account-maintenance",
    },
)
with urllib.request.urlopen(request, timeout=15) as response:
    print(json.dumps(json.load(response), sort_keys=True))
PY

# The persistent gate closes the race between preflight and env replacement.
GATE_RESULT="$(redis_command SET broker_runner:switch:gate \
  "{\"operation_id\":\"${GATE_ID}\",\"operation_type\":\"ib_account_reconfigure\"}" NX)"
[ "$GATE_RESULT" = "OK" ] || {
  echo "Another Broker switch or maintenance gate is active." >&2; exit 1;
}
GATE_ACQUIRED=1

echo "Running IB account change preflight..."
compose_root exec -T backend python - <<'PY'
import json
import urllib.request

def get(url):
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.load(response)

strategies = get("http://strategy-service:8104/strategies?compact=true")
items = strategies.get("strategies", strategies if isinstance(strategies, list) else [])
running = [item for item in items if str(item.get("status") or item.get("runtimeStatus") or "").lower() in {"running", "starting", "stopping"}]
if running:
    raise SystemExit("Running/transitioning strategies block IB account replacement")
open_orders = get("http://broker-runner-service:8115/broker/orders/open")
positions = get("http://broker-runner-service:8115/broker/account/positions")
if open_orders:
    raise SystemExit("Open or partially-filled Broker orders block IB account replacement")
if any(abs(float(item.get("position") or item.get("quantity") or 0)) > 0 for item in positions):
    raise SystemExit("Non-zero Broker positions block IB account replacement")
PY

atomic_env_set() {
  local path="$1" key="$2" value="$3" temporary escaped
  temporary="$(mktemp "${path}.tmp.XXXXXX")"
  chmod --reference="$path" "$temporary" 2>/dev/null || chmod 600 "$temporary"
  grep -v -E "^${key}=" "$path" > "$temporary" || true
  escaped="$value"
  escaped="${escaped//\'/\\\'}"
  printf "%s='%s'\n" "$key" "$escaped" >> "$temporary"
  mv "$temporary" "$path"
}

ENV_CHANGED=1
atomic_env_set "$MIDDLE_ENV" TWS_USERID "$IB_USERNAME"
atomic_env_set "$MIDDLE_ENV" TWS_PASSWORD "$IB_PASSWORD"
atomic_env_set "$MIDDLE_ENV" VNC_SERVER_PASSWORD "$IB_VNC_PASSWORD"
atomic_env_set "$ROOT_ENV" BROKER_RUNNER_IB_ACCOUNT "$EXPECTED_ACCOUNT"
atomic_env_set "$ROOT_ENV" BROKER_RUNNER_IB_GATEWAY_PORT "$([ "$MODE" = paper ] && echo 4004 || echo 4003)"

compose_middle --profile ib up -d --force-recreate --wait ib-gateway
redis_command SET broker_runner:active_adapter_id ibkr_paper >/dev/null
compose_root up -d --force-recreate --wait broker-runner-service

compose_root exec -T backend python - "$EXPECTED_ACCOUNT" <<'PY'
import json
import sys
import urllib.request
from dotenv import dotenv_values

expected = sys.argv[1]
with urllib.request.urlopen("http://broker-runner-service:8115/broker/account/summary", timeout=30) as response:
    rows = json.load(response)
accounts = {str(item.get("account") or "") for item in rows if item.get("account")}
if expected not in accounts:
    raise SystemExit("IB Gateway connected to an account other than the expected account")
request = urllib.request.Request(
    "http://broker-runner-service:8115/broker/admin/bootstrap/import",
    data=json.dumps({"operation": "rebind_active_deployment_account", "profile_id": "ibkr_paper"}).encode(),
    headers={
        "Content-Type": "application/json",
        "X-Broker-Admin-Token": str(
            dotenv_values("/app/.env").get("BROKER_RUNNER_ADMIN_TOKEN") or ""
        ),
        "X-Service-Identity": "ib-account-maintenance",
    },
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    profile = json.load(response)
state = profile.get("fund_state") or {}
if state.get("equity") is None or state.get("quality") not in {"good", "degraded"}:
    raise SystemExit("The replacement account did not produce its first valid fund snapshot")
PY

compose_root up -d --force-recreate --wait account-service market-data-service
redis_command DEL broker_runner:switch:gate >/dev/null
COMMITTED=1
echo "IB account replacement completed for the deployment-managed singleton Profile."
