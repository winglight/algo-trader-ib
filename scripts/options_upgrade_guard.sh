#!/usr/bin/env bash
set -euo pipefail

ROOT="$1"
TARGET_SQL="$2"
GUARD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# A current schema keeps the option authorities. Image compatibility and
# reproducible release locks are checked separately by the release process.
supports_options=1
schema_versions="$(sed -n 's/^-- ATI_OPTIONS_SCHEMA_VERSION: \([0-9][0-9]*\)$/\1/p' "$TARGET_SQL")"
if ! [[ "$schema_versions" =~ ^[1-9][0-9]*$ ]]; then
  supports_options=0
fi
for table in option_local_basket option_local_round option_protection_session account_exposure_claims option_order_group option_owned_lot option_protection_recovery broker_option_dispatch option_order_raw_inbox order_delivery_close option_local_grant option_account_snapshot option_account_cursor option_lifecycle_allocation; do
  if ! grep -Eq "^CREATE TABLE IF NOT EXISTS ${table} [(]" "$TARGET_SQL"; then
    supports_options=0
    break
  fi
done
[ "$supports_options" = 0 ] || exit 0

# An existing installation is inspected using its current image and network,
# before files are replaced or protection services are stopped.
[ -f "$ROOT/docker-compose.yml" ] || { echo "OPTIONS_DOWNGRADE_FACTS_UNAVAILABLE: existing Compose is missing" >&2; exit 1; }
if ! docker compose -f "$ROOT/docker-compose.yml" exec -T orders-service python3 - < "$GUARD_DIR/options_upgrade_guard.py"; then
  echo "OPTIONS_SCHEMA_DOWNGRADE_BLOCKED: keep current services running; pause entry and complete position/order reconciliation before retrying." >&2
  exit 1
fi
