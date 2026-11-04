#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/installer_python.sh"
ati_ensure_installer_python
ACTION="${1:-install}"
if [ "$#" -gt 0 ]; then shift; fi
python3 "$ROOT_DIR/scripts/agent_installation.py" "$ACTION" --root "$ROOT_DIR" "$@"

if [[ "$ACTION" == install || "$ACTION" == upgrade ]]; then
  source "$ROOT_DIR/scripts/component_modes.sh"
  ati_record_component "$ROOT_DIR" agent "$(ati_detect_components "$ROOT_DIR")"
fi
