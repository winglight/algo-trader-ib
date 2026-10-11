"""Read-only legacy-schema downgrade check, executed in the current Orders image."""

import asyncio
import json
import os
from pathlib import Path
from urllib.request import urlopen

from ati_shared_sdk.common.env_shared import load_env_file
from ati_shared_sdk.common.options.account_client import AccountEvidenceSnapshot
from ati_shared_sdk.common.options.models import ExecutionTarget
from ati_shared_sdk.database import DatabaseSettings, create_async_engine

# These are the original trading/recovery authorities, not a parallel ledger.
CHECKS = {
    "rounds": ("option_local_round", "state NOT IN ('CLOSED','CANCELED','BLOCKED') OR active_basket_id IS NOT NULL"),
    "protection": ("option_protection_session", "status<>'CLOSED'"),
    "reservations": ("account_exposure_claims", "owner_domain='OPTIONS' AND status<>'RELEASED'"),
    "groups": ("option_order_group", "status NOT IN ('FILLED','CANCELED','REJECTED','EXPIRED')"),
    "lots": ("option_owned_lot", "signed_remaining_contracts<>0 OR reserved_close_contracts<>0"),
    "recovery": ("option_protection_recovery", "phase<>'CLOSED'"),
    "unknown_dispatch": ("broker_option_dispatch", "phase<>'ACKNOWLEDGED'"),
    "unprocessed_events": ("option_order_raw_inbox", "processing_state<>'APPLIED'"),
    "delivery_orders": ("order_delivery_close", "phase<>'SETTLED' OR reserved_quantity<>0"),
    "daily_grants": ("option_local_grant", "JSON_VALUE(payload,'$.grant.status')='AVAILABLE' AND JSON_VALUE(payload,'$.grant.expires_at')>DATE_FORMAT(UTC_TIMESTAMP(6),'%Y-%m-%dT%H:%i:%s.%fZ')"),
}
REQUIRED_TABLES = {table for table, _ in CHECKS.values()} | {
    "option_local_basket", "option_account_snapshot", "option_account_cursor", "option_lifecycle_allocation",
}


async def readiness(sessions, *, account_key=None):
    """Inspect all accounts in production; scoped inspection supports the same integration flow."""
    params = {"key": account_key} if account_key else {}
    scope = " AND account_key=:key" if account_key else ""
    counts = {}
    async with sessions() as session:
        tables = {row["table_name"] for row in (await session.execute("SELECT table_name AS table_name FROM information_schema.tables WHERE table_schema=DATABASE()")).mappings().all()}
        present = tables & REQUIRED_TABLES
        if not present:
            return {"schema": "LEGACY", "ready": True, "counts": {}}
        if not REQUIRED_TABLES <= tables:
            return {"schema": "UNKNOWN", "ready": False, "reason": "OPTIONS_SCHEMA_INCOMPLETE", "counts": {}}
        for name, (table, predicate) in CHECKS.items():
            if name in {"rounds", "daily_grants"}:
                scoped = " AND basket_id IN (SELECT basket_id FROM option_local_basket WHERE account_key=:key)" if account_key else ""
            else:
                scoped = scope
            counts[name] = (await session.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE ({predicate}){scoped}", params)).mappings().one()["n"]
        # Current Account snapshots expose unowned native options too. Missing
        # or incomplete facts cannot be treated as an empty position list.
        snapshots = (await session.execute("""SELECT s.payload FROM option_account_snapshot s
            WHERE NOT EXISTS (SELECT 1 FROM option_account_snapshot newer
              WHERE newer.account_key=s.account_key AND newer.account_sequence>s.account_sequence)""" + scope, params)).mappings().all()
        counts["account_unknown"] = 0
        counts["native_positions"] = 0
        counts["account_missing"] = (await session.execute("""SELECT COUNT(*) AS n FROM option_account_cursor c
            WHERE NOT EXISTS (SELECT 1 FROM option_account_snapshot s WHERE s.account_key=c.account_key)""" +
            (" AND c.account_key=:key" if account_key else ""), params)).mappings().one()["n"]
        for row in snapshots:
            payload = row["payload"]
            value = json.loads(payload) if isinstance(payload, str) else payload
            evidence = AccountEvidenceSnapshot.model_validate(value).require_fresh()
            observation = evidence.observation
            if not evidence.complete or not observation.positions_complete or observation.unresolved_positions:
                counts["account_unknown"] += 1
            counts["native_positions"] += sum(position.signed_contracts != 0 for position in observation.positions)
        # Reuse the exact owned-delivery algorithm and settlement evidence;
        # historical corrections are replaced, never added together.
        from src.orders.delivery_close import delivery_balances
        targets = (await session.execute("SELECT DISTINCT account_key,JSON_EXTRACT(receipt,'$.request.context') AS target FROM option_lifecycle_allocation WHERE 1=1" + scope, params)).mappings().all()
        counts["delivery_lots"] = 0
        for row in targets:
            target = json.loads(row["target"]) if isinstance(row["target"], str) else row["target"]
            lots = await delivery_balances(session, ExecutionTarget.model_validate(target))
            counts["delivery_lots"] += sum(bool(lot.signed_remaining_shares or lot.reserved_close_shares) for lot in lots)
    return {"schema": "OPTIONS_V9", "ready": not any(counts.values()), "counts": counts}


def paused(url, field):
    with urlopen(url, timeout=3) as response:
        return json.load(response).get(field) is False


async def main():
    env = load_env_file(Path("/app/.env"))
    env.update(os.environ)
    engine = create_async_engine(DatabaseSettings.from_env(env=env))
    try:
        result = await readiness(engine.session_factory())
        if result["schema"] == "OPTIONS_V9":
            # The running processes must have disabled entry, not merely their
            # edited env files. This check runs before the installer stops them.
            stopped = paused("http://risk-service:8103/healthz", "options_new_entry_enabled") and paused("http://options-service:8118/health", "new_entry_enabled")
            if not stopped:
                result.update(ready=False, reason="OPTIONS_ENTRY_MUST_BE_PAUSED")
            else:
                result = await readiness(engine.session_factory())
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["ready"] else 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        # Never disclose database URLs, tokens or native account payloads.
        print(json.dumps({"ready": False, "reason": "OPTIONS_DOWNGRADE_FACTS_UNAVAILABLE", "error_type": type(exc).__name__}))
        raise SystemExit(1)
