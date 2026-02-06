#!/usr/bin/env python3
"""Synchronise the project `.env` file with `.env.example` defaults."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.env._env_shared import load_env_file, render_env_template

TEMPLATE_PATH = ROOT_DIR / ".env.example"
OUTPUT_PATH = ROOT_DIR / ".env"

_LEGACY_APP_MANAGED_SERVICES = (
    "strategy|http://localhost:8104/healthz|./scripts/run/backend.sh restart strategy|"
    "logs/strategy_service/strategy_service.log|restart_mode=command"
)
_LEGACY_APP_MANAGED_SERVICES_WITH_UNDERSCORE = (
    "api|http://localhost:8000/healthz|./scripts/run/backend.sh restart api||restart_mode=command;"
    "account|http://localhost:8100/healthz|./scripts/run/backend.sh restart account||restart_mode=command;"
    "orders|http://localhost:8101/healthz|./scripts/run/backend.sh restart orders||restart_mode=command;"
    "market_data|http://localhost:8102/healthz|./scripts/run/backend.sh restart market_data||restart_mode=command;"
    "risk|http://localhost:8103/healthz|./scripts/run/backend.sh restart risk||restart_mode=command;"
    "strategy|http://localhost:8104/healthz|./scripts/run/backend.sh restart strategy||restart_mode=command"
)

# Keys that should follow new template defaults when the current value is a known
# legacy default produced by older templates.
_MIGRATION_FROM_LEGACY_DEFAULTS: dict[str, tuple[str, ...]] = {
    "APP_MANAGED_SERVICES": (
        _LEGACY_APP_MANAGED_SERVICES,
        _LEGACY_APP_MANAGED_SERVICES_WITH_UNDERSCORE,
    ),
}


def _apply_legacy_default_migrations(
    template_values: dict[str, str],
    current_values: dict[str, str],
) -> dict[str, str]:
    migrated = dict(current_values)
    for key, legacy_values in _MIGRATION_FROM_LEGACY_DEFAULTS.items():
        if key not in template_values:
            continue
        current_value = migrated.get(key)
        if current_value is None:
            continue
        if current_value.strip() in legacy_values:
            migrated.pop(key, None)
    return migrated


def generate_root_env(output_path: Path = OUTPUT_PATH) -> str:
    """Render the project `.env` file preserving local overrides."""

    template_values = load_env_file(TEMPLATE_PATH, keep_quotes=True)
    current_values = load_env_file(OUTPUT_PATH, keep_quotes=True)
    current_values = _apply_legacy_default_migrations(template_values, current_values)

    merged_values = dict(template_values)
    merged_values.update(current_values)

    rendered = render_env_template(TEMPLATE_PATH, merged_values)
    output_path.write_text(rendered, encoding="utf-8")
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Maintained for API parity with other generators; has no effect.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help="Output path for the generated environment file",
    )
    args = parser.parse_args(argv)

    generate_root_env(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
