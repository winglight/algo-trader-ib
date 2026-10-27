#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export ATI_LOCAL_UID="$(id -u)"
export ATI_LOCAL_GID="$(id -g)"

ACTION="${1:-install}"
REQUESTED_CONNECTOR_COMPONENT=TRADING
REQUESTED_CONNECTOR_IMAGE="${ATI_TRADING_CONNECTOR_IMAGE:-${ATI_UPDATE_IMAGE_TAG:+ghcr.io/winglight/algo-trader/ati-trading-connector:${ATI_UPDATE_IMAGE_TAG}}}"

ENV_FILE="$ROOT_DIR/.trading-connector.env"
# Parse only installer-owned non-secret fields; never execute a saved env file.
PARSED_ENV="$(mktemp)"
trap 'rm -f "$PARSED_ENV"' EXIT
python3 - "$ENV_FILE" > "$PARSED_ENV" <<'PYENV'
import shlex,sys
from pathlib import Path
allowed={'ATI_ENVIRONMENT_DIR','ATI_LOCAL_UID','ATI_LOCAL_GID','ATI_CLOUD_GATEWAY_URL','ATI_TRADING_CONNECTOR_IMAGE','ATI_MARKET_DATA_DIR'}
p=Path(sys.argv[1])
for line in p.read_text().splitlines() if p.exists() else []:
    k,sep,v=line.partition('=')
    if sep and k in allowed:
        parts=shlex.split(v)
        if len(parts)!=1: raise SystemExit('Invalid saved connector configuration')
        sys.stdout.write(k+'\0'+parts[0]+'\0')
PYENV
while IFS= read -r -d '' key && IFS= read -r -d '' value; do
  if [[ -z "${!key:-}" ]]; then export "$key=$value"; fi
done < "$PARSED_ENV"
rm -f "$PARSED_ENV"
trap - EXIT
export ATI_ENVIRONMENT_DIR="${ATI_ENVIRONMENT_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/ati/environment}"
export ATI_CLOUD_GATEWAY_URL="${ATI_CLOUD_GATEWAY_URL:-https://ati.broyustudio.com}"
export ATI_MARKET_DATA_DIR="${ATI_MARKET_DATA_DIR:-$ROOT_DIR/data/market-data}"
COMPOSE=(docker compose)
[[ ! -f "$ENV_FILE" ]] || COMPOSE+=(--env-file "$ENV_FILE")
COMPOSE+=(-f "$ROOT_DIR/docker-compose.trading-connector.yml")
export ATI_CONNECTOR_COMPONENT="${REQUESTED_CONNECTOR_COMPONENT}"
case "$ATI_CONNECTOR_COMPONENT" in TRADING) ;; *) echo "Invalid connector component" >&2; exit 2 ;; esac
if [[ -n "$REQUESTED_CONNECTOR_IMAGE" ]]; then
  export ATI_TRADING_CONNECTOR_IMAGE="$REQUESTED_CONNECTOR_IMAGE"
fi
case "$ACTION" in install|upgrade|status|stop) ;; *) echo 'Usage: install_trading_connector.sh [install|upgrade|status|stop]' >&2; exit 2 ;; esac
if [[ "$ACTION" == status ]]; then
  "${COMPOSE[@]}" ps
  exit
fi
if [[ "$ACTION" == stop ]]; then
  "${COMPOSE[@]}" stop trading-connector
  exit
fi
if [[ -f "$ROOT_DIR/.research.env" ]]; then
  python3 "$ROOT_DIR/scripts/verify_existing_connector.py" "$ROOT_DIR" "$ATI_ENVIRONMENT_DIR"
  exit $?
fi
mkdir -p "$ATI_ENVIRONMENT_DIR" "$ATI_MARKET_DATA_DIR"
mkdir -p "$ATI_ENVIRONMENT_DIR/trading"
touch "$ATI_ENVIRONMENT_DIR/trading-enabled"
# Release installs only pull the separately packaged connector, never Cloud sources.
if [[ "$ACTION" == upgrade ]] || ! docker image inspect "${ATI_TRADING_CONNECTOR_IMAGE:-ghcr.io/winglight/algo-trader/ati-trading-connector:latest}" >/dev/null 2>&1; then
  "${COMPOSE[@]}" pull trading-connector
