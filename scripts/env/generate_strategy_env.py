#!/usr/bin/env python3
"""Generate an environment file for the Strategy service configuration."""

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
from src.market_data.config import load_market_data_example_defaults
from src.strategy_service.config import (
    load_strategy_example_defaults,
    load_strategy_service_settings,
)


TEMPLATE_PATH = ROOT_DIR / "config/strategy_service.env.example"


def _serialise_cors(values: tuple[str, ...]) -> str:
    if not values:
        return "*"
    return ",".join(values)


def _serialise_periods(values: tuple[str, ...]) -> str:
    if not values:
        return "day,week,month,all"
    return ",".join(values)


def _serialise_instruments(values: tuple[tuple[str, str | None], ...]) -> str:
    if not values:
        return ""
    formatted: list[str] = []
    for symbol, timeframe in values:
        if timeframe:
            formatted.append(f"{symbol}:{timeframe}")
        else:
            formatted.append(symbol)
    return ",".join(formatted)


def _default_content() -> str:
    template_values = load_env_file(TEMPLATE_PATH, keep_quotes=True)
    template_defaults = load_env_file(TEMPLATE_PATH, keep_quotes=False)
    config: dict[str, str] = dict(template_values)

    env = dict(os.environ)
    project_env = load_env_file(ROOT_DIR / ".env", keep_quotes=False)
    base_defaults: dict[str, str] = {}
    base_defaults.update(load_strategy_example_defaults())
    base_defaults.update(load_market_data_example_defaults())
    base_defaults.update(template_defaults)
    base_defaults.update(project_env)
    ib_gateway_keys = (
        "IB_GATEWAY_HOST",
        "IB_GATEWAY_PORT",
        "IB_CLIENT_ID",
        "IB_CLIENT_ID_FALLBACKS",
        "IB_CONNECT_TIMEOUT",
        "IB_READ_ONLY",
    )
    for key in ib_gateway_keys:
        value = project_env.get(key)
        if value is not None and value != "":
            base_defaults[key] = value
    for key, value in base_defaults.items():
        if key not in env and value is not None and value != "":
            env[key] = value

    service_settings = load_strategy_service_settings(env)
    shared_env = load_shared_env_values(
        (
            "LOG_DIR",
            "LOG_LEVEL",
            "LOG_STREAM",
            "REDIS_URL",
            "MARKET_DATA_REDIS_CHANNEL_PREFIX",
            "MARKET_DATA_REDIS_DOM_CHANNEL",
            "MARKET_DATA_REDIS_TICKER_CHANNEL",
            "MARKET_DATA_REDIS_BAR_CHANNEL",
            "MARIADB_URL",
            "MARIADB_POOL_SIZE",
            "MARIADB_MAX_OVERFLOW",
            "MARIADB_POOL_RECYCLE",
            "IB_GATEWAY_HOST",
            "IB_GATEWAY_PORT",
            "IB_CLIENT_ID",
            "IB_CLIENT_ID_FALLBACKS",
            "IB_CONNECT_TIMEOUT",
            "IB_READ_ONLY",
        ),
        keep_quotes=True,
        prefer_env=False,
    )
    mariadb_echo = env.get("MARIADB_ECHO")
    if mariadb_echo is None:
        mariadb_echo = template_defaults.get("MARIADB_ECHO", "false")
    mariadb_echo_normalised = str(mariadb_echo).strip().lower()
    if mariadb_echo_normalised not in {"true", "false"}:
        mariadb_echo_normalised = "false"

    performance_periods = _serialise_periods(service_settings.performance_periods)
    market_data_symbols = _serialise_instruments(
        service_settings.market_data_rest_instruments
    )
    computed_values = {
        "STRATEGY_APP_NAME": service_settings.app_name,
        "STRATEGY_APP_VERSION": service_settings.version,
        "STRATEGY_APP_DEBUG": service_settings.debug,
        "STRATEGY_DOCS_URL": service_settings.docs_url or "/docs",
        "STRATEGY_REDOC_URL": service_settings.redoc_url or "/docs",
        "STRATEGY_OPENAPI_URL": service_settings.openapi_url,
        "STRATEGY_REQUEST_ID_HEADER": service_settings.request_id_header,
        "STRATEGY_CORS_ENABLED": service_settings.cors_enabled,
        "STRATEGY_CORS_ALLOW_ORIGINS": _serialise_cors(
            service_settings.cors_allow_origins
        ),
        "STRATEGY_CORS_ALLOW_METHODS": _serialise_cors(
            service_settings.cors_allow_methods
        ),
        "STRATEGY_CORS_ALLOW_HEADERS": _serialise_cors(
            service_settings.cors_allow_headers
        ),
        "STRATEGY_STATUS_CHANNEL": service_settings.status_channel,
        "STRATEGY_METRICS_CHANNEL": service_settings.metrics_channel,
        "STRATEGY_PUBLISH_STATUS_EVENTS": service_settings.publish_status_events,
        "STRATEGY_PUBLISH_METRIC_EVENTS": service_settings.publish_metric_events,
        "STRATEGY_METRICS_REFRESH_INTERVAL": service_settings.metrics_refresh_interval,
        "STRATEGY_PERFORMANCE_PERIODS": performance_periods,
        "STRATEGY_MARKET_TIMEZONE": service_settings.market_timezone,
        "STRATEGY_SOURCE_DIR": service_settings.strategy_source_dir,
        "STRATEGY_PACKAGE": service_settings.strategy_package,
        "STRATEGY_PREDICTIVE_RESULT_CHANNEL": service_settings.predictive.result_channel,
        "STRATEGY_PREDICTIVE_ACTIVATION_CHANNEL": service_settings.predictive.activation_channel,
        "STRATEGY_PREDICTIVE_MODEL_NAME": service_settings.predictive.model_name,
        "STRATEGY_PREDICTIVE_FUSION_STRATEGY": service_settings.predictive.fusion_defaults.strategy,
        "STRATEGY_PREDICTIVE_FUSION_NEWS_WEIGHT": service_settings.predictive.fusion_defaults.news_weight,
        "STRATEGY_PREDICTIVE_FUSION_CONFIDENCE_THRESHOLD": service_settings.predictive.fusion_defaults.confidence_threshold,
        "STRATEGY_PREDICTIVE_FUSION_ENABLE_NEWS_FEATURES": service_settings.predictive.fusion_defaults.enable_news_features,
        "STRATEGY_PREDICTIVE_FUSION_NEWS_MODEL_VERSION": service_settings.predictive.fusion_defaults.news_model_version or "",
        "STRATEGY_MARKET_DATA_BASE_URL": (
            service_settings.market_data_rest_base_url or "http://127.0.0.1:8102"
        ),
        "STRATEGY_MARKET_DATA_SYMBOLS": market_data_symbols,
        "STRATEGY_MARKET_DATA_DOM_THROTTLE_SECONDS": service_settings.market_data_dom_throttle_seconds,
        "STRATEGY_MARKET_DATA_TICKER_THROTTLE_SECONDS": service_settings.market_data_ticker_throttle_seconds,
        "STRATEGY_MARKET_DATA_BAR_THROTTLE_SECONDS": service_settings.market_data_bar_throttle_seconds,
        "STRATEGY_MARKET_DATA_STREAM_SUPERVISOR_INTERVAL": service_settings.market_data_stream_supervisor_interval_seconds,
        "STRATEGY_MARKET_DATA_INACTIVITY_THRESHOLD_SECONDS": service_settings.market_data_inactivity_threshold_seconds,
        "STRATEGY_MARKET_DATA_RECOVERY_MAX_ATTEMPTS": service_settings.market_data_recovery_max_attempts,
        "STRATEGY_PUBSUB_HEARTBEAT_INTERVAL_SECONDS": service_settings.pubsub_heartbeat_interval_seconds,
        "STRATEGY_PUBSUB_IDLE_TIMEOUT_SECONDS": service_settings.pubsub_idle_timeout_seconds,
        "STRATEGY_THRESHOLD_MODEL_ENDPOINT": service_settings.threshold_model_endpoint or "",
        "STRATEGY_THRESHOLD_MODEL_TIMEOUT_SECONDS": service_settings.threshold_model_timeout_seconds,
        "STRATEGY_THRESHOLD_MODEL_AUTH_TOKEN": service_settings.threshold_model_auth_token or "",
        "STRATEGY_SERVICE_REGISTRY_ENABLED": service_settings.service_registry_enabled,
        "STRATEGY_SERVICE_REGISTRY_NAME": service_settings.service_registry_name,
        "STRATEGY_SERVICE_REGISTRY_SCHEME": service_settings.service_registry_scheme,
        "STRATEGY_SERVICE_REGISTRY_HOST": service_settings.service_registry_host,
        "STRATEGY_SERVICE_REGISTRY_PORT": service_settings.service_registry_port,
        "STRATEGY_SERVICE_REGISTRY_URL": service_settings.service_registry_url or "",
        "STRATEGY_SERVICE_REGISTRY_KEY": service_settings.service_registry_key,
        "STRATEGY_SERVICE_REGISTRY_HEARTBEAT": service_settings.service_registry_heartbeat,
        "STRATEGY_SERVICE_REGISTRY_REGISTRATION_ATTEMPTS": service_settings.service_registry_registration_attempts,
        "STRATEGY_SERVICE_DISCOVERY_ENABLED": service_settings.service_discovery_enabled,
        "STRATEGY_SERVICE_DISCOVERY_REGISTRY_KEY": (
            service_settings.service_discovery_registry_key
        ),
        "MARKET_DATA_REDIS_CHANNEL_PREFIX": service_settings.market_data_channel_prefix
        or "",
        "MARKET_DATA_REDIS_DOM_CHANNEL": service_settings.market_data_dom_channel or "",
        "MARKET_DATA_REDIS_TICKER_CHANNEL": service_settings.market_data_ticker_channel or "",
        "MARKET_DATA_REDIS_BAR_CHANNEL": service_settings.market_data_bar_channel or "",
        "STRATEGY_ORDERS_BASE_URL": service_settings.orders_service_base_url or "http://127.0.0.1:8101",
        "STRATEGY_ORDERS_STATUS_CHANNEL": service_settings.orders_status_channel or "orders.status",
        "STRATEGY_ORDERS_FILL_CHANNEL": service_settings.orders_fill_channel or "orders.fill",
        "STRATEGY_ORDERS_SERVICE_NAME": service_settings.orders_service_name,
        "STRATEGY_RISK_SERVICE_NAME": service_settings.risk_service_name,
        "STRATEGY_MARKET_DATA_SERVICE_NAME": service_settings.market_data_service_name,
        "STRATEGY_ACCOUNT_SERVICE_NAME": getattr(service_settings, "account_service_name", "account"),
        "STRATEGY_SCREENER_RESULTS_CHANNEL": service_settings.screener_results_channel,
        "STRATEGY_SCREENER_METADATA_CACHE_PATH": service_settings.screener_metadata_cache_path
        or str((Path.home() / ".cache" / "algo-trader" / "screener_metadata.json")),
        "MARIADB_ECHO": mariadb_echo_normalised,
    }

    for key, value in computed_values.items():
        set_value_from_template(config, template_values, key, value)

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
    redis_url = base_env.get("REDIS_URL") or config.get("REDIS_URL")
    if redis_url:
        norm = _normalise_redis_url(redis_url)
        content = re.sub(r"^REDIS_URL=.*$", f"REDIS_URL={norm}", content, flags=re.MULTILINE)
    mariadb_url = base_env.get("MARIADB_URL") or config.get("MARIADB_URL")
    if mariadb_url:
        norm_db = _normalise_mariadb_url(mariadb_url)
        content = re.sub(r"^MARIADB_URL=.*$", f"MARIADB_URL={norm_db}", content, flags=re.MULTILINE)
    return content


def generate_strategy_env(path: Path, *, overwrite: bool = False) -> Path:
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
        default=Path("config/strategy_service.env"),
        help="Path to the environment file to generate (default: config/strategy_service.env)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    args = parser.parse_args()

    path = generate_strategy_env(args.output, overwrite=args.overwrite)
    print(f"Generated strategy service environment file at: {path}")


if __name__ == "__main__":
    main()
