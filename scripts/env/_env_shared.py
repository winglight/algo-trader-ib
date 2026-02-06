"""Shared helpers for environment generation scripts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

try:  # pragma: no cover - optional dependency
    import redis.asyncio as redis_asyncio  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - redis is optional during generation
    redis_asyncio = None

ROOT_DIR = Path(__file__).resolve().parents[2]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

LOGGER = logging.getLogger(__name__)


class RegistryResolutionError(RuntimeError):
    """Raised when the service registry cannot be queried for shared values."""


def _strip_surrounding_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _coerce_shared_value(value: Any, *, keep_quotes: bool) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
    else:
        text = str(value).strip()
    if not text:
        return None
    if not keep_quotes:
        text = _strip_surrounding_quotes(text).strip()
        if not text:
            return None
    return text


def _extract_shared_environment(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Extract shared environment metadata from a registry payload."""

    search_paths: Sequence[Sequence[str]] = (
        ("metadata", "config", "environment", "shared"),
        ("metadata", "config", "environment"),
        ("metadata", "environment", "shared"),
        ("metadata", "environment"),
        ("metadata", "shared_environment"),
        ("config", "environment", "shared"),
        ("config", "environment"),
        ("environment", "shared"),
        ("environment",),
        ("shared_environment",),
    )

    for path in search_paths:
        node: Any = payload
        for segment in path:
            if not isinstance(node, Mapping):
                break
            node = node.get(segment)
        else:
            if isinstance(node, Mapping):
                return node
    return {}