fi
# Persist non-secret deployment choices so later compose operations use the
# same environment identity. Credentials stay only inside the protected state.
python3 - "$ROOT_DIR/.trading-connector.env" <<'PY'
import os, sys, shlex
from pathlib import Path
keys=('ATI_ENVIRONMENT_DIR','ATI_LOCAL_UID','ATI_LOCAL_GID','ATI_CLOUD_GATEWAY_URL','ATI_TRADING_CONNECTOR_IMAGE','ATI_MARKET_DATA_DIR','ATI_CONNECTOR_COMPONENT')
Path(sys.argv[1]).write_text(''.join(k+'='+shlex.quote(os.environ[k])+'\n' for k in keys if k in os.environ))
PY
COMPOSE=(docker compose --env-file "$ROOT_DIR/.trading-connector.env" -f "$ROOT_DIR/docker-compose.trading-connector.yml")
# A previous failed connection may have left .trading-connector.env behind. Retry
# connection whenever credentials are absent; never call an unbound worker ready.
NEEDS_CONNECT="$(python3 - "$ATI_ENVIRONMENT_DIR/installation.json" <<'PY_CHECK'
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
print('0' if p.exists() and json.loads(p.read_text()).get('credential') else '1')
PY_CHECK
)"
if [[ "$ACTION" == install || "$NEEDS_CONNECT" == 1 ]]; then
  # Import an existing trading installation once. Capture identity privately;
  # never echo the legacy runtime token or installation secret.
  python3 - "$ROOT_DIR" "$ATI_ENVIRONMENT_DIR" <<'PY'
import json, os, secrets, subprocess, sys
from pathlib import Path
root, directory=map(Path,sys.argv[1:])
identity=directory/'installation.json'
if not identity.exists() and os.environ.get("ATI_RESEARCH_IMPORT_TRADING", "1") == "1":
    command=['docker','compose','-f',str(root/'docker-compose.yml'),'exec','-T','backend','python','-c',
        'import json; from ati_shared_sdk.common.license_state import LocalLicenseManager; print(json.dumps(LocalLicenseManager().load_state()))']
    result=subprocess.run(command,cwd=root,capture_output=True,text=True)
    if result.returncode==0:
        state=json.loads(result.stdout)
        value=dict(installId=state['install_id'],fingerprint=state['machine_fingerprint_hash'],
                   proof=state['install_secret'])
        runtime=(state.get('cloud_authorization') or {}).get('runtime_access_token')
        if runtime: value['migrationRuntimeToken']=runtime
        identity.write_text(json.dumps(value)); identity.chmod(0o600)
        (directory/'trading-enabled').touch()
PY
  # The container cannot open a host browser. Print one cloud URL, and have the
  # installation launcher open that URL on the host exactly once.
  "${COMPOSE[@]}" run --rm -T --user "$ATI_LOCAL_UID:$ATI_LOCAL_GID" trading-connector connect --no-browser \
    --gateway "$ATI_CLOUD_GATEWAY_URL" --state-dir /var/lib/ati-environment &
  BIND_PID=$!
  python3 - "$ATI_ENVIRONMENT_DIR/installation.json" <<'PY'
import json, sys, time, webbrowser
from pathlib import Path
path=Path(sys.argv[1])
for _ in range(120):
    value=json.loads(path.read_text()) if path.exists() else {}
    if value.get('credential'):
        break
    if value.get('bindingUrl'):
        print('Bind this environment:',value['bindingUrl'],flush=True)
        webbrowser.open(value['bindingUrl'])
        break
    time.sleep(1)
PY
  wait "$BIND_PID"
fi
"${COMPOSE[@]}" up -d --no-deps trading-connector
if [[ -f "$ATI_ENVIRONMENT_DIR/trading-enabled" ]]; then
  # Give the single grant writer a chance to publish before opening trading UI.
  for ((attempt=0; attempt<20; attempt++)); do
    [[ -f "$ATI_ENVIRONMENT_DIR/trading/runtime-grant.json" ]] && break
    sleep 1
  done
fi
echo 'Trading environment connector installed; research task claiming is disabled.'
