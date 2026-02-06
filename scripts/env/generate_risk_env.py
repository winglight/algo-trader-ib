#!/usr/bin/env python3
"""Generate an environment file for the Risk service configuration."""

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
from src.risk.config import load_risk_settings
from src.risk_service.config import (
    load_risk_example_defaults,
    load_risk_service_settings,
)


TEMPLATE_PATH = ROOT_DIR / "config/risk_service.env.example"


def _serialise_cors(values: tuple[str, ...]) -> str:
    if not values:
        return "*"
    return ",".join(values)


def _serialise_metrics(values: tuple[str, ...]) -> str:
    if not values:
        return ""
    return ",".join(values)


def _default_content() -> str:
    template_values = load_env_file(TEMPLATE_PATH, keep_quotes=True)
    template_defaults = load_env_file(TEMPLATE_PATH, keep_quotes=False)
    config: dict[str, str] = dict(template_values)

    env = dict(os.environ)
    project_env = load_env_file(ROOT_DIR / ".env", keep_quotes=False)
    base_defaults: dict[str, str] = {}
    base_defaults.update(load_risk_example_defaults())
    base_defaults.update(template_defaults)
    base_defaults.update(project_env)
    for key, value in base_defaults.items():
        if key not in env and value is not None and value != "":
            env[key] = value

    service_settings = load_risk_service_settings(env)
    engine_settings = load_risk_settings(env)

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
        ),
        keep_quotes=True,
        prefer_env=False,
    )
    performance_metrics = _serialise_metrics(service_settings.performance_metric_keys)

    mariadb_echo = env.get("MARIADB_ECHO")
    if mariadb_echo is None:
        mariadb_echo = template_defaults.get("MARIADB_ECHO", "false")
    mariadb_echo_normalised = str(mariadb_echo).strip().lower()
    if mariadb_echo_normalised not in {"true", "false"}:
        mariadb_echo_normalised = "false"

    computed_values = {
        "RISK_APP_NAME": service_settings.app_name,
        "RISK_APP_VERSION": service_settings.version,
        "RISK_APP_DEBUG": service_settings.debug,
        "RISK_DOCS_URL": service_settings.docs_url or "/docs",
        "RISK_REDOC_URL": service_settings.redoc_url or "/docs",
        "RISK_OPENAPI_URL": service_settings.openapi_url,
        "RISK_REQUEST_ID_HEADER": service_settings.request_id_header,
        "RISK_CORS_ENABLED": service_settings.cors_enabled,
        "RISK_CORS_ALLOW_ORIGINS": _serialise_cors(service_settings.cors_allow_origins),
        "RISK_CORS_ALLOW_METHODS": _serialise_cors(service_settings.cors_allow_methods),
        "RISK_CORS_ALLOW_HEADERS": _serialise_cors(service_settings.cors_allow_headers),
        "RISK_ALERTS_CHANNEL": service_settings.alerts_channel,
        "RISK_METRICS_CHANNEL": service_settings.metrics_channel,
        "RISK_PUBLISH_METRICS": service_settings.publish_metrics,
        "RISK_EVENT_HISTORY_LIMIT": service_settings.event_history_limit,
        "RISK_PERFORMANCE_METRICS": performance_metrics,
        "RISK_TRAILING_COOLDOWN": engine_settings.trailing_cooldown_seconds,
        "RISK_DEFAULT_REDUCE_FRACTION": engine_settings.default_reduce_fraction,
        "RISK_POSITION_EPSILON": engine_settings.position_epsilon,
        "RISK_VIOLATION_EVENT_LEVEL": engine_settings.violation_event_level.value,
        "RISK_WARNING_EVENT_LEVEL": engine_settings.warning_event_level.value,
        "RISK_INFO_EVENT_LEVEL": engine_settings.info_event_level.value,
        "RISK_SERVICE_REGISTRY_ENABLED": service_settings.service_registry_enabled,
        "RISK_SERVICE_REGISTRY_NAME": service_settings.service_registry_name,
        "RISK_SERVICE_REGISTRY_SCHEME": service_settings.service_registry_scheme,
        "RISK_SERVICE_REGISTRY_HOST": service_settings.service_registry_host,
        "RISK_SERVICE_REGISTRY_PORT": service_settings.service_registry_port,
        "RISK_SERVICE_REGISTRY_URL": service_settings.service_registry_url or "",
        "RISK_SERVICE_REGISTRY_KEY": service_settings.service_registry_key,
        "RISK_SERVICE_REGISTRY_HEARTBEAT": service_settings.service_registry_heartbeat,
        "RISK_SERVICE_REGISTRY_REGISTRATION_ATTEMPTS": service_settings.service_registry_registration_attempts,
        "MARKET_DATA_REDIS_CHANNEL_PREFIX": service_settings.market_data_channel_prefix
        or "",
        "MARKET_DATA_REDIS_DOM_CHANNEL": service_settings.market_data_dom_channel or "",
        "MARKET_DATA_REDIS_TICKER_CHANNEL": service_settings.market_data_ticker_channel or "",
        "MARKET_DATA_REDIS_BAR_CHANNEL": service_settings.market_data_bar_channel or "",
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


def generate_risk_env(path: Path, *, overwrite: bool = False) -> Path:
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
        default=Path("config/risk_service.env"),
        help="Path to the environment file to generate (default: config/risk_service.env)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    args = parser.parse_args()

    path = generate_risk_env(args.output, overwrite=args.overwrite)
    print(f"Generated risk service environment file at: {path}")


if __name__ == "__main__":
    main()
