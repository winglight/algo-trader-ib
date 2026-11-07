#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-install}"
case "$ACTION" in install|upgrade|status|stop) ;; *) echo 'Usage: install_research.sh [install|upgrade|status|stop]' >&2; exit 2 ;; esac
ENV_FILE="$ROOT_DIR/.research.env"
REQUESTED_IMAGE="${ATI_RESEARCH_IMAGE:-${ATI_UPDATE_IMAGE_TAG:+ghcr.io/winglight/algo-trader/ati-local-research:${ATI_UPDATE_IMAGE_TAG}}}"
# Configuration is data, never sourced. Unknown fields cannot execute commands.
PARSED_ENV="$(mktemp)"
trap 'rm -f "$PARSED_ENV"' EXIT
python3 - "$ENV_FILE" "$ROOT_DIR/.local-tools.env" "$ROOT_DIR/.trading-connector.env" > "$PARSED_ENV" <<'PY'
import shlex, sys
from pathlib import Path
saved = {}
for filename, allowed in (
    (sys.argv[3], {'ATI_MARKET_DATA_DIR'}),
    (sys.argv[1], {'ATI_ENVIRONMENT_DIR', 'ATI_CLOUD_GATEWAY_URL', 'ATI_RESEARCH_IMAGE', 'ATI_MARKET_DATA_DIR'}),
    (sys.argv[2], {'ATI_MARKET_DATA_DIR'}),
):
    path = Path(filename)
    fields = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        key, separator, value = line.partition('=')
        if not separator or key not in allowed:
            continue
        parts = shlex.split(value)
        if len(parts) != 1 or key in fields or any(c in parts[0] for c in ('\0', '\n', '\r')):
            raise SystemExit('Invalid saved research configuration')
        fields[key] = parts[0]
    saved.update(fields)
for key, value in saved.items():
    sys.stdout.write(key+'\0'+value+'\0')
PY
while IFS= read -r -d '' key && IFS= read -r -d '' value; do
  if [[ -z "${!key:-}" ]]; then export "$key=$value"; fi
done < "$PARSED_ENV"
rm -f "$PARSED_ENV"; trap - EXIT
export ATI_ENVIRONMENT_DIR="${ATI_ENVIRONMENT_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/ati/environment}"
export ATI_CLOUD_GATEWAY_URL="${ATI_CLOUD_GATEWAY_URL:-https://ati.broyustudio.com}"
export ATI_MARKET_DATA_DIR="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "${ATI_MARKET_DATA_DIR:-$ROOT_DIR/data/market-data}")"
export ATI_LOCAL_UID="$(id -u)" ATI_LOCAL_GID="$(id -g)"
export ATI_RESEARCH_IMAGE="${REQUESTED_IMAGE:-${ATI_RESEARCH_IMAGE:-ghcr.io/winglight/algo-trader/ati-local-research:latest}}"
COMPOSE=(docker compose --project-name ati-local-research --project-directory "$ROOT_DIR" --env-file /dev/null -f "$ROOT_DIR/docker-compose.research.yml")
if [[ "$ACTION" == status ]]; then "${COMPOSE[@]}" ps; exit; fi
# The lock remains held across shell exec, image preparation, connection and up.
if [[ -z "${ATI_CONNECTOR_INSTALL_LOCK_FD:-}" ]]; then
  exec python3 "$ROOT_DIR/scripts/connector_installer_lock.py" "$ATI_ENVIRONMENT_DIR" bash "${BASH_SOURCE[0]}" "$@"
fi
python3 "$ROOT_DIR/scripts/connector_installer_lock.py" "$ATI_ENVIRONMENT_DIR" --check
if [[ "$ACTION" == stop ]]; then "${COMPOSE[@]}" stop research-worker; exit; fi
mkdir -p -m 0755 "$ATI_MARKET_DATA_DIR"
# Pull only the reviewed standalone native runtime. No Cloud sources or services
# are installed here; no market-data download is part of installation.
"${COMPOSE[@]}" pull research-worker
python3 - "$ENV_FILE" <<'PY'
import os, shlex, sys, tempfile
from pathlib import Path
p=Path(sys.argv[1]); keys=('ATI_ENVIRONMENT_DIR','ATI_LOCAL_UID','ATI_LOCAL_GID','ATI_CLOUD_GATEWAY_URL','ATI_RESEARCH_IMAGE','ATI_MARKET_DATA_DIR')
fd,name=tempfile.mkstemp(prefix='.research.env.',dir=p.parent)
try:
    with os.fdopen(fd,'w') as f: f.write(''.join(k+'='+shlex.quote(os.environ[k])+'\n' for k in keys))
    os.replace(name,p)
finally:
    if os.path.exists(name): os.unlink(name)
PY
# A headless installation never reads a trading license or creates trading-enabled.
# For all-mode installs the already-authorized trading connector establishes that
# identity first; the verified single-writer handoff preserves its account grants.
python3 "$ROOT_DIR/scripts/connector_writer_handoff.py" "$ROOT_DIR" "$ATI_ENVIRONMENT_DIR"
