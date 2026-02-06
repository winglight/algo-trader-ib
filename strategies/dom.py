"""Depth-of-market subscription helpers for strategies."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, ClassVar, Dict, Mapping

from src.dom import DomSymbolConfig
from src.server.market_data import MarketDataServiceError, MarketDataServiceUnavailable
from src.strategy.base import BaseStrategy, StrategyError
from src.strategy.runtime import DomRuntimeTelemetryService
from src.strategy.types import StrategyIdentifier, normalize_strategy_identifier

try:  # pragma: no cover - optional typing dependency
    from src.redis_client.pubsub import PubSubChannel
except Exception:  # pragma: no cover - fallback when redis client unavailable
    PubSubChannel = Any  # type: ignore[misc, assignment]


_Dispatcher = Callable[[Mapping[str, Any]], Awaitable[None]]


class DomServiceSubscriptionMixin:
    """Shared helpers for managing DOM service symbol subscriptions."""

    _dom_service: Any | None
    _dom_subscription_active: bool
    _dom_subscription_retry_attempts: int
    _dom_subscription_symbol: str | None
    _dom_subscription_depth_levels: int | None
    _dom_subscription_metadata_tag: str | None
    _dom_subscription_metadata: Mapping[str, Any] | None

    def _dom_subscription_reset_state(self) -> None:
        self._dom_subscription_active = False
        self._dom_subscription_retry_attempts = 0
        self._dom_subscription_symbol = None
        self._dom_subscription_depth_levels = None
        self._dom_subscription_metadata_tag = None
        self._dom_subscription_metadata = None

    @staticmethod
    def _dom_exception_cause_code(exc: Exception) -> str:
        name = exc.__class__.__name__ if exc.__class__.__name__ else "error"
        return f"market_data/{name.lower()}"

    def _dom_set_subscription_status(self, **kwargs: Any) -> None:
        setter = getattr(self, "_telemetry_set_phase_status", None)
        if not callable(setter):
            return
        phase = getattr(self, "_PHASE_SUBSCRIPTION", "subscription")
        setter(phase, **kwargs)

    # ------------------------------------------------------------------
    def start_dom_subscription(
        self,
        *,
        symbol: str,
        depth_levels: int,
        metadata_tag: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Kick off DOM service subscription initialisation with retries."""

        self._dom_subscription_symbol = symbol or None
        self._dom_subscription_depth_levels = depth_levels
        self._dom_subscription_metadata_tag = metadata_tag
        self._dom_subscription_metadata = dict(metadata or {}) or None
        self._dom_subscription_active = False
        self._dom_subscription_retry_attempts = 0

        if not symbol or self._dom_service is None:
            return

        self._dom_submit_async(self._dom_ensure_subscription())

    # ------------------------------------------------------------------
    def stop_dom_subscription(self, *, symbol: str | None = None) -> None:
        """Stop the active DOM service subscription if possible."""

        target_symbol = symbol or self._dom_subscription_symbol
        service = self._dom_service
        self._dom_subscription_active = False
        self._dom_subscription_retry_attempts = 0
        self._dom_subscription_symbol = None
        self._dom_subscription_depth_levels = None
        self._dom_subscription_metadata_tag = None
        self._dom_subscription_metadata = None

        if service is None or not target_symbol:
            return

        stop_result = service.stop_symbol(target_symbol)
        if inspect.isawaitable(stop_result):
            self._dom_submit_async(stop_result)

    # ------------------------------------------------------------------
    def _dom_submit_async(self, coro: Awaitable[Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
        else:
            loop.create_task(coro)

    # ------------------------------------------------------------------
    async def _dom_create_subscription(self) -> None:
        symbol = self._dom_subscription_symbol
        depth_levels = self._dom_subscription_depth_levels
        service = self._dom_service
        if service is None or not symbol or depth_levels is None:
            return
        if hasattr(service, "is_running") and callable(service.is_running):
            if not service.is_running():
                await service.start()
        metadata: dict[str, Any] = {}
        if self._dom_subscription_metadata is not None:
            metadata.update(self._dom_subscription_metadata)
        metadata_tag = self._dom_subscription_metadata_tag
        if metadata_tag and "strategy" not in metadata:
            metadata["strategy"] = metadata_tag
        config = DomSymbolConfig(
            symbol=symbol,
            depth_levels=int(depth_levels),
            metadata=metadata or None,
        )
        await service.start_symbol(config)

    # ------------------------------------------------------------------
    async def _dom_ensure_subscription(self) -> None:
        if not getattr(self, "active", False):
            return
        service = self._dom_service
        symbol = self._dom_subscription_symbol
        if service is None or not symbol or self._dom_subscription_depth_levels is None:
            return
        try:
            await self._dom_create_subscription()
        except MarketDataServiceUnavailable as exc:
            self._dom_subscription_active = False
            if not getattr(self, "active", False):
                return
            if symbol != self._dom_subscription_symbol:
                return
            attempt = self._dom_subscription_retry_attempts + 1
            self._dom_subscription_retry_attempts = attempt
            delay = min(60.0, 2.0 ** (attempt - 1))
            cause_code = self._dom_exception_cause_code(exc)
            self._dom_set_subscription_status(
                status="waiting",
                status_code="awaiting_service",
                status_reason="DOM service unavailable; scheduling retry",
                status_cause=str(exc),
                status_cause_code=cause_code,
                status_details={
                    "retry_delay_seconds": delay,
                    "retry_attempt": attempt,
                    "symbol": symbol,
                },
            )
            self._telemetry_log(
                "DOM subscription unavailable via market data service; scheduling retry",
                level="WARN",
                tone="warning",
                deduplicate=False,
                details={
                    "cause_category": "market_data",
                    "cause_code": "market_data/unavailable",
                    "retry_delay_seconds": delay,
                    "attempt": attempt,
                    "exception": str(exc),
                },
            )
            self.logger.warning(
                "Failed to initialise DOM subscription for %s: %s. Retrying in %.1fs",
                symbol,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
            if getattr(self, "active", False) and symbol == self._dom_subscription_symbol:
                self._dom_submit_async(self._dom_ensure_subscription())
        except MarketDataServiceError as exc:
            self._dom_subscription_active = False
            if not getattr(self, "active", False):
                return
            if symbol != self._dom_subscription_symbol:
                return
            attempt = self._dom_subscription_retry_attempts + 1
            self._dom_subscription_retry_attempts = attempt
            delay = min(60.0, 2.0 ** (attempt - 1))
            cause_code = self._dom_exception_cause_code(exc)
            self._dom_set_subscription_status(
                status="waiting",
                status_code="retrying",
                status_reason="DOM subscription failed; scheduling retry",
                status_cause=str(exc),
                status_cause_code=cause_code,
                status_details={
                    "retry_delay_seconds": delay,
                    "retry_attempt": attempt,
                    "symbol": symbol,
                },
            )
            self._telemetry_log(
                "DOM subscription failed due to service error; scheduling retry",
                level="ERROR",
                tone="negative",
                deduplicate=False,
                details={
                    "cause_category": "strategy",
                    "cause_code": "strategy/dom_subscription_error",
                    "retry_delay_seconds": delay,
                    "attempt": attempt,
                    "exception": str(exc),
                },
            )
            self.logger.warning(
                "Failed to initialise DOM subscription for %s: %s. Retrying in %.1fs",
                symbol,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
            if getattr(self, "active", False) and symbol == self._dom_subscription_symbol:
                self._dom_submit_async(self._dom_ensure_subscription())
        else:
            self._dom_subscription_active = True
            attempts = self._dom_subscription_retry_attempts
            self._dom_subscription_retry_attempts = 0
            self._telemetry_clear_status()
            self._dom_set_subscription_status(
                status="connected",
                status_code="subscribed",
                status_reason="DOM subscription established",
                status_cause="DOM service confirmed subscription",
                status_cause_code="dom_subscription_established",
                status_details={
                    "retry_attempt": attempts,
                    "symbol": symbol,
                },
            )
            self._telemetry_log(
                "DOM subscription established",
                level="INFO",
                tone="positive",
                deduplicate=False,
                details={
                    "cause_category": "dom_service",
                    "cause_code": "dom/subscription_established",
                    "attempt": attempts,
                },
            )
            await self._verify_dom_stream_health()


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 0.0


class DomStreamHealthMixin:
    """Shared helpers for verifying DOM stream health via coordinator facades."""

    _market_data_subscription_health: Any | None
    _dom_stream_refresh_inflight: bool
    _dom_stream_missing_logged: bool
    _dom_subscription_active: bool
    _dom_subscription_retry_attempts: int

    def _coordinator_facade(self) -> Any | None:
        facade = getattr(self, "market_data_subscription_health", None)
        if facade is not None:
            return facade
        return getattr(self, "_market_data_subscription_health", None)

    async def _verify_dom_stream_health(self) -> None:
        now = datetime.now(timezone.utc)
        try:
            interval = float(getattr(self, "dom_stream_health_check_interval", 30.0))
        except Exception:
            interval = 30.0
        interval = max(interval, 1.0)
        base_threshold = max(interval, 30.0)
        stale_threshold = max(120.0, interval * 3.0, base_threshold * 2.0)
        last_rejected_at = getattr(self, "_last_dom_rejected_at", None)
        last_dom_at: datetime | None = None
        inactive_seconds: float | None = None

        telemetry = getattr(self, "runtime_telemetry", None)
        if telemetry is not None:
            try:
                key = telemetry._normalise_strategy_id(getattr(self, "identifier", None) or self.name)
                session = telemetry._require_session(key)
            except Exception:
                session = None
            if session is not None:
                last_dom_at = getattr(session, "last_dom_at", None)
                if last_dom_at is not None:
                    self._last_dom_snapshot_at = last_dom_at
                start_time = getattr(session, "start_time", now)
                inactive_seconds = (now - (last_dom_at or start_time)).total_seconds()
                if last_rejected_at is not None:
                    rejected_seconds = (now - last_rejected_at).total_seconds()
                else:
                    rejected_seconds = None
                if inactive_seconds < base_threshold:
                    self._dom_subscription_active = True
                    if getattr(self, "_dom_stream_missing_logged", False):
                        self._telemetry_log(
                            "DOM subscription healthy via recent snapshots",
                            level="INFO",
                            tone="positive",
                            deduplicate=False,
                        )
                    self._dom_stream_missing_logged = False
                    self._dom_stream_refresh_inflight = False
                    self._dom_subscription_retry_attempts = 0
                    return
                if rejected_seconds is not None and rejected_seconds < base_threshold:
                    self._dom_subscription_active = True
                    if getattr(self, "_dom_stream_missing_logged", False):
                        self._telemetry_log(
                            "DOM snapshots received but filtered; skipping refresh",
                            level="INFO",
                            tone="neutral",
                            deduplicate=False,
                        )
                    self._dom_stream_missing_logged = False
                    self._dom_stream_refresh_inflight = False
                    self._dom_subscription_retry_attempts = 0
                    return
        if inactive_seconds is None:
            last_seen = getattr(self, "_last_dom_snapshot_at", None)
            if isinstance(last_seen, datetime):
                inactive_seconds = max(0.0, (now - last_seen).total_seconds())
            else:
                inactive_seconds = None
        facade = self._coordinator_facade()
        checker = getattr(facade, "ensure_stream_active", None) if facade else None
        refresher = getattr(facade, "refresh_subscription", None) if facade else None
        if checker is None or not callable(checker):
            if inactive_seconds is not None and inactive_seconds >= stale_threshold:
                await self._force_dom_client_refresh(
                    refresher=refresher if callable(refresher) else None,
                    inactivity_seconds=inactive_seconds,
                    stale_threshold=stale_threshold,
                    last_snapshot_at=last_dom_at or getattr(self, "_last_dom_snapshot_at", None),
                )
            return
        try:
            is_active = await checker(self.name, "dom")
        except Exception:
            self.logger.exception(
                "Failed to verify DOM subscription health via market data coordinator"
            )
            if inactive_seconds is not None and inactive_seconds >= stale_threshold:
                await self._force_dom_client_refresh(
                    refresher=refresher if callable(refresher) else None,
                    inactivity_seconds=inactive_seconds,
                    stale_threshold=stale_threshold,
                    last_snapshot_at=last_dom_at or getattr(self, "_last_dom_snapshot_at", None),
                )
            return
        if is_active:
            self._dom_subscription_active = True
            coordinator_details = {
                "inactive_seconds": None if inactive_seconds is None else round(inactive_seconds, 1),
                "threshold_seconds": round(base_threshold, 1),
                "stale_threshold_seconds": round(stale_threshold, 1),
                "last_snapshot_at": last_dom_at.isoformat() if last_dom_at else None,
            }
            self._telemetry_log(
                "DOM stream active per coordinator",
                level="INFO",
                tone="neutral",
                deduplicate=True,
                details=coordinator_details,
            )
            self._dom_stream_missing_logged = False
            self._dom_stream_refresh_inflight = False
            self._dom_subscription_retry_attempts = 0
            if inactive_seconds is not None and inactive_seconds >= stale_threshold:
                await self._force_dom_client_refresh(
                    refresher=refresher,
                    inactivity_seconds=inactive_seconds,
                    stale_threshold=stale_threshold,
                    last_snapshot_at=last_dom_at,
                )
            return
        self._dom_subscription_active = False
        if last_rejected_at is not None:
            if (now - last_rejected_at).total_seconds() < base_threshold:
                if getattr(self, "_dom_stream_missing_logged", False):
                    self._telemetry_log(
                        "DOM snapshots received but filtered; skipping refresh",
                        level="INFO",
                        tone="neutral",
                        deduplicate=False,
                    )
                self._dom_stream_missing_logged = False
                self._dom_stream_refresh_inflight = False
                self._dom_subscription_retry_attempts = 0
                return
        if not getattr(self, "_dom_stream_missing_logged", False):
            self._telemetry_log(
                "DOM subscription inactive; awaiting coordinator refresh",
                level="WARN",
                tone="warning",
                deduplicate=False,
            )
            self._dom_stream_missing_logged = True
        if callable(refresher) and not getattr(self, "_dom_stream_refresh_inflight", False):
            self._dom_stream_refresh_inflight = True
            try:
                refreshed = await refresher(self.name)
            except Exception:
                self.logger.exception(
                    "Failed to request DOM subscription refresh via coordinator"
                )
                self._telemetry_log(
                    "DOM subscription refresh request failed",
                    level="ERROR",
                    tone="negative",
                    deduplicate=False,
                )
                self._dom_stream_refresh_inflight = False
            else:
                if refreshed:
                    self._telemetry_log(
                        "Requested DOM subscription refresh via coordinator",
                        level="INFO",
                        tone="neutral",
                        deduplicate=False,
                    )
                else:
                    try:
                        resolved_channel = self._resolve_channel(self.dom_channel)
                    except Exception:
                        resolved_channel = self.dom_channel
                    details = {
                        "symbol": self.symbol,
                        "subscription_id": self.subscription_id,
                        "resolved_channel": resolved_channel,
                        "dom_channel": self.dom_channel,
                    }
                    try:
                        now = datetime.now(timezone.utc)
                        last_rejected_at = getattr(self, "_last_dom_rejected_at", None)
                        if last_rejected_at is not None:
                            details["recent_filtered_seconds"] = round((now - last_rejected_at).total_seconds(), 1)
                    except Exception:
                        pass
                    self._telemetry_log(
                        "DOM subscription refresh request rejected",
                        level="ERROR",
                        tone="negative",
                        deduplicate=False,
                        details=details,
                    )
                    if inactive_seconds is not None and inactive_seconds >= stale_threshold:
                        await self._force_dom_client_refresh(
                            refresher=None,
                            inactivity_seconds=inactive_seconds,
                            stale_threshold=stale_threshold,
                            last_snapshot_at=last_dom_at or getattr(self, "_last_dom_snapshot_at", None),
                        )
                self._dom_stream_refresh_inflight = False


@dataclass
class DOMSubscriptionStrategy(DomStreamHealthMixin, DomServiceSubscriptionMixin, BaseStrategy):
    """Base strategy that subscribes to Redis DOM snapshots and emits events."""

    symbol: str = ""
    subscription_id: str | None = None
    dom_channel: str = "market.dom"
    redis_channel_prefix: str | None = None
    depth_levels: int = 10
    dom_metadata_tag: str | None = None
    dom_stream_health_check_interval: float = 30.0

    strategy_type: ClassVar[str] = "DOMSubscriptionStrategy"
    data_feed_mode: ClassVar[str] = "dom"
    required_market_data_streams: ClassVar[tuple[str, ...]] = ("dom",)
    _PHASE_SUBSCRIPTION: ClassVar[str] = "subscription"
    parameter_definitions: ClassVar[Mapping[str, Mapping[str, Any]]] = {
        "symbol": {
            "type": "str",
            "allow_null": True,
            "default": "",
            "description": "Symbol used when filtering incoming DOM snapshots.",
        },
        "subscription_id": {
            "type": "str",
            "allow_null": True,
            "default": None,
            "description": "Explicit subscription identifier for DOM snapshots.",
        },
        "dom_channel": {
            "type": "str",
            "default": "market.dom",
            "description": "Redis channel used to consume DOM snapshot updates.",
        },
        "redis_channel_prefix": {
            "type": "str",
            "allow_null": True,
            "default": "",
            "description": "Optional prefix applied to the configured DOM channel.",
        },
        "depth_levels": {
            "type": "int",
            "default": 10,
            "min": 1,
            "description": "Depth levels requested from the DOM service when subscribing.",
        },
        "dom_metadata_tag": {
            "type": "str",
            "allow_null": True,
            "default": None,
            "description": "Optional identifier inserted into DOM service subscription metadata.",
        },
        "dom_stream_health_check_interval": {
            "type": "float",
            "default": 30.0,
            "min": 1.0,
            "max": 3600.0,
            "description": "Interval in seconds between DOM stream health checks.",
        },
    }
    default_parameters: ClassVar[Mapping[str, Any]] = {
        "symbol": "",
        "subscription_id": None,
        "dom_channel": "market.dom",
        "redis_channel_prefix": "",
        "depth_levels": 10,
        "dom_metadata_tag": None,
        "dom_stream_health_check_interval": 30.0,
    }

    _pubsub: PubSubChannel | None = field(default=None, init=False, repr=False)
    _dispatch_event: _Dispatcher | None = field(default=None, init=False, repr=False)
    _listener_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False
    )
    _latest_snapshot: Mapping[str, Any] | None = field(
        default=None, init=False, repr=False
    )
    runtime_telemetry: DomRuntimeTelemetryService | None = field(
        default=None, init=False, repr=False
    )
    _telemetry_cached_session_key: StrategyIdentifier | None = field(
        default=None, init=False, repr=False
    )
    _last_runtime_status: str | None = field(default=None, init=False, repr=False)
    _dom_service: Any | None = field(default=None, init=False, repr=False)
    _dom_subscription_active: bool = field(default=False, init=False, repr=False)
    _dom_subscription_retry_attempts: int = field(default=0, init=False, repr=False)
    _dom_subscription_symbol: str | None = field(default=None, init=False, repr=False)
    _dom_subscription_depth_levels: int | None = field(
        default=None, init=False, repr=False
    )
    _dom_subscription_metadata_tag: str | None = field(
        default=None, init=False, repr=False
    )
    _dom_subscription_metadata: Mapping[str, Any] | None = field(
        default=None, init=False, repr=False
    )
    _subscription_id_auto_derived: bool = field(default=False, init=False, repr=False)
    _service_subscription_id: str | None = field(default=None, init=False, repr=False)
    _market_data_subscription_health: Any | None = field(
        default=None, init=False, repr=False
    )
    _dom_stream_refresh_inflight: bool = field(default=False, init=False, repr=False)
    _dom_stream_missing_logged: bool = field(default=False, init=False, repr=False)
    _dom_stream_health_check_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False
    )
    _dom_stream_health_check_interval: float = field(
        default=30.0, init=False, repr=False
    )
    _last_dom_rejected_at: datetime | None = field(default=None, init=False, repr=False)
    _last_dom_snapshot_at: datetime | None = field(default=None, init=False, repr=False)
    _dom_forced_refresh_next_at: float = field(default=0.0, init=False, repr=False)
    _dom_stale_log_next_at: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:  # noqa: D401 - defer to parent docstring
        super().__post_init__()
        self.set_parameter_definitions(self.parameter_definitions)
        for name, default in self.default_parameters.items():
            current = getattr(self, name, None)
            is_empty_current = (
                current is None or (isinstance(current, str) and current == "")
            )
            if is_empty_current and default not in (None, ""):
                setattr(self, name, default)
        if self.symbol:
            self.symbol = self.symbol.upper()
        if self.subscription_id is not None:
            text = str(self.subscription_id).strip()
            self.subscription_id = text or None
        if isinstance(self.redis_channel_prefix, str):
            self.redis_channel_prefix = self.redis_channel_prefix.strip()
        self._subscription_id_auto_derived = False
        self.depth_levels = max(1, int(self.depth_levels or 0))
        if self.dom_metadata_tag is not None:
            text = str(self.dom_metadata_tag).strip()
            self.dom_metadata_tag = text or None
        if not self.subscription_id and self.symbol:
            self.subscription_id = self.symbol
            self._subscription_id_auto_derived = True
        try:
            interval = float(self.dom_stream_health_check_interval)
        except (TypeError, ValueError):
            interval = float(self.default_parameters["dom_stream_health_check_interval"])
        interval = min(3600.0, max(1.0, interval))
        self.dom_stream_health_check_interval = interval
        self._dom_stream_health_check_interval = interval
        self._dom_forced_refresh_next_at = 0.0
        self._dom_stale_log_next_at = 0.0

    # ------------------------------------------------------------------
    def _normalise_parameter_value(self, name: str, value: Any) -> Any:
        if name == "symbol":
            if value is None:
                return ""
            if isinstance(value, str):
                return value.strip().upper()
            return str(value).upper()
        if name == "subscription_id":
            if value is None:
                return None
            text = str(value).strip()
            return text or None
        if name == "dom_metadata_tag":
            if value is None:
                return None
            return str(value).strip() or None
        if name == "dom_stream_health_check_interval":
            try:
                parsed = float(value)
            except (TypeError, ValueError):  # pragma: no cover - defensive
                return self.default_parameters["dom_stream_health_check_interval"]
            return min(3600.0, max(1.0, parsed))
        if name == "depth_levels":
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return 1
            return max(1, parsed)
        return super()._normalise_parameter_value(name, value)

    # ------------------------------------------------------------------
    def set_dependencies(
        self,
        *,
        pubsub: PubSubChannel | None = None,
        event_dispatcher: Callable[[str, Mapping[str, Any]], Awaitable[None]] | None = None,
        runtime_telemetry: DomRuntimeTelemetryService | None = None,
        dom_service: Any | None = None,
        **dependencies: Any,
    ) -> None:
        """Inject Redis pubsub and event dispatcher dependencies."""

        super().set_dependencies(**dependencies)
        if pubsub is not None:
            self._pubsub = pubsub
        if event_dispatcher is not None:
            async def _dispatch(payload: Mapping[str, Any]) -> None:
                result = event_dispatcher(self.name, payload)
                if asyncio.iscoroutine(result):
                    await result
            self._dispatch_event = _dispatch
        if runtime_telemetry is not None:
            self.runtime_telemetry = runtime_telemetry
        if dom_service is not None:
            self._dom_service = dom_service
        prefix_value = dependencies.get("redis_channel_prefix")
        if isinstance(prefix_value, str):
            token = prefix_value.strip()
            if token or self.redis_channel_prefix != token:
                self.redis_channel_prefix = token
        dom_channel_value = dependencies.get("dom_channel")
        if isinstance(dom_channel_value, str):
            text = dom_channel_value.strip()
            if text and text != self.dom_channel:
                self.dom_channel = text
        facade = dependencies.get("market_data_subscription_health")
        if facade is None:
            facade = getattr(self, "market_data_subscription_health", None)
        if facade is not None:
            self._market_data_subscription_health = facade

    # ------------------------------------------------------------------
    def start(self) -> bool:
        if not self.symbol:
            message = "DOM subscription requires a symbol"
            self.logger.warning(message)
            raise StrategyError(message)
        started = super().start()
        if not started:
            return False
        # Ensure telemetry session is created before any phase status updates
        self._telemetry_start_session()
        # When a DOM service is injected we can operate without Redis pubsub.
        # If neither pubsub nor service is available, fail fast.
        if self._pubsub is None and self._dom_service is None:
            super().stop()
            message = "DOM subscription requires Redis pubsub or DOM service dependency"
            self.logger.warning(message)
            self._telemetry_set_phase_status(
                self._PHASE_SUBSCRIPTION,
                status="failed",
                status_code="error",
                status_reason=message,
                status_cause=message,
                status_cause_code="dom_subscription_missing_dependencies",
            )
            raise StrategyError(message)
        self._telemetry_set_phase_status(
            self._PHASE_SUBSCRIPTION,
            status="initialising",
            status_code="starting",
            status_reason="Initialising DOM subscription",
            status_cause="Strategy starting",
            status_cause_code="dom_subscription_starting",
        )
        # 订阅依赖诊断输出：记录解析后的频道与订阅标识，便于前端定位
        try:
            resolved_channel = self._resolve_channel(self.dom_channel)
        except Exception:
            resolved_channel = self.dom_channel
        self._telemetry_log(
            "DOM subscription initialising",
            level="INFO",
            tone="neutral",
            deduplicate=False,
            details={
                "symbol": self.symbol,
                "subscription_id": self.subscription_id,
                "dom_channel": self.dom_channel,
                "resolved_channel": resolved_channel,
                "redis_channel_prefix": (self.redis_channel_prefix or ""),
            },
        )

        if self.symbol and self._dom_service is not None:
            metadata_tag = self.dom_metadata_tag or self.name
            self._telemetry_set_phase_status(
                self._PHASE_SUBSCRIPTION,
                status="connecting",
                status_code="initialising",
                status_reason="Requesting DOM service subscription",
                status_cause="Awaiting DOM service confirmation",
                status_cause_code="dom_subscription_requesting",
                status_details={
                    "symbol": self.symbol,
                    "depth_levels": self.depth_levels,
                },
            )
            self.start_dom_subscription(
                symbol=self.symbol,
                depth_levels=self.depth_levels,
                metadata_tag=metadata_tag,
            )
            # If the DOM service supports listeners and the strategy exposes a
            # DOM snapshot handler, register it to receive live snapshots.
            callback = getattr(self, "_on_dom_snapshot", None)
            registrar = getattr(self._dom_service, "register_listener", None)
            if callable(registrar) and callable(callback):
                remover = registrar(callback)
                if callable(remover):
                    setattr(self, "_listener_remove", remover)
        if self._pubsub is not None and self._dispatch_event is not None:
            loop = asyncio.get_running_loop()
            self._listener_task = loop.create_task(
                self._run_listener(), name=f"{self.name}-dom-listener"
            )
        self._start_dom_stream_health_check()
        return True

    # ------------------------------------------------------------------
    async def on_start(self) -> None:
        self._start_dom_stream_health_check()

    # ------------------------------------------------------------------
    def stop(self) -> bool:
        stopped = super().stop()
        if stopped:
            self._cancel_listener()
            self._cancel_dom_stream_health_check()
            if self._dom_service is not None and self.symbol:
                self.stop_dom_subscription(symbol=self.symbol)
            remover = getattr(self, "_listener_remove", None)
            if callable(remover):
                try:
                    remover()
                except Exception:
                    pass
                setattr(self, "_listener_remove", None)
            self._telemetry_set_phase_status(
                self._PHASE_SUBSCRIPTION,
                status="stopped",
                status_code="stopped",
                status_reason="DOM subscription stopped",
                status_cause="Strategy stopped",
                status_cause_code="dom_subscription_stopped",
            )
            self._telemetry_stop_session("DOM subscription stopped")
        return stopped

    # ------------------------------------------------------------------
    async def on_stop(self) -> None:
        self._cancel_dom_stream_health_check()

    # ------------------------------------------------------------------
    def _cancel_listener(self) -> None:
        task = self._listener_task
        if task is None:
            return
        task.cancel()
        self._listener_task = None

    # ------------------------------------------------------------------
    def _restart_dom_listener_task(self) -> bool:
        try:
            self._cancel_listener()
        except Exception:
            pass
        if not getattr(self, "active", False):
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        try:
            self._listener_task = loop.create_task(
                self._run_listener(), name=f"{self.name}-dom-listener"
            )
        except Exception:
            self.logger.exception("Failed to create DOM listener task")
            return False
        return True

    # ------------------------------------------------------------------
    async def recover_streams(self, streams: set[str], reason: str | None = None) -> None:
        if not getattr(self, "active", False):
            return
        if "dom" not in streams:
            return
        now = datetime.now(timezone.utc)
        details = {
            "streams": ["dom"],
            "reason": reason or "stream_recovery_requested",
        }
        self._telemetry_log(
            "Strategy stream recovery requested",
            level="WARN",
            tone="warning",
            deduplicate=False,
            details=details,
            timestamp=now,
        )
        self._restart_dom_listener_task()
        symbol = self._dom_subscription_symbol or self.symbol
        if symbol and self._dom_service is not None:
            depth_levels = self._dom_subscription_depth_levels or self.depth_levels
            metadata_tag = (
                self._dom_subscription_metadata_tag or self.dom_metadata_tag or self.name
            )
            metadata = self._dom_subscription_metadata
            try:
                self.stop_dom_subscription(symbol=symbol)
            except Exception:
                pass
            self.start_dom_subscription(
                symbol=symbol,
                depth_levels=depth_levels,
                metadata_tag=metadata_tag,
                metadata=metadata,
            )

    # ------------------------------------------------------------------
    async def _run_listener(self) -> None:
        assert self._pubsub is not None
        dispatcher = self._dispatch_event
        if dispatcher is None:
            return
        channel = self._resolve_channel(self.dom_channel)
        backoff = 1.0
        try:
            while self.active:
                try:
                    stream = await self._pubsub.listen(channel)
                    try:
                        async for message in stream:
                            if not self.active:
                                break
                            if not isinstance(message, Mapping):
                                continue
                            if not self._accept_snapshot(message):
                                try:
                                    candidate_id = message.get("subscription_id")
                                    symbol = message.get("symbol")
                                except Exception:
                                    candidate_id = None
                                    symbol = None
                                self._telemetry_log(
                                    "DOM snapshot rejected",
                                    level="INFO",
                                    tone="neutral",
                                    deduplicate=False,
                                    details={
                                        "candidate_subscription_id": candidate_id,
                                        "candidate_symbol": symbol,
                                        "configured_subscription_id": self.subscription_id,
                                        "configured_symbol": self.symbol,
                                    },
                                )
                                self._last_dom_rejected_at = datetime.now(timezone.utc)
                                continue
                            snapshot = dict(message)
                            self._telemetry_record_snapshot(snapshot)
                            status_token = str(snapshot.get("status") or "").strip().lower()
                            if status_token == "no_data":
                                await self._on_no_data_snapshot(snapshot)
                            self._latest_snapshot = snapshot
                            event = self._build_dom_event(snapshot)
                            try:
                                # Dispatch to self for local strategy processing
                                if hasattr(self, "on_market_event") and callable(self.on_market_event):
                                    await self.on_market_event(event)
                                # Dispatch to external coordinator if configured
                                if dispatcher is not None:
                                    await dispatcher(event)
                            except Exception:
                                self.logger.exception("DOM event dispatch failed")
                                self._telemetry_log(
                                    "DOM event dispatch failed",
                                    level="ERROR",
                                    tone="negative",
                                )
                        backoff = 1.0
                    finally:
                        await stream.aclose()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("DOM listener crashed")
                    self._telemetry_log(
                        "DOM listener crashed",
                        level="ERROR",
                        tone="negative",
                    )
                if not self.active:
                    break
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)
        finally:
            self._listener_task = None
            self._telemetry_log(
                "DOM listener stopped",
                level="INFO",
                tone="neutral",
            )

    async def _on_no_data_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        facade = self._coordinator_facade()
        if facade is None:
            return
        if self._dom_stream_refresh_inflight:
            return
        refresher = getattr(facade, "refresh_subscription", None)
        if not callable(refresher):
            return
        self._dom_stream_refresh_inflight = True
        try:
            ok = await refresher(self.name)
        except Exception:
            self._dom_stream_refresh_inflight = False
            self._telemetry_log(
                "DOM no_data snapshot; refresh failed",
                level="ERROR",
                tone="negative",
                deduplicate=False,
            )
            return
        if ok:
            self._telemetry_log(
                "DOM no_data snapshot; refresh requested",
                level="INFO",
                tone="neutral",
                deduplicate=False,
            )
        else:
            self._telemetry_log(
                "DOM no_data snapshot; refresh rejected",
                level="WARN",
                tone="warning",
                deduplicate=False,
            )
        self._dom_stream_refresh_inflight = False

    async def _force_dom_client_refresh(
        self,
        *,
        refresher: Callable[[str], Awaitable[bool]] | None,
        inactivity_seconds: float,
        stale_threshold: float,
        last_snapshot_at: datetime | None,
    ) -> None:
        now_monotonic = asyncio.get_running_loop().time()
        if now_monotonic < self._dom_forced_refresh_next_at:
            return
        cooldown = max(
            float(getattr(self, "dom_stream_health_check_interval", 30.0)),
            float(stale_threshold),
            10.0,
        )
        self._dom_forced_refresh_next_at = now_monotonic + cooldown
        self._dom_stream_refresh_inflight = False
        self._dom_subscription_retry_attempts = 0
        details = {
            "inactive_seconds": round(inactivity_seconds, 1),
            "stale_threshold_seconds": round(stale_threshold, 1),
            "last_snapshot_at": last_snapshot_at.isoformat() if last_snapshot_at else None,
        }
        if now_monotonic >= self._dom_stale_log_next_at:
            self._dom_stale_log_next_at = now_monotonic + cooldown
            self._telemetry_log(
                "DOM stream stale; forcing client-side refresh",
                level="WARN",
                tone="warning",
                deduplicate=False,
                details=details,
            )
            self.logger.warning(
                "DOM stream stale for %s (%.1fs >= %.1fs); forcing client-side refresh",
                self.name,
                inactivity_seconds,
                stale_threshold,
            )

        if not self._restart_dom_listener_task():
            self._telemetry_log(
                "DOM listener restart failed during stale recovery",
                level="ERROR",
                tone="negative",
                deduplicate=False,
                details=details,
            )

        if callable(refresher):
            try:
                refreshed = await refresher(self.name)
            except Exception:
                self.logger.exception("Forced DOM refresh via coordinator failed")
                self._telemetry_log(
                    "Forced DOM refresh via coordinator failed",
                    level="ERROR",
                    tone="negative",
                    deduplicate=False,
                    details=details,
                )
            else:
                self._telemetry_log(
                    "Forced DOM refresh requested via coordinator",
                    level="INFO",
                    tone="neutral" if refreshed else "warning",
                    deduplicate=False,
                    details={**details, "refresh_accepted": refreshed},
                )

        symbol = self._dom_subscription_symbol or self.symbol
        depth_levels = self._dom_subscription_depth_levels or self.depth_levels
        metadata_tag = (
            self._dom_subscription_metadata_tag
            or self.dom_metadata_tag
            or self.name
        )
        metadata = self._dom_subscription_metadata
        if symbol and self._dom_service is not None:
            self.stop_dom_subscription(symbol=symbol)
            self.start_dom_subscription(
                symbol=symbol,
                depth_levels=depth_levels,
                metadata_tag=metadata_tag,
                metadata=metadata,
            )
            self._telemetry_log(
                "Forced DOM service subscription restart requested",
                level="INFO",
                tone="neutral",
                deduplicate=False,
                details={**details, "symbol": symbol, "depth_levels": depth_levels},
            )

    async def _restart_dom_listener_if_inactive(self) -> None:
        try:
            telemetry = getattr(self, "runtime_telemetry", None)
            if telemetry is None:
                return
            key = telemetry._normalise_strategy_id(getattr(self, "identifier", None) or self.name)
            session = telemetry._require_session(key)
        except Exception:
            return

        now = datetime.now(timezone.utc)
        last_at = getattr(session, "last_dom_at", None)
        start_time = getattr(session, "start_time", now)
        threshold = max(float(getattr(self, "dom_stream_health_check_interval", 30.0)) * 2.0, 120.0)
        inactive_seconds = (now - (last_at or start_time)).total_seconds()
        if inactive_seconds < threshold:
            return

        task = getattr(self, "_listener_task", None)
        if task is None:
            if self._restart_dom_listener_task():
                return
            self.logger.error("Failed to restart DOM listener after inactivity")
            self._telemetry_log(
                "Failed to restart DOM listener",
                level="ERROR",
                tone="negative",
                deduplicate=False,
            )
            return
        if not getattr(self, "active", False):
            return

        self._telemetry_log(
            "DOM consumer inactivity detected; restarting listener",
            level="WARN",
            tone="warning",
            deduplicate=False,
            details={
                "inactive_seconds": round(inactive_seconds, 1),
                "threshold_seconds": round(threshold, 1),
            },
        )

        if not self._restart_dom_listener_task():
            self.logger.error("Failed to restart DOM listener after inactivity")
            self._telemetry_log(
                "Failed to restart DOM listener",
                level="ERROR",
                tone="negative",
                deduplicate=False,
            )

    # ------------------------------------------------------------------
    def _start_dom_stream_health_check(self) -> None:
        task = self._dom_stream_health_check_task
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - defensive when loop missing
            return
        self._dom_stream_health_check_task = loop.create_task(
            self._run_dom_stream_health_checks(),
            name=f"{self.name}-dom-health-check",
        )
        self.logger.info("DOM stream health check task started")

    # ------------------------------------------------------------------
    def _cancel_dom_stream_health_check(self) -> None:
        task = self._dom_stream_health_check_task
        if task is not None and not task.done():
            task.cancel()
        self._dom_stream_health_check_task = None

    # ------------------------------------------------------------------
    async def _run_dom_stream_health_checks(self) -> None:
        try:
            while self.active and self.enabled:
                try:
                    await self._verify_dom_stream_health()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("DOM stream health check failed")
                finally:
                    self._dom_stream_refresh_inflight = False
                try:
                    await self._restart_dom_listener_if_inactive()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("DOM listener inactivity watchdog failed")
                await asyncio.sleep(self._dom_stream_health_check_interval)
        except asyncio.CancelledError:
            return
        finally:
            self._dom_stream_health_check_task = None

    # ------------------------------------------------------------------
    def apply_parameter_updates(self, updates: Mapping[str, Any]) -> Dict[str, Any]:
        if not updates:
            return {}

        previous_symbol = self.symbol
        applied = super().apply_parameter_updates(updates)

        subscription_updated = "subscription_id" in updates
        symbol_updated = "symbol" in applied and applied["symbol"] != previous_symbol

        if subscription_updated:
            self._service_subscription_id = None
            if self.subscription_id:
                self._subscription_id_auto_derived = False
            else:
                self._subscription_id_auto_derived = False
                derived = (self.symbol or "").strip()
                if derived:
                    self.subscription_id = derived
                    self._subscription_id_auto_derived = True
                applied["subscription_id"] = self.subscription_id

        if symbol_updated:
            self._service_subscription_id = None
            if self._subscription_id_auto_derived:
                derived = (self.symbol or "").strip()
                self.subscription_id = derived or None
                applied["subscription_id"] = self.subscription_id

        return applied

    # ------------------------------------------------------------------
    def _resolve_channel(self, name: str) -> str:
        prefix = (self.redis_channel_prefix or "").strip()
        base_token = name.strip()
        if not base_token:
            return base_token
        if any(token in base_token for token in ("*", "?")):
            return base_token
        base_channel = f"{prefix}{base_token}" if prefix else base_token
        identifier = (self.subscription_id or self.symbol or "").strip()
        wildcard_prefix = "" if prefix else "*"
        if identifier:
            suffix = identifier.upper()
            if base_channel.upper().endswith(f"-{suffix}"):
                return f"{wildcard_prefix}{base_channel}"
            if base_channel.endswith("-"):
                return f"{wildcard_prefix}{base_channel}{suffix}"
            return f"{wildcard_prefix}{base_channel}-{suffix}"
        if base_channel.endswith("-"):
            return f"{wildcard_prefix}{base_channel}*"
        return f"{wildcard_prefix}{base_channel}-*"

    # ------------------------------------------------------------------
    def _accept_snapshot(self, snapshot: Mapping[str, Any]) -> bool:
        def _adopt(candidate_id: str) -> None:
            if not candidate_id or not self._subscription_id_auto_derived:
                return
            if self._service_subscription_id:
                return
            text = candidate_id.strip()
            if not text:
                return
            self._service_subscription_id = text
            self._subscription_id_auto_derived = False
            self._telemetry_log(
                "Adopted service subscription id",
                level="INFO",
                tone="neutral",
                deduplicate=False,
                details={
                    "adopted_subscription_id": text,
                    "configured_subscription_id": (self.subscription_id or ""),
                    "symbol": (self.symbol or ""),
                },
            )

        configured_id = (self.subscription_id or "").strip()
        override_id = (self._service_subscription_id or "").strip()
        target_id = override_id or configured_id
        candidate = str(snapshot.get("subscription_id", "")).strip()
        if not candidate:
            meta = snapshot.get("metadata")
            if isinstance(meta, Mapping):
                raw = meta.get("subscription_id") or meta.get("subscriptionId")
                if isinstance(raw, str):
                    candidate = raw.strip()
        snapshot_symbol = str(snapshot.get("symbol", "")).strip()
        configured_symbol = (self.symbol or "").strip()

        if not target_id:
            if candidate:
                _adopt(candidate)
            return True

        if candidate:
            if candidate == target_id:
                return True
            if candidate.upper() == target_id.upper():
                if candidate != target_id:
                    _adopt(candidate)
                return True

        if snapshot_symbol and configured_symbol:
            if snapshot_symbol.upper() == configured_symbol.upper():
                if candidate and candidate.upper() != target_id.upper():
                    _adopt(candidate)
                return True

        return False

    # ------------------------------------------------------------------
    def _build_dom_event(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        bids = snapshot.get("bids") or []
        asks = snapshot.get("asks") or []
        bid_volume = sum(_coerce_float(level.get("size")) for level in bids)
        ask_volume = sum(_coerce_float(level.get("size")) for level in asks)
        event = {
            "type": "dom",
            "symbol": snapshot.get("symbol") or self.symbol,
            "subscription_id": snapshot.get("subscription_id")
            or self.subscription_id,
            "timestamp": snapshot.get("timestamp"),
            "bid_volume": bid_volume,
            "ask_volume": ask_volume,
            "mid_price": snapshot.get("mid_price"),
            "spread": snapshot.get("spread"),
            "best_bid": snapshot.get("best_bid"),
            "best_ask": snapshot.get("best_ask"),
            "depth": snapshot.get("depth"),
            "total_bid_size": snapshot.get("total_bid_size", bid_volume),
            "total_ask_size": snapshot.get("total_ask_size", ask_volume),
            "snapshot": snapshot,
        }
        return event

    # ------------------------------------------------------------------
    def latest_snapshot(self) -> Mapping[str, Any] | None:
        snapshot = self._latest_snapshot
        if snapshot is None:
            return None
        return dict(snapshot)

    # ------------------------------------------------------------------
    def _telemetry(self) -> DomRuntimeTelemetryService | None:
        return self.runtime_telemetry

    def _telemetry_strategy_id(self) -> StrategyIdentifier:
        identifier = getattr(self, "identifier", None)
        return identifier if identifier is not None else self.name

    def _telemetry_identifier_candidates(self) -> tuple[StrategyIdentifier, ...]:
        candidates: list[StrategyIdentifier] = []
        cached = self._telemetry_cached_session_key
        if cached is not None:
            candidates.append(cached)
        current_raw = self._telemetry_strategy_id()
        current = normalize_strategy_identifier(current_raw)
        if current is not None:
            if current not in candidates:
                candidates.append(current)
        elif not candidates:
            candidates.append(current_raw)
        return tuple(candidates)

    def _telemetry_warn_missing_session(
        self, candidates: tuple[StrategyIdentifier, ...], operation: str
    ) -> None:
        if not candidates:
            return
        self.logger.warning(
            "Telemetry session not found for identifiers %s while %s",
            candidates,
            operation,
        )

    def _telemetry_start_session(self) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        strategy_id = self._telemetry_strategy_id()
        self._telemetry_cached_session_key = None
        try:
            telemetry.start_session(
                strategy_id,
                subscription_id=self.subscription_id,
                symbol=self.symbol or None,
            )
        except Exception:  # pragma: no cover - defensive
            self._telemetry_cached_session_key = None
            self.logger.exception("Failed to start telemetry session")
        else:
            cached = normalize_strategy_identifier(strategy_id)
            self._telemetry_cached_session_key = cached

    def _telemetry_stop_session(self, reason: str | None = None) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        candidates = self._telemetry_identifier_candidates()
        last_error: KeyError | None = None
        for candidate in candidates:
            try:
                telemetry.stop_session(candidate, reason=reason)
            except KeyError as exc:
                last_error = exc
                continue
            except Exception:  # pragma: no cover - defensive
                self.logger.exception("Failed to stop telemetry session")
                return
            else:
                self._telemetry_cached_session_key = None
                return
        if last_error is not None:
            self._telemetry_warn_missing_session(candidates, "stopping telemetry session")

    def _telemetry_record_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        timestamp = self._parse_timestamp(snapshot.get("timestamp"))
        if timestamp is not None:
            self._last_dom_snapshot_at = timestamp
        subscription_id = snapshot.get("subscription_id") or self.subscription_id
        symbol = snapshot.get("symbol") or self.symbol or None
        if subscription_id is not None:
            subscription_id = str(subscription_id)
        if symbol is not None:
            symbol = str(symbol)
        candidates = self._telemetry_identifier_candidates()
        last_error: KeyError | None = None
        for candidate in candidates:
            try:
                telemetry.record_dom_snapshot(
                    candidate,
                    timestamp=timestamp,
                    subscription_id=subscription_id,
                    symbol=symbol,
                )
            except KeyError as exc:
                last_error = exc
                continue
            except Exception:  # pragma: no cover - defensive
                self.logger.exception("Failed to record DOM snapshot telemetry")
                return
            else:
                return
        if last_error is not None:
            self._telemetry_warn_missing_session(
                candidates, "recording DOM snapshot telemetry"
            )

    def _telemetry_log(
        self,
        message: str,
        *,
        level: str = "INFO",
        tone: str = "neutral",
        deduplicate: bool = True,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        if deduplicate and message == self._last_runtime_status:
            return
        candidates = self._telemetry_identifier_candidates()
        last_error: KeyError | None = None
        for candidate in candidates:
            try:
                telemetry.log_event(
                    candidate,
                    message,
                    level=level,
                    tone=tone,
                    details=details,
                )
            except KeyError as exc:
                last_error = exc
                continue
            except Exception:  # pragma: no cover - defensive
                self.logger.exception("Failed to log telemetry event")
                return
            else:
                if deduplicate:
                    self._last_runtime_status = message
                else:
                    self._last_runtime_status = None
                return
        if last_error is not None:
            self._telemetry_warn_missing_session(
                candidates, "logging telemetry event"
            )

    def _telemetry_set_phase_status(
        self,
        phase: str,
        *,
        status: str | None,
        status_code: str | None = None,
        status_reason: str | None = None,
        status_cause: str | None = None,
        status_cause_code: str | None = None,
        status_details: Mapping[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        candidates = self._telemetry_identifier_candidates()
        last_error: KeyError | None = None
        for candidate in candidates:
            try:
                telemetry.set_phase_status(
                    candidate,
                    phase,
                    status=status,
                    status_code=status_code,
                    status_reason=status_reason,
                    status_cause=status_cause,
                    status_cause_code=status_cause_code,
                    status_details=status_details,
                    timestamp=timestamp,
                )
            except KeyError as exc:
                last_error = exc
                continue
            except Exception:  # pragma: no cover - defensive
                self.logger.exception("Failed to set telemetry phase status")
                return
            else:
                return
        if last_error is not None:
            self._telemetry_warn_missing_session(
                candidates, "setting telemetry phase status"
            )

    def _telemetry_clear_status(self) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        candidates = self._telemetry_identifier_candidates()
        last_error: KeyError | None = None
        for candidate in candidates:
            try:
                telemetry.clear_status_cause(candidate)
            except KeyError as exc:
                last_error = exc
                continue
            except Exception:  # pragma: no cover - defensive
                self.logger.exception("Failed to clear telemetry status")
                return
            else:
                return
        if last_error is not None:
            self._telemetry_warn_missing_session(
                candidates, "clearing telemetry status"
            )

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        return None
