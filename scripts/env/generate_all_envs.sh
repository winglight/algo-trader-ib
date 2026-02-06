#!/usr/bin/env python3
"""Run all environment generation scripts."""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Iterable

ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.env._env_shared import load_env_file, render_env_template  # noqa: E402
import re
from urllib.parse import urlparse, urlunparse

DEFAULT_GENERATORS: tuple[str, ...] = (
    "generate_root_env.py",
    "generate_ai_model_ops_env.py",
    "generate_news_service_env.py",
    "generate_account_env.py",
    "generate_market_data_env.py",
    "generate_orders_env.py",
    "generate_risk_env.py",
    "generate_strategy_env.py",
)


GENERATOR_OUTPUTS: dict[str, str] = {
    "generate_root_env.py": ".env",
    "generate_ai_model_ops_env.py": "config/ai_model_ops_service.env",
    "generate_news_service_env.py": "config/news_service.env",
    "generate_account_env.py": "config/account_service.env",
    "generate_market_data_env.py": "config/market_data_service.env",
    "generate_optimizer_env.py": "config/optimizer_service.env",
    "generate_orders_env.py": "config/orders_service.env",
    "generate_risk_env.py": "config/risk_service.env",
    "generate_strategy_env.py": "config/strategy_service.env",
}


def _run_generators(generator_scripts: Iterable[str], *, output_dir: pathlib.Path | None = None) -> None:
    script_dir = pathlib.Path(__file__).resolve().parent
    python_executable = os.environ.get("ENV_GEN_PYTHON", "python")
    # Client IDs increase by 10 per service to avoid accidental reuse:
    # Account=10, Market Data=20, Optimizer=30, Orders=40, Risk=50, Strategy=60.
    client_id_mapping = {
        "generate_account_env.py": 10,
        "generate_market_data_env.py": 20,
        "generate_optimizer_env.py": 30,
        "generate_orders_env.py": 40,
        "generate_risk_env.py": 50,
        "generate_strategy_env.py": 60,
    }

    for script_name in generator_scripts:
        script_path = script_dir / script_name
        if not script_path.exists():
            print(f"Skipping missing generator script: {script_path}")
            continue
        print(f"Running {script_name}...")
        env = os.environ.copy()
        if script_name in client_id_mapping:
            env["IB_CLIENT_ID"] = str(client_id_mapping[script_name])
        cmd = [python_executable, str(script_path), "--overwrite"]
        dest_path: pathlib.Path | None = None
        target_rel = GENERATOR_OUTPUTS.get(script_name)
        if output_dir is not None and target_rel is not None:
            dest_path = output_dir / target_rel
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            cmd.extend(["--output", str(dest_path)])
        completed = subprocess.run(
            cmd,
            check=False,
            env=env,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            print(
                f"Generator {script_name} failed (exit {completed.returncode}); skipping.\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
            if dest_path is not None and not dest_path.exists() and target_rel is not None:
                example = ROOT_DIR / f"{target_rel}.example"
                if example.exists():
                    try:
                        dest_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
                        print(f"Copied example to {dest_path}")
                    except Exception as exc:
                        print(f"Failed to copy example for {script_name}: {exc}")
            continue

        written_path: pathlib.Path | None = None
        if dest_path is not None:
            written_path = dest_path
        else:
            target_rel = GENERATOR_OUTPUTS.get(script_name)
            if target_rel:
                written_path = ROOT_DIR / target_rel
        if written_path and written_path.exists():
            _synchronise_url_hosts_in_file(written_path)

    if output_dir is not None:
        print(f"Environment files generated in {output_dir / 'config'}")
        _copy_config_files(output_dir)
        _synchronise_env_service_urls_in_dir(output_dir)
        _synchronise_url_hosts_in_file(output_dir / ".env.container")
    else:
        print(f"Environment files generated in {ROOT_DIR / 'config'}")
        _synchronise_root_env_service_urls()


def _load_service_registry_url(
    env_path: pathlib.Path,
    *,
    registry_prefix: str,
) -> str | None:
    if not env_path.exists():
        return None

    values = load_env_file(env_path)
    explicit_url = values.get(f"{registry_prefix}_SERVICE_REGISTRY_URL")
    if explicit_url:
        return explicit_url

    scheme = values.get(f"{registry_prefix}_SERVICE_REGISTRY_SCHEME")
    host = values.get(f"{registry_prefix}_SERVICE_REGISTRY_HOST")
    port = values.get(f"{registry_prefix}_SERVICE_REGISTRY_PORT")

    if not (scheme and host and port):
        return None

    scheme = str(scheme).strip()
    host = str(host).strip()
    port = str(port).strip()
    if not scheme or not host or not port:
        return None

    return f"{scheme}://{host}:{port}"


def _load_service_registry_host(
    env_path: pathlib.Path,
    *,
    registry_prefix: str,
) -> str | None:
    if not env_path.exists():
        return None

    values = load_env_file(env_path)
    host = values.get(f"{registry_prefix}_SERVICE_REGISTRY_HOST")
    if not host:
        return None
    return str(host).strip() or None


def _update_root_env(values: dict[str, str]) -> None:
    if not values:
        return

    template_path = ROOT_DIR / ".env.example"
    output_path = ROOT_DIR / ".env"
    if not output_path.exists() or not template_path.exists():
        return

    template_values = load_env_file(template_path, keep_quotes=True)
    current_values = load_env_file(output_path, keep_quotes=True)

    changed = False
    for key, value in values.items():
        if not value:
            continue
        text = str(value)
        if current_values.get(key) == text:
            continue
        current_values[key] = text
        changed = True

    if not changed:
        return

    merged: dict[str, str] = dict(template_values)
    merged.update(current_values)

    rendered = render_env_template(template_path, merged)
    output_path.write_text(rendered, encoding="utf-8")


def _copy_config_files(output_dir: pathlib.Path) -> None:
    targets = (
        "config/ai_model_ops.yml",
        "config/ai_model_ops.compose.yml",
        "config/integration.yml",
        "config/news_service.yml",
    )
    for rel_path in targets:
        source = ROOT_DIR / rel_path
        if not source.exists():
            continue
        destination = output_dir / rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _synchronise_root_env_service_urls() -> None:
    updates: dict[str, str] = {}

    ai_url = _load_service_registry_url(
        ROOT_DIR / "config/ai_model_ops_service.env",
        registry_prefix="AI_MODEL_OPS",
    )
    if ai_url:
        updates["AI_MODEL_OPS_SERVICE_URL"] = ai_url

    news_url = _load_service_registry_url(
        ROOT_DIR / "config/news_service.env",
        registry_prefix="NEWS_SERVICE",
    )
    if news_url:
        updates["NEWS_SERVICE_URL"] = news_url

    _update_root_env(updates)


def _synchronise_env_service_urls_in_dir(output_dir: pathlib.Path) -> None:
    updates: dict[str, str] = {}

    ai_url = _load_service_registry_url(
        output_dir / "config/ai_model_ops_service.env",
        registry_prefix="AI_MODEL_OPS",
    )
    if ai_url:
        updates["AI_MODEL_OPS_SERVICE_URL"] = ai_url

    news_url = _load_service_registry_url(
        output_dir / "config/news_service.env",
        registry_prefix="NEWS_SERVICE",
    )
    if news_url:
        updates["NEWS_SERVICE_URL"] = news_url

    account_host = _load_service_registry_host(
        output_dir / "config/account_service.env",
        registry_prefix="ACCOUNT",
    )
    if account_host:
        updates["ACCOUNT_SERVICE_REGISTRY_HOST"] = account_host

    orders_host = _load_service_registry_host(
        output_dir / "config/orders_service.env",
        registry_prefix="ORDERS",
    )
    if orders_host:
        updates["ORDERS_SERVICE_REGISTRY_HOST"] = orders_host

    market_host = _load_service_registry_host(
        output_dir / "config/market_data_service.env",
        registry_prefix="MARKET_DATA",
    )
    if market_host:
        updates["MARKET_DATA_SERVICE_REGISTRY_HOST"] = market_host

    risk_host = _load_service_registry_host(
        output_dir / "config/risk_service.env",
        registry_prefix="RISK",
    )
    if risk_host:
        updates["RISK_SERVICE_REGISTRY_HOST"] = risk_host

    strategy_host = _load_service_registry_host(
        output_dir / "config/strategy_service.env",
        registry_prefix="STRATEGY",
    )
    if strategy_host:
        updates["STRATEGY_SERVICE_REGISTRY_HOST"] = strategy_host

    if not updates:
        return

    template_path = ROOT_DIR / ".env.example"
    output_path = output_dir / ".env.container"
    if not output_path.exists() or not template_path.exists():
        return

    template_values = load_env_file(template_path, keep_quotes=True)
    current_values = load_env_file(output_path, keep_quotes=True)

    changed = False
    for key, value in updates.items():
        if not value:
            continue
        text = str(value)
        if current_values.get(key) == text:
            continue
        current_values[key] = text
        changed = True
    if not changed:
        return

    merged: dict[str, str] = dict(template_values)
    merged.update(current_values)
    rendered = render_env_template(template_path, merged)
    output_path.write_text(rendered, encoding="utf-8")


def _synchronise_url_hosts_in_file(path: pathlib.Path) -> None:
    if not path.exists():
        return
    base_env = load_env_file(ROOT_DIR / ".env", keep_quotes=True)
    redis_url = base_env.get("REDIS_URL")
    mariadb_url = base_env.get("MARIADB_URL")
    ib_host = base_env.get("IB_GATEWAY_HOST")
    ib_port = base_env.get("IB_GATEWAY_PORT")
    if not any((redis_url, mariadb_url, ib_host, ib_port)):
        return

    def _extract_host_port(url: str) -> tuple[str | None, str | None]:
        p = urlparse(url.strip().strip('"').strip("'"))
        host = p.hostname
        port = str(p.port) if p.port is not None else None
        return host, port

    def _replace_host_port(url: str, new_host: str | None, new_port: str | None) -> str:
        raw = url.strip()
        quote: str | None = None
        if raw and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
            quote = raw[0]
            raw = raw[1:-1]
        p = urlparse(raw)
        userinfo = ""
        if p.username:
            userinfo = p.username
        if p.password:
            userinfo = f"{userinfo}:{p.password}" if userinfo else f":{p.password}"
        host = new_host or p.hostname or ""
        port = new_port or (str(p.port) if p.port is not None else "")
        netloc = f"{userinfo}@{host}{(':' + port) if port else ''}" if userinfo else f"{host}{(':' + port) if port else ''}"
        rebuilt = urlunparse((p.scheme or "", netloc, p.path or "", p.params or "", p.query or "", p.fragment or ""))
        if quote:
            return f"{quote}{rebuilt}{quote}"
        return rebuilt

    content = path.read_text(encoding="utf-8")
    changed = False

    if redis_url:
        new_host, new_port = _extract_host_port(redis_url)
        def _sub_redis(m: re.Match[str]) -> str:
            current = m.group(1)
            return f"REDIS_URL={_replace_host_port(current, new_host, new_port)}"
        new_content = re.sub(r"^REDIS_URL=(.*)$", _sub_redis, content, flags=re.MULTILINE)
        if new_content != content:
            content = new_content
            changed = True

    if mariadb_url:
        new_host, new_port = _extract_host_port(mariadb_url)
        def _sub_mariadb(m: re.Match[str]) -> str:
            current = m.group(1)
            return f"MARIADB_URL={_replace_host_port(current, new_host, new_port)}"
        new_content = re.sub(r"^MARIADB_URL=(.*)$", _sub_mariadb, content, flags=re.MULTILINE)
        if new_content != content:
            content = new_content
            changed = True

    if ib_host is not None:
        def _sub_ib_host(_: re.Match[str]) -> str:
            return f"IB_GATEWAY_HOST={ib_host}"
        new_content = re.sub(r"^IB_GATEWAY_HOST=.*$", _sub_ib_host, content, flags=re.MULTILINE)
        if new_content != content:
            content = new_content
            changed = True

    if ib_port is not None:
        def _sub_ib_port(_: re.Match[str]) -> str:
            return f"IB_GATEWAY_PORT={ib_port}"
        new_content = re.sub(r"^IB_GATEWAY_PORT=.*$", _sub_ib_port, content, flags=re.MULTILINE)
        if new_content != content:
            content = new_content
            changed = True

    if changed:
        path.write_text(content, encoding="utf-8")


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("generators", nargs="*", help="Specific generator scripts to run")
    parser.add_argument("--output-dir", type=pathlib.Path, default=None, help="Directory to write generated env files")
    args = parser.parse_args(argv)

    if args.generators:
        generators = tuple(args.generators)
    else:
        generators = DEFAULT_GENERATORS

    _run_generators(generators, output_dir=args.output_dir)


if __name__ == "__main__":
    main(sys.argv[1:])
