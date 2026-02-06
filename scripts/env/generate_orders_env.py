#!/usr/bin/env python3
"""Generate an environment file for the Orders service configuration."""

from __future__ import annotations

import argparse
import sys
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
from src.orders.config import OrdersServiceSettings


TEMPLATE_PATH = ROOT_DIR / "config/orders_service.env.example"


def _default_content() -> str:
    template_values = load_env_file(TEMPLATE_PATH, keep_quotes=True)
    config: dict[str, str] = dict(template_values)

    orders_settings = OrdersServiceSettings()
    computed_values = {
        "ORDERS_APP_NAME": orders_settings.app_name,
        "ORDERS_APP_VERSION": orders_settings.version,
        "ORDERS_APP_DEBUG": orders_settings.debug,
        "ORDERS_DOCS_URL": orders_settings.docs_url or "/docs",
        "ORDERS_REDOC_URL": orders_settings.redoc_url or "/docs",
        "ORDERS_OPENAPI_URL": orders_settings.openapi_url,
        "ORDERS_REQUEST_ID_HEADER": orders_settings.request_id_header,
        "ORDERS_REDIS_CHANNEL_PREFIX": orders_settings.redis_channel_prefix,
        "ORDERS_REDIS_STATUS_CHANNEL": orders_settings.redis_status_channel,
        "ORDERS_REDIS_FILL_CHANNEL": orders_settings.redis_fill_channel,
        "ORDERS_DEFAULT_STOCK_EXCHANGE": orders_settings.default_stock_exchange,
        "ORDERS_DEFAULT_STOCK_CURRENCY": orders_settings.default_stock_currency,
        "ORDERS_DEFAULT_FUTURE_EXCHANGE": orders_settings.default_future_exchange,
        "ORDERS_DEFAULT_FUTURE_CURRENCY": orders_settings.default_future_currency,
        "ORDERS_DEFAULT_TIF": orders_settings.default_time_in_force,
        "ORDERS_ENABLE_STATUS_EVENTS": orders_settings.enable_status_events,
        "ORDERS_ENABLE_FILL_EVENTS": orders_settings.enable_fill_events,
        "ORDERS_SERVICE_REGISTRY_ENABLED": orders_settings.service_registry_enabled,
        "ORDERS_SERVICE_REGISTRY_NAME": orders_settings.service_registry_name,
        "ORDERS_SERVICE_REGISTRY_SCHEME": orders_settings.service_registry_scheme,
        "ORDERS_SERVICE_REGISTRY_HOST": orders_settings.service_registry_host,
        "ORDERS_SERVICE_REGISTRY_PORT": orders_settings.service_registry_port,
        "ORDERS_SERVICE_REGISTRY_URL": orders_settings.service_registry_url or "",
        "ORDERS_SERVICE_REGISTRY_KEY": orders_settings.service_registry_key,
        "ORDERS_SERVICE_REGISTRY_HEARTBEAT": orders_settings.service_registry_heartbeat,
        "ORDERS_SERVICE_REGISTRY_REGISTRATION_ATTEMPTS": orders_settings.service_registry_registration_attempts,
        "ORDERS_AUTO_CONFIGURE_IB": 1,
        "ORDERS_SYNC_TIMEOUT_SECONDS": orders_settings.sync_timeout_seconds,
    }

    for key, value in computed_values.items():
        set_value_from_template(config, template_values, key, value)

    shared_env = load_shared_env_values(
        (
            "LOG_DIR",
            "LOG_LEVEL",
            "LOG_STREAM",
            "REDIS_URL",
            "MARIADB_URL",
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
        # Preserve password and optional username if provided in netloc
        userinfo = ''
        if p.username:
            # Include username
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
    redis_url = base_env.get("REDIS_URL") or config.get("REDIS_URL")
    if redis_url:
        norm = _normalise_redis_url(redis_url)
        content = re.sub(r"^REDIS_URL=.*$", f"REDIS_URL={norm}", content, flags=re.MULTILINE)
    mariadb_url = base_env.get("MARIADB_URL") or config.get("MARIADB_URL")
    if mariadb_url:
        norm_db = _normalise_mariadb_url(mariadb_url)
        content = re.sub(r"^MARIADB_URL=.*$", f"MARIADB_URL={norm_db}", content, flags=re.MULTILINE)
    return content


def generate_orders_env(path: Path, *, overwrite: bool = False) -> Path:
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
        default=Path("config/orders_service.env"),
        help="Path to the environment file to generate (default: config/orders_service.env)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    args = parser.parse_args()

    path = generate_orders_env(args.output, overwrite=args.overwrite)
    print(f"Generated orders service environment file at: {path}")


if __name__ == "__main__":
    main()
