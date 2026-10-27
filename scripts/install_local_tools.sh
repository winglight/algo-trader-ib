#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-install}"
case "$ACTION" in install|upgrade) ;; *) echo 'Use install or upgrade.' >&2; exit 2 ;; esac
export ATI_LOCAL_TOOLS_IMAGE="${ATI_LOCAL_TOOLS_IMAGE:-ghcr.io/winglight/algo-trader/ati-local-tools:${ATI_UPDATE_IMAGE_TAG:-latest}}"
# Preserve the prior local data directory without evaluating configuration as shell.
PREVIOUS_DATA="$(python3 - "$ROOT_DIR/.local-tools.env" <<'PYREAD'
import shlex,sys
from pathlib import Path
p=Path(sys.argv[1])
for line in p.read_text().splitlines() if p.exists() else []:
    k,sep,v=line.partition('=')
    if sep and k=='ATI_MARKET_DATA_DIR':
        words=shlex.split(v)
        if len(words)!=1: raise SystemExit('Invalid saved data directory')
        print(words[0]);break
PYREAD
)"
export ATI_MARKET_DATA_DIR="${ATI_MARKET_DATA_DIR:-${PREVIOUS_DATA:-$ROOT_DIR/data/market-data}}"
export ATI_LOCAL_UID="$(id -u)" ATI_LOCAL_GID="$(id -g)"
mkdir -p "$ATI_MARKET_DATA_DIR"
# Image acquisition only. No Cloud login, task claim, market download or service start.
docker pull "$ATI_LOCAL_TOOLS_IMAGE"
python3 - "$ROOT_DIR/.local-tools.env" <<'PYENV'
import os,shlex,sys
from pathlib import Path
keys=('ATI_LOCAL_TOOLS_IMAGE','ATI_MARKET_DATA_DIR','ATI_LOCAL_UID','ATI_LOCAL_GID')
Path(sys.argv[1]).write_text(''.join(k+'='+shlex.quote(os.environ[k])+'\n' for k in keys))
PYENV
echo 'Local offline tools installed (LOCAL_USER); Studio research task execution is not included.'
echo 'See docs/local-user-replay.md and docs/local-data-download.md.'