class SharedEnvRegistryResolver:
    """Resolve shared environment overrides from the Redis service registry."""

    def __init__(
        self,
        redis_url: str,
        registry_key: str,
        service_name: str,
        *,
        timeout: float = 2.0,
    ) -> None:
        self._redis_url = redis_url
        self._registry_key = registry_key
        self._service_name = service_name
        self._timeout = max(0.0, float(timeout)) or 2.0

    @classmethod
    def from_sources(
        cls,
        env_values: Mapping[str, str],
        example_values: Mapping[str, str],
    ) -> "SharedEnvRegistryResolver | None":
        """Build a resolver using values from environment files."""

        def lookup(*names: str) -> str | None:
            for name in names:
                env_value = _value_from_environ(name)
                if env_value:
                    return env_value
                candidate = env_values.get(name)
                if candidate:
                    return _strip_surrounding_quotes(candidate)
                example = example_values.get(name)
                if example:
                    return _strip_surrounding_quotes(example)
            return None

        redis_url = lookup("SERVICE_REGISTRY_URL", "REDIS_URL")
        service_name = lookup("SERVICE_REGISTRY_SHARED_SETTINGS_NAME")
        if not redis_url or not service_name:
            return None

        registry_key = lookup(
            "SERVICE_REGISTRY_SHARED_SETTINGS_KEY",
            "SERVICE_REGISTRY_KEY",
        ) or "services:registry"

        timeout_raw = lookup("SERVICE_REGISTRY_SHARED_SETTINGS_TIMEOUT")
        timeout = 2.0
        if timeout_raw:
            try:
                timeout = float(timeout_raw)
            except ValueError:
                LOGGER.debug(
                    "Invalid SERVICE_REGISTRY_SHARED_SETTINGS_TIMEOUT value: %s",
                    timeout_raw,
                )
        return cls(redis_url, registry_key, service_name, timeout=timeout)

    def resolve(self, keys: Sequence[str], *, keep_quotes: bool = False) -> dict[str, str]:
        """Return overrides resolved from the service registry."""

        if not keys:
            return {}

        async def runner() -> dict[str, str]:
            return await self._resolve_async(tuple(keys), keep_quotes=keep_quotes)

        try:
            return asyncio.run(runner())
        except RuntimeError as exc:  # pragma: no cover - event loop already running
            if "event loop" not in str(exc).lower():
                raise
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(runner())
            finally:
                loop.close()

    async def _resolve_async(
        self,
        keys: Sequence[str],
        *,
        keep_quotes: bool,
    ) -> dict[str, str]:
        if redis_asyncio is None:
            raise RegistryResolutionError("redis asyncio client is not available")

        client = redis_asyncio.from_url(
            self._redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        try:
            try:
                raw_payload = await asyncio.wait_for(
                    client.hget(self._registry_key, self._service_name),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError as exc:
                raise RegistryResolutionError("Timed out querying service registry") from exc
            except Exception as exc:  # pragma: no cover - redis runtime errors
                raise RegistryResolutionError("Failed to query service registry") from exc
        finally:
            await self._close_client(client)

        if raw_payload is None:
            return {}

        if isinstance(raw_payload, bytes):
            raw_text = raw_payload.decode("utf-8", errors="ignore").strip()
        else:
            raw_text = str(raw_payload).strip()

        if not raw_text:
            return {}

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise RegistryResolutionError("Registry payload is not valid JSON") from exc

        if not isinstance(payload, Mapping):
            raise RegistryResolutionError("Registry payload must be a JSON object")

        shared_mapping = _extract_shared_environment(payload)
        if not shared_mapping:
            return {}

        resolved: dict[str, str] = {}
        for key in keys:
            if key not in shared_mapping:
                continue
            value = _coerce_shared_value(shared_mapping.get(key), keep_quotes=keep_quotes)
            if value is not None:
                resolved[key] = value
        return resolved

    async def _close_client(self, client: Any) -> None:
        close = getattr(client, "close", None)
        wait_closed = getattr(client, "wait_closed", None)

        try:
            if callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    await result
        finally:
            if callable(wait_closed):  # pragma: no cover - depends on redis version
                result = wait_closed()
                if asyncio.iscoroutine(result):
                    await result


_SHARED_REGISTRY_RESOLVER_FACTORY: Callable[
    [Mapping[str, str], Mapping[str, str]],
    SharedEnvRegistryResolver | None,
] = SharedEnvRegistryResolver.from_sources


def _resolve_registry_shared_env(
    keys: Sequence[str],
    *,
    env_values: Mapping[str, str],
    example_values: Mapping[str, str],
    keep_quotes: bool,
) -> dict[str, str]:
    if not keys:
        return {}

    factory = _SHARED_REGISTRY_RESOLVER_FACTORY
    if factory is None:
        return {}

    try:
        resolver = factory(env_values, example_values)
    except Exception:  # pragma: no cover - defensive guard
        LOGGER.exception("Failed to initialise shared registry resolver")
        return {}

    if resolver is None:
        return {}

    try:
        return resolver.resolve(keys, keep_quotes=keep_quotes)
    except RegistryResolutionError as exc:
        LOGGER.debug("Shared registry lookup failed: %s", exc)
        return {}


def _strip_inline_comment(value: str) -> str:
    """Remove inline ``#`` comments while keeping literal hashes inside values."""

    if "#" not in value:
        return value

    result: list[str] = []
    quote: str | None = None
    escaped = False
    prev_char: str | None = None

    for char in value:
        if escaped:
            result.append(char)
            escaped = False
            prev_char = char
            continue

        if char == "\\" and quote is not None:
            result.append(char)
            escaped = True
            prev_char = char
            continue

        if char in {"'", '"'}:
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
            result.append(char)
            prev_char = char
            continue

        if (
            char == "#"
            and quote is None
            and (prev_char is None or prev_char.isspace())
        ):
            break

        result.append(char)
        prev_char = char

    return "".join(result).rstrip()


def _parse_env_file(path: Path, *, keep_quotes: bool = False) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("\ufeff"):
            line = line.lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        if key.lower().startswith("export "):
            key = key[7:].strip()
        value = _strip_inline_comment(value).strip()
        if not key:
            continue
        if (
            not keep_quotes
            and value
            and value[0] == value[-1]
            and value[0] in {"\"", "'"}
        ):
            value = value[1:-1]
        values[key] = value
    return values


def _load_env_file(path: Path, *, keep_quotes: bool = False) -> dict[str, str]:
    if not path.exists():
        return {}
    return _parse_env_file(path, keep_quotes=keep_quotes)


def load_env_file(path: Path, *, keep_quotes: bool = False) -> dict[str, str]:
    """Expose parsing of arbitrary ``.env``-style files for generators."""

    return _load_env_file(path, keep_quotes=keep_quotes)


def _value_from_environ(key: str) -> str | None:
    """Return the value for ``key`` from ``os.environ`` if it is meaningful."""

    value = os.environ.get(key)
    if value is None or value == "":
        return None
    return value


def load_shared_env_values(
    keys: Iterable[str] | None = None,
    *,
    keep_quotes: bool = False,
    prefer_env: bool = True,
) -> dict[str, str]:
    """Load selected values from the root ``.env`` file or fall back to defaults.

    Parameters
    ----------
    keys:
        Optional subset of keys to resolve. When omitted the shared defaults are
        used.
    keep_quotes:
        When ``True`` the helper preserves surrounding single or double quotes
        found in ``.env`` files. This is useful for values such as URLs that may
        contain ``&`` or other shell-sensitive characters and need to remain
        quoted verbatim in generated environment files.
    prefer_env:
        When ``True`` (the default) values explicitly exported via
        ``os.environ`` take precedence over entries defined in ``.env`` files.
        Set this to ``False`` when the literal formatting of the ``.env`` value
        must be preserved (for example when retaining surrounding quotes).
    """

    example_values = _load_env_file(ROOT_DIR / ".env.example", keep_quotes=keep_quotes)
    env_values = _load_env_file(ROOT_DIR / ".env", keep_quotes=keep_quotes)

    if keys is None:
        seen: dict[str, None] = {}
        for mapping in (env_values, example_values):
            for key in mapping:
                if key not in seen:
                    seen[key] = None
        keys = tuple(seen.keys())
    else:
        keys = tuple(keys)

    registry_values = _resolve_registry_shared_env(
        keys,
        env_values=env_values,
        example_values=example_values,
        keep_quotes=keep_quotes,
    )
    resolved: dict[str, str] = {}

    for key in keys:
        registry_value = registry_values.get(key)
        if registry_value is not None and registry_value != "":
            resolved[key] = registry_value
            continue

        env_value = _value_from_environ(key)

        if prefer_env and env_value is not None:
            resolved[key] = env_value
            continue

        found = False
        for candidate_source in (env_values, example_values):
            candidate = candidate_source.get(key)
            if candidate is not None and candidate != "":
                resolved[key] = candidate
                found = True
                break

        if found:
            continue

        if not prefer_env and env_value is not None:
            resolved[key] = env_value
        else:
            resolved[key] = ""

    return resolved


def _infer_quote_style(template_value: str | None) -> str | None:
    if not template_value:
        return None
    if len(template_value) >= 2 and template_value[0] == template_value[-1]:
        if template_value[0] in {'"', "'"}:
            return template_value[0]
    return None


def format_env_value(value: object, *, quote: str | None = None) -> str:
    if isinstance(value, bool):
        result = "true" if value else "false"
    elif value is None:
        result = ""
    else:
        result = str(value)

    if quote:
        return f"{quote}{result}{quote}"
    return result


def set_value_from_template(
    target: dict[str, str],
    template_values: Mapping[str, str],
    key: str,
    value: object,
    *,
    quote: str | None = None,
) -> None:
    template_quote = _infer_quote_style(template_values.get(key))
    resolved_quote = quote if quote is not None else template_quote
    target[key] = format_env_value(value, quote=resolved_quote)


def apply_overrides(target: dict[str, str], overrides: Mapping[str, str]) -> None:
    for key, value in overrides.items():
        if value is None or value == "":
            continue
        target[key] = value


def apply_root_env_overrides(target: dict[str, str]) -> None:
    project_env = _load_env_file(ROOT_DIR / ".env", keep_quotes=True)
    for key in tuple(target.keys()):
        candidate = project_env.get(key)
        if candidate is not None and candidate != "":
            quote = _infer_quote_style(target.get(key))
            target[key] = (
                format_env_value(candidate, quote=quote)
                if quote
                else candidate
            )

    for key in tuple(target.keys()):
        env_value = _value_from_environ(key)
        if env_value is not None and env_value != "":
            quote = _infer_quote_style(target.get(key))
            target[key] = (
                format_env_value(env_value, quote=quote)
                if quote
                else env_value
            )


def render_env_template(template_path: Path, values: Mapping[str, str]) -> str:
    template_lines = template_path.read_text(encoding="utf-8").splitlines()
    rendered: list[str] = []
    seen_keys: set[str] = set()

    for raw_line in template_lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            rendered.append(raw_line)
            continue

        key, _, _ = raw_line.partition("=")
        key = key.strip()
        if key in values:
            rendered.append(f"{key}={values[key]}")
            seen_keys.add(key)
        else:
            rendered.append(raw_line)

    extra_keys = [key for key in values.keys() if key not in seen_keys]
    if extra_keys:
        if rendered and rendered[-1] != "":
            rendered.append("")
        for key in sorted(extra_keys):
            rendered.append(f"{key}={values[key]}")

    return "\n".join(rendered).rstrip() + "\n"

