#!/usr/bin/env bash
# Explicit offline replay; never a Studio claim/permit or trading operation.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.local-tools.env"
[[ -f "$ENV_FILE" ]] || { echo 'Install local offline tools first.' >&2; exit 1; }
# Host file arguments are mapped individually read-only. No arbitrary Docker flags.
args=(); mounts=(); output_seen=0
while (( $# )); do
  key="$1"; shift
  case "$key" in
    --workflow|--execution|--instrument|--data-root|--output)
      (( $# )) || { echo "Missing value for $key" >&2; exit 2; }
      host="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$1")"; shift
      case "$key" in
        --output)
          [[ ! -e "$host" && -d "$(dirname "$host")" && -w "$(dirname "$host")" ]] || { echo 'Output must be a new file in an existing writable directory.' >&2; exit 2; }
          mounts+=(--mount "type=bind,src=$(dirname "$host"),dst=/output")
          args+=(--output "/output/$(basename "$host")"); output_seen=1 ;;
        --data-root)
          [[ -d "$host" && -r "$host" ]] || { echo 'Readable data directory required.' >&2; exit 2; }
          mounts+=(--mount "type=bind,src=$host,dst=/data,readonly"); args+=(--data-root /data) ;;
        *)
          [[ -f "$host" && -r "$host" ]] || { echo "Readable input required: $key" >&2; exit 2; }
          target="/inputs/${key#--}.json"; mounts+=(--mount "type=bind,src=$host,dst=$target,readonly"); args+=("$key" "$target") ;;
      esac ;;
    --symbol|--timeframe|--start|--end|--context-start|--batch-size)
      (( $# )) || { echo "Missing value for $key" >&2; exit 2; }; args+=("$key" "$1"); shift ;;
    --help) args+=(--help) ;;
    *) echo "Unknown option: $key" >&2; exit 2 ;;
  esac
done
IMAGE="$(docker compose --env-file "$ENV_FILE" -f "$ROOT_DIR/docker-compose.local-tools.yml" config --images)"
[[ -n "$IMAGE" && "$IMAGE" != *$'\n'* ]] || { echo 'Expected one local-tools image.' >&2; exit 1; }
exec docker run --rm --network none --user "$(id -u):$(id -g)" --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp:rw,nosuid,size=128m \
  "${mounts[@]}" "$IMAGE" "${args[@]}"
