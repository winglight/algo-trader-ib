#!/usr/bin/env python3
"""Generate an environment file for the Account service configuration."""

from __future__ import annotations

import argparse
import os
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
from src.account.config import AccountServiceSettings, load_account_settings
TEMPLATE_PATH = ROOT_DIR / "config/account_service.env.example"


def _account_settings_env() -> dict[str, str]:
    """Return environment values for ``AccountServiceSettings`` resolution."""

    root_env_values = load_env_file(ROOT_DIR / ".env")
    resolved: dict[str, str] = dict(root_env_values)

    for key, value in os.environ.items():
        if value is None or value == "":
            continue
        resolved[key] = value

    return resolved


def _default_content() -> str:
    account_settings_env = _account_settings_env()
    account_settings = (
        load_account_settings(account_settings_env)
        if account_settings_env
        else AccountServiceSettings()
    )
    template_values = load_env_file(TEMPLATE_PATH, keep_quotes=True)
    config: dict[str, str] = dict(template_values)

    computed_values = {
        "ACCOUNT_APP_NAME": account_settings.app_name,
        "ACCOUNT_APP_VERSION": account_settings.version,
        "ACCOUNT_APP_DEBUG": account_settings.debug,
        "ACCOUNT_DOCS_URL": account_settings.docs_url or "/docs",
        "ACCOUNT_REDOC_URL": account_settings.redoc_url or "/docs",
        "ACCOUNT_OPENAPI_URL": account_settings.openapi_url,
        "ACCOUNT_CORS_ENABLED": account_settings.cors_enabled,
        "ACCOUNT_REDIS_CHANNEL_PREFIX": "",
        "ACCOUNT_REDIS_ACCOUNT_CHANNEL": account_settings.redis_account_channel,
        "ACCOUNT_REDIS_POSITIONS_CHANNEL": account_settings.redis_positions_channel,
        "ACCOUNT_PUBLISH_ON_STARTUP": account_settings.publish_on_startup,
        "ACCOUNT_SUBSCRIPTION_POLL_INTERVAL": account_settings.subscription_poll_interval,
        "ACCOUNT_SNAPSHOT_TIMEOUT": account_settings.snapshot_timeout,
        "ACCOUNT_SUBSCRIPTION_QUEUE": "",
        "ACCOUNT_REQUEST_ID_HEADER": account_settings.request_id_header,
        "ACCOUNT_SERVICE_REGISTRY_ENABLED": account_settings.service_registry_enabled,
        "ACCOUNT_SERVICE_REGISTRY_NAME": account_settings.service_registry_name,
        "ACCOUNT_SERVICE_REGISTRY_SCHEME": account_settings.service_registry_scheme,
        "ACCOUNT_SERVICE_REGISTRY_HOST": account_settings.service_registry_host,
        "ACCOUNT_SERVICE_REGISTRY_PORT": account_settings.service_registry_port,
        "ACCOUNT_SERVICE_REGISTRY_URL": account_settings.service_registry_url or "",
        "ACCOUNT_SERVICE_REGISTRY_KEY": account_settings.service_registry_key,
        "ACCOUNT_SERVICE_REGISTRY_HEARTBEAT": account_settings.service_registry_heartbeat,
        "ACCOUNT_SERVICE_REGISTRY_REGISTRATION_ATTEMPTS": account_settings.service_registry_registration_attempts,
        "ACCOUNT_AUTO_CONFIGURE_IB": 1,
        "ACCOUNT_MARKET_DATA_SERVICE_NAME": account_settings.market_data_service_name or "",
        "ACCOUNT_MARKET_DATA_BASE_URL": account_settings.market_data_rest_base_url or "",
        "ACCOUNT_MARKET_DATA_TIMEOUT": account_settings.market_data_rest_timeout,
        "ACCOUNT_MARKET_DATA_OWNER_ID": account_settings.market_data_owner_id or "",
        "ACCOUNT_MARKET_DATA_CHANNEL_PREFIX": account_settings.market_data_channel_prefix or "",
        "ACCOUNT_MARKET_DATA_TICKER_CHANNEL": account_settings.market_data_ticker_channel or "",
        "ACCOUNT_MARKET_DATA_TICKER_THROTTLE_SECONDS": (
            ""
            if account_settings.market_data_ticker_throttle_seconds is None
            else account_settings.market_data_ticker_throttle_seconds
        ),
        "ACCOUNT_SERVICE_DISCOVERY_ENABLED": account_settings.service_discovery_enabled,
        "ACCOUNT_SERVICE_DISCOVERY_KEY": account_settings.service_discovery_registry_key,
    }

    for key, value in computed_values.items():
        set_value_from_template(config, template_values, key, value)

    shared_overrides = load_shared_env_values(
        (
            "LOG_DIR",
            "LOG_LEVEL",
            "LOG_STREAM",
            "REDIS_URL",
            "MARIADB_URL",
            "MARIADB_POOL_SIZE",
            "MARIADB_MAX_OVERFLOW",
            "MARIADB_POOL_RECYCLE",
            "MARIADB_RETRY_ATTEMPTS",
            "MARIADB_RETRY_BASE_DELAY",
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
    apply_overrides(config, shared_overrides)

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
    redis_url = base_env.get("REDIS_URL") or config.get("REDIS_URL")
    if redis_url:
        norm = _normalise_redis_url(redis_url)
        content = re.sub(r"^REDIS_URL=.*$", f"REDIS_URL={norm}", content, flags=re.MULTILINE)
    mariadb_url = base_env.get("MARIADB_URL") or config.get("MARIADB_URL")
    if mariadb_url:
        norm_db = _normalise_mariadb_url(mariadb_url)
        content = re.sub(r"^MARIADB_URL=.*$", f"MARIADB_URL={norm_db}", content, flags=re.MULTILINE)
    return content


def generate_account_env(path: Path, *, overwrite: bool = False) -> Path:
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
        default=Path("config/account_service.env"),
        help="Path to the environment file to generate (default: config/account_service.env)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    args = parser.parse_args()

    path = generate_account_env(args.output, overwrite=args.overwrite)
    print(f"Generated account service environment file at: {path}")


if __name__ == "__main__":
    main()
