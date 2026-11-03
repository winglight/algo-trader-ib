#!/usr/bin/env bash
# Historical local simulation through the same isolated SDK replay container.
set -euo pipefail
for arg in "$@"; do
  [[ "$arg" != --mode && "$arg" != --mode=* ]] || { echo 'Simulation mode is fixed by this entry point.' >&2; exit 2; }
done
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run_local_backtest.sh" --mode simulation "$@"
