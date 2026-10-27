#!/usr/bin/env bash
# Explicit user-run CLI; never invoked by the installer or webpage.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.local-tools.env"
[[ -f "$ENV_FILE" ]] || { echo 'Install the local runtime first; see docs/local-data-download.md.' >&2; exit 1; }
# Read only the non-secret data directory choice; do not source arbitrary shell.
DATA_DIR="$(python3 - "$ENV_FILE" "$ROOT_DIR" <<'PY'
import os, shlex, sys
from pathlib import Path
values={}
for line in Path(sys.argv[1]).read_text().splitlines():
    key, sep, value=line.partition('=')
    if sep and key=='ATI_MARKET_DATA_DIR':
        parts=shlex.split(value)
        if len(parts)!=1: raise SystemExit('Invalid market data directory configuration')
        values[key]=parts[0]
root=Path(os.environ.get('ATI_MARKET_DATA_DIR') or values.get('ATI_MARKET_DATA_DIR') or str(Path(sys.argv[2])/'data/market-data')).expanduser()
print(root.resolve())
PY
)"
[[ -d "$DATA_DIR" && -r "$DATA_DIR" && -w "$DATA_DIR" ]] || { echo 'The selected local data directory must already exist and be readable/writable by your user.' >&2; exit 1; }
IMAGE="$(docker compose --env-file "$ENV_FILE" -f "$ROOT_DIR/docker-compose.local-tools.yml" config --images)"
[[ -n "$IMAGE" && "$IMAGE" != *$'\n'* ]] || { echo 'Expected one installed local-tools image.' >&2; exit 1; }
# No environment identity/credentials volume, Docker socket or trading mounts.
args=(docker run --rm --user "$(id -u):$(id -g)" --read-only --cap-drop ALL
  --security-opt no-new-privileges:true --tmpfs /tmp:rw,nosuid,size=128m
  --add-host host.docker.internal:host-gateway
  --volume "$DATA_DIR:/app/data/market-data:rw"
  --env APCA_API_KEY_ID --env APCA_API_SECRET_KEY --env DATABENTO_API_KEY
  --entrypoint python)
if [[ -t 0 && -t 1 ]]; then args+=(-it); else args+=(-i); fi
extra=(--ib-host host.docker.internal)
if [[ -n "${ATI_IB_CONTRACTS_FILE:-}" ]]; then
  [[ -f "$ATI_IB_CONTRACTS_FILE" && -r "$ATI_IB_CONTRACTS_FILE" ]] || { echo 'IB contract file is not readable.' >&2; exit 1; }
  contracts="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$ATI_IB_CONTRACTS_FILE")"
  args+=(--volume "$contracts:/run/ati-ib-contracts.json:ro")
  extra+=(--ib-contracts /run/ati-ib-contracts.json)
fi
exec "${args[@]}" "$IMAGE" -m ati_shared_sdk.common.market_data.local_download \
  --output-dir /app/data/market-data "${extra[@]}" "$@"
