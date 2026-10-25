#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ATI_ENVIRONMENT_DIR="${ATI_ENVIRONMENT_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/ati/environment}"
export ATI_LOCAL_UID="$(id -u)"
export ATI_LOCAL_GID="$(id -g)"
export ATI_CLOUD_GATEWAY_URL="${ATI_CLOUD_GATEWAY_URL:-https://ati.broyustudio.com}"
ACTION="${1:-install}"
REQUESTED_RESEARCH_IMAGE="${ATI_RESEARCH_IMAGE:-}"
ENV_FILE="$ROOT_DIR/.research.env"
if [[ -f "$ENV_FILE" ]]; then
  # This installer owns this non-secret key=value file.
  set -a
  source "$ENV_FILE"
  set +a
fi
if [[ -n "$REQUESTED_RESEARCH_IMAGE" ]]; then
  export ATI_RESEARCH_IMAGE="$REQUESTED_RESEARCH_IMAGE"
fi
case "$ACTION" in install|upgrade|status|stop) ;; *) echo 'Usage: install_research.sh [install|upgrade|status|stop]' >&2; exit 2 ;; esac
mkdir -p "$ATI_ENVIRONMENT_DIR/trading"
if [[ "$ACTION" == status ]]; then
  docker compose -f "$ROOT_DIR/docker-compose.research.yml" ps
  exit
fi
if [[ "$ACTION" == stop ]]; then
  docker compose -f "$ROOT_DIR/docker-compose.research.yml" stop research-worker
  exit
fi
# A source checkout builds the same distributable used by release installations.
if [[ -f "$ROOT_DIR/../cloud/docker/docker-compose.local-research-build.yml" && -z "${ATI_RESEARCH_IMAGE:-}" ]]; then
  export ATI_RESEARCH_IMAGE=ati-local-research:dev
  docker compose -f "$ROOT_DIR/../cloud/docker/docker-compose.local-research-build.yml" build local-research-image
elif [[ "$ACTION" == upgrade ]] || ! docker image inspect "${ATI_RESEARCH_IMAGE:-ghcr.io/winglight/algo-trader/ati-local-research:latest}" >/dev/null 2>&1; then
  docker compose -f "$ROOT_DIR/docker-compose.research.yml" pull research-worker
fi
# Persist non-secret deployment choices so later compose operations use the
# same environment identity. Credentials stay only inside the protected state.
python3 - "$ROOT_DIR/.research.env" <<'PY'
import os, sys
from pathlib import Path
keys=('ATI_ENVIRONMENT_DIR','ATI_LOCAL_UID','ATI_LOCAL_GID','ATI_CLOUD_GATEWAY_URL','ATI_RESEARCH_IMAGE')
Path(sys.argv[1]).write_text(''.join(k+'='+os.environ[k]+'\n' for k in keys if k in os.environ))
PY
COMPOSE=(docker compose --env-file "$ROOT_DIR/.research.env" -f "$ROOT_DIR/docker-compose.research.yml")
if [[ "$ACTION" == install ]]; then
  # Import an existing trading installation once. Capture identity privately;
  # never echo the legacy runtime token or installation secret.
  python3 - "$ROOT_DIR" "$ATI_ENVIRONMENT_DIR" <<'PY'
import json, os, secrets, subprocess, sys
from pathlib import Path
root, directory=map(Path,sys.argv[1:])
identity=directory/'installation.json'
if not identity.exists():
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
  "${COMPOSE[@]}" run --rm -T --user "$ATI_LOCAL_UID:$ATI_LOCAL_GID" research-worker connect --no-browser \
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
"${COMPOSE[@]}" up -d --no-deps research-worker
if [[ -f "$ATI_ENVIRONMENT_DIR/trading-enabled" ]]; then
  # Give the single grant writer a chance to publish before opening trading UI.
  for ((attempt=0; attempt<20; attempt++)); do
    [[ -f "$ATI_ENVIRONMENT_DIR/trading/runtime-grant.json" ]] && break
    sleep 1
  done
fi
echo 'Research service installed. Select this environment in Cloud Studio execution settings.'
