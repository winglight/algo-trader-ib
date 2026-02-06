#!/usr/bin/env python3
"""Generate an environment file for the Market Data service configuration."""

from __future__ import annotations

import argparse
import sys
import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import re
from urllib.parse import urlparse
from scripts.env._env_shared import (
    apply_overrides,
    apply_root_env_overrides,
    load_env_file,
    load_shared_env_values,
    render_env_template,
    set_value_from_template,
)
from src.market_data.config import (
    load_market_data_api_settings,
    load_market_data_example_defaults,
    load_market_data_settings,
)

from src.market_data.config import MARKET_CHANNEL_ENV_VARS, resolve_market_data_channels


TEMPLATE_PATH = ROOT_DIR / "config/market_data_service.env.example"


def _serialise_cors(values: tuple[str, ...]) -> str:
    if not values:
        return "*"
    return ",".join(values)


def _default_content() -> str:
    template_values = load_env_file(TEMPLATE_PATH, keep_quotes=True)
    template_defaults = load_env_file(TEMPLATE_PATH, keep_quotes=False)
    config: dict[str, str] = dict(template_values)

    env = dict(os.environ)
    project_env = load_env_file(ROOT_DIR / ".env", keep_quotes=False)
    base_defaults: dict[str, str] = {}
    base_defaults.update(load_market_data_example_defaults())
    base_defaults.update(template_defaults)
    base_defaults.update(project_env)
    shared_market_env = load_shared_env_values(
        (*MARKET_CHANNEL_ENV_VARS, "MARKET_DATA_PATH"), prefer_env=False
    )
    base_defaults.update({k: v for k, v in shared_market_env.items() if v})

    for key, value in base_defaults.items():
        if key not in env and value is not None and value != "":
            env[key] = value

    api_settings = load_market_data_api_settings(env)
    market_channels = resolve_market_data_channels(env)
    env.update(market_channels)
    service_settings = load_market_data_settings(env)

    computed_values = {
        "MARKET_DATA_APP_NAME": api_settings.app_name,
        "MARKET_DATA_APP_VERSION": api_settings.version,
        "MARKET_DATA_APP_DEBUG": api_settings.debug,
        "MARKET_DATA_DOCS_URL": api_settings.docs_url or "/docs",
        "MARKET_DATA_REDOC_URL": api_settings.redoc_url or "/docs",
        "MARKET_DATA_OPENAPI_URL": api_settings.openapi_url,
        "MARKET_DATA_REQUEST_ID_HEADER": api_settings.request_id_header,
        "MARKET_DATA_DEFAULT_TIMEFRAME": api_settings.default_timeframe,
        "MARKET_DATA_CORS_ENABLED": api_settings.cors_enabled,
        "MARKET_DATA_CORS_ALLOW_ORIGINS": _serialise_cors(
            api_settings.cors_allow_origins
        ),
        "MARKET_DATA_CORS_ALLOW_METHODS": _serialise_cors(
            api_settings.cors_allow_methods
        ),
        "MARKET_DATA_CORS_ALLOW_HEADERS": _serialise_cors(
            api_settings.cors_allow_headers
        ),
        "MARKET_DATA_REDIS_CHANNEL_PREFIX": market_channels[
            "MARKET_DATA_REDIS_CHANNEL_PREFIX"
        ],
        "MARKET_DATA_REDIS_DOM_CHANNEL": market_channels[
            "MARKET_DATA_REDIS_DOM_CHANNEL"
        ],
        "MARKET_DATA_REDIS_TICKER_CHANNEL": market_channels[
            "MARKET_DATA_REDIS_TICKER_CHANNEL"
        ],
        "MARKET_DATA_REDIS_BAR_CHANNEL": market_channels[
            "MARKET_DATA_REDIS_BAR_CHANNEL"
        ],
        "MARKET_DATA_DEFAULT_DEPTH_ROWS": service_settings.default_depth_rows,
        "MARKET_DATA_DOM_THROTTLE_SECONDS": service_settings.dom_throttle_seconds,
        "MARKET_DATA_PRICE_THROTTLE_SECONDS": service_settings.price_throttle_seconds,
        "MARKET_DATA_BAR_THROTTLE_SECONDS": service_settings.bar_throttle_seconds,
        "MARKET_DATA_AUTO_RESTART": service_settings.auto_restart,
        "MARKET_DATA_RESTART_DELAY_SECONDS": service_settings.restart_delay_seconds,
        "MARKET_DATA_DEFAULT_BAR_SIZE": service_settings.default_bar_size,
        "MARKET_DATA_DEFAULT_BAR_DURATION": service_settings.default_bar_duration,
        "MARKET_DATA_DEFAULT_BAR_WHAT_TO_SHOW": service_settings.default_bar_what_to_show,
        "MARKET_DATA_DEFAULT_BAR_USE_RTH": service_settings.default_bar_use_rth,
        "MARKET_DATA_DEFAULT_STOCK_EXCHANGE": service_settings.default_stock_exchange,
        "MARKET_DATA_DEFAULT_STOCK_CURRENCY": service_settings.default_stock_currency,
        "MARKET_DATA_DEFAULT_FOREX_EXCHANGE": service_settings.default_forex_exchange,
        "MARKET_DATA_DEFAULT_FOREX_CURRENCY": service_settings.default_forex_currency,
        "MARKET_DATA_DEFAULT_FUTURE_EXCHANGE": service_settings.default_future_exchange,
        "MARKET_DATA_DEFAULT_FUTURE_CURRENCY": service_settings.default_future_currency,
        "MARKET_DATA_DEFAULT_INDEX_EXCHANGE": service_settings.default_index_exchange,
        "MARKET_DATA_DEFAULT_INDEX_CURRENCY": service_settings.default_index_currency,
        "MARKET_DATA_PERSISTENCE_ENABLED": service_settings.persistence.enabled,
        "MARKET_DATA_PERSISTENCE_BASE_PATH": service_settings.persistence.base_path,
        "MARKET_DATA_PERSISTENCE_BARS_SUBDIR": (
            service_settings.persistence.bars_subdirectory or ""
        ),
        "MARKET_DATA_PERSISTENCE_DOM_SUBDIR": service_settings.persistence.dom_subdirectory,
        "MARKET_DATA_PERSISTENCE_FLUSH_INTERVAL_SECONDS": service_settings.persistence.flush_interval_seconds,
        "MARKET_DATA_PERSISTENCE_MAX_BATCH_SIZE": service_settings.persistence.max_batch_size,
        "MARKET_DATA_PERSISTENCE_MAX_QUEUE_SIZE": service_settings.persistence.max_queue_size,
        "MARKET_DATA_SERVICE_REGISTRY_ENABLED": api_settings.service_registry_enabled,
        "MARKET_DATA_SERVICE_REGISTRY_NAME": api_settings.service_registry_name,
        "MARKET_DATA_SERVICE_REGISTRY_SCHEME": api_settings.service_registry_scheme,
        "MARKET_DATA_SERVICE_REGISTRY_HOST": api_settings.service_registry_host,
        "MARKET_DATA_SERVICE_REGISTRY_PORT": api_settings.service_registry_port,
        "MARKET_DATA_SERVICE_REGISTRY_URL": api_settings.service_registry_url or "",
        "MARKET_DATA_SERVICE_REGISTRY_KEY": api_settings.service_registry_key,
        "MARKET_DATA_SERVICE_REGISTRY_HEARTBEAT": api_settings.service_registry_heartbeat,
        "MARKET_DATA_SERVICE_REGISTRY_REGISTRATION_ATTEMPTS": api_settings.service_registry_registration_attempts,
        "MARKET_DATA_PATH": env.get("MARKET_DATA_PATH", config.get("MARKET_DATA_PATH", "")),
    }

    for key, value in computed_values.items():
        set_value_from_template(config, template_values, key, value)

    shared_env = load_shared_env_values(
        (
            "LOG_DIR",
            "LOG_LEVEL",
            "LOG_STREAM",
            "REDIS_URL",
            "IB_GATEWAY_HOST",
            "IB_GATEWAY_PORT",
            "IB_CLIENT_ID",
            "IB_CONNECT_TIMEOUT",
            "IB_READ_ONLY",
            "IB_ACCOUNT",
            "IB_MARKET_DATA_DEPTH_ROWS",
            "IB_MARKET_DATA_QUEUE_SIZE",
        ),
        keep_quotes=True,
        prefer_env=False,
    )
    apply_overrides(config, shared_env)

    apply_root_env_overrides(config)

    def _normalise_redis_url(url: str) -> str:
        p = urlparse(url)
        userinfo = ''
        if p.username:
            userinfo = p.username
        if p.password:
            userinfo = f"{userinfo}:{p.password}" if userinfo else f":{p.password}"
        netloc = f"{userinfo}@redis:6379" if userinfo else "redis:6379"
        path = p.path or "/0"
        scheme = "redis"
        return f"{scheme}://{netloc}{path}"

    def _normalise_mariadb_url(url: str) -> str:
        p = urlparse(url)
        scheme = p.scheme or "mysql"
        userinfo = ''
        if p.username:
            userinfo = p.username
        if p.password:
            userinfo = f"{userinfo}:{p.password}" if userinfo else f":{p.password}"
        netloc = f"{userinfo}@mariadb:3306" if userinfo else "mariadb:3306"
        path = p.path or "/algo_trader"
        return f"{scheme}://{netloc}{path}"

    content = render_env_template(TEMPLATE_PATH, config)
    base_env = load_env_file(ROOT_DIR / ".env", keep_quotes=True)
    env_redis = os.environ.get("REDIS_URL")
    if env_redis:
        if re.search(r"^REDIS_URL=", content, flags=re.MULTILINE):
            content = re.sub(r"^REDIS_URL=.*$", f"REDIS_URL={env_redis}", content, flags=re.MULTILINE)
        else:
            content = content.rstrip() + f"\nREDIS_URL={env_redis}\n"
    else:
        redis_url = base_env.get("REDIS_URL") or config.get("REDIS_URL")
        if redis_url:
            norm = _normalise_redis_url(redis_url)
            content = re.sub(r"^REDIS_URL=.*$", f"REDIS_URL={norm}", content, flags=re.MULTILINE)
    mariadb_url = base_env.get("MARIADB_URL") or config.get("MARIADB_URL")
    if mariadb_url:
        norm_db = _normalise_mariadb_url(mariadb_url)
        content = re.sub(r"^MARIADB_URL=.*$", f"MARIADB_URL={norm_db}", content, flags=re.MULTILINE)
    return content


def generate_market_data_env(path: Path, *, overwrite: bool = False) -> Path:
    if path.exists() and not overwrite:
        raise SystemExit(
            f"Refusing to overwrite existing file: {path}. Use --overwrite to replace it."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    content = _default_content()
    path.write_text(content, encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("config/market_data_service.env"),
        help="Path to the environment file to generate (default: config/market_data_service.env)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    args = parser.parse_args()

    path = generate_market_data_env(args.output, overwrite=args.overwrite)
    print(f"Generated market data service environment file at: {path}")


if __name__ == "__main__":
    main()
