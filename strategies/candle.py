"""Candle aggregation helpers for market data driven strategies."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
import threading
from threading import Event, RLock
import time
from typing import (
    Any,
    Awaitable,
    Callable,
    ClassVar,
    Deque,
    Mapping,
    MutableMapping,
    Sequence,
)

from src.common.market_data.aggregation import (
    MinuteBarAggregator,
    floor_timestamp as _floor_timestamp,
    normalize_interval_token as _normalize_interval_token,
)
from src.data_layer import (
    BusSubscriptionToken,
    DataSourceError,
    DataSourceManagerProtocol,
    DataSubscriptionRequest,
    DataSubscriptionToken,
    EventEnvelope,
    get_data_layer_mode,
    get_data_source_manager,
    is_unified_mode,
)
from src.market_data.history_chunks import HistoryReplayConfig, load_history_with_backoff
from src.strategy.base import BaseStrategy
from src.strategy.runtime import DomRuntimeTelemetryService
from src.strategy.types import StrategyIdentifier

try:  # pragma: no cover - optional typing dependency
    from src.redis_client.pubsub import PubSubChannel
except Exception:  # pragma: no cover - fallback when redis client unavailable
    PubSubChannel = Any  # type: ignore[misc, assignment]


_Dispatcher = Callable[[Mapping[str, Any]], Awaitable[None]]
_HistoryLoader = Callable[[str, str, int], Awaitable[Sequence[Mapping[str, Any]]]]


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return datetime.now(timezone.utc)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            ts = datetime.fromisoformat(text)
        except ValueError:
            ts = datetime.now(timezone.utc)
    else:
        try:
            ts = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:  # pragma: no cover - defensive
            ts = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return default


def _interval_to_delta(interval: str) -> timedelta:
    lookup = {
        "1s": timedelta(seconds=1),
        "5s": timedelta(seconds=5),
        "10s": timedelta(seconds=10),
        "15s": timedelta(seconds=15),
        "30s": timedelta(seconds=30),
        "1m": timedelta(minutes=1),
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
        "1d": timedelta(days=1),
    }
    interval_key = interval.lower()
    delta = lookup.get(interval_key)
    if delta is None:
        raise ValueError(f"Unsupported candle interval '{interval}'")
    return delta


def _interval_seconds(interval: str | None) -> int | None:
    token = _normalize_interval_token(interval) if interval else None
    if not token:
        return None
    unit = token[-1]
    if unit not in ("s", "m", "h", "d"):
        return None
    try:
        value = int(token[:-1])
    except ValueError:
        return None
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return value * multiplier


@dataclass
class CandleSubscriptionStrategy(BaseStrategy):
    """Base strategy that aggregates ticker updates into candles."""

    strategy_type: ClassVar[str] = "CandleSubscriptionStrategy"
    is_kline_strategy: ClassVar[bool] = True
    data_feed_mode: ClassVar[str] = "kline"
    use_base_exit: ClassVar[bool] = True
    _PHASE_SUBSCRIPTION: ClassVar[str] = "subscription"
    _PHASE_AGGREGATION: ClassVar[str] = "aggregation"
    _PHASE_DISPATCH: ClassVar[str] = "dispatch"
    _PHASE_SIGNALS: ClassVar[str] = "signal_generation"
    symbol: str = ""
    interval: str = "1m"
    history_limit: int = 200
    history_chunk_max_bars: int = 100_000
    history_chunk_max_span: timedelta = field(
        default=timedelta(days=30), repr=False
    )
    history_retry_attempts: int = 3
    history_retry_delay: float = 2.0
    history_retry_backoff: float = 2.0
    ticker_channel: str = "market.ticker"
    redis_channel_prefix: str | None = None
    data_layer_channel: str = "market.bar"
    cooldown_seconds: float = 15.0
    signal_frequency_seconds: float = 120.0
    max_loss_streak: int = 3
    health_check_interval: float = 30.0

    _pubsub: PubSubChannel | None = field(default=None, init=False, repr=False)
    _dispatch_event: _Dispatcher | None = field(default=None, init=False, repr=False)
    _history_loader: _HistoryLoader | None = field(default=None, init=False, repr=False)
    _listener_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False
    )
    _candles: Deque[Mapping[str, Any]] = field(default_factory=deque, init=False, repr=False)
    _current_candle: MutableMapping[str, Any] | None = field(
        default=None, init=False, repr=False
    )
    _interval_delta: timedelta = field(default=timedelta(minutes=1), init=False, repr=False)
    _candles_lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _pending_orders: Deque[Mapping[str, Any]] = field(
        default_factory=deque, init=False, repr=False
    )
    _pending_orders_lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _connection_manager: Any | None = field(default=None, init=False, repr=False)
    _data_layer_manager: DataSourceManagerProtocol | None = field(
        default=None, init=False, repr=False
    )
    _data_layer_subscription: DataSubscriptionToken | None = field(
        default=None, init=False, repr=False
    )
    _event_bus_token: BusSubscriptionToken | None = field(
        default=None, init=False, repr=False
    )
    _last_event_ts: int | None = field(default=None, init=False, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _subscription_retry_stop: Event = field(
        default_factory=Event, init=False, repr=False
    )
    _subscription_retry_thread: threading.Thread | None = field(
        default=None, init=False, repr=False
    )
    _use_unified_data: bool = field(default=False, init=False, repr=False)
    required_market_data_streams: tuple[str, ...] = field(
        default=("bar",), init=False, repr=False
    )
    _history_backfill_failed: bool = field(default=False, init=False, repr=False)
    _history_backfill_completed: bool = field(default=False, init=False, repr=False)
    runtime_telemetry: DomRuntimeTelemetryService | None = field(
        default=None, init=False, repr=False
    )
    _last_runtime_status: str | None = field(default=None, init=False, repr=False)
    _last_signal_wait_state: tuple[Any, ...] | None = field(default=None, init=False, repr=False)
    _last_subscription_wait_state: tuple[str | None, str | None] | None = field(
        default=None, init=False, repr=False
    )
    _tick_count: int = field(default=0, init=False, repr=False)
    _closed_candle_count: int = field(default=0, init=False, repr=False)
    _last_tick_timestamp: datetime | None = field(default=None, init=False, repr=False)
    _current_candle_volume: float = field(default=0.0, init=False, repr=False)
    _coro_event_loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _coro_event_loop_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _last_closed_timestamp: datetime | None = field(
        default=None, init=False, repr=False
    )
    _last_closed_volume: float = field(default=0.0, init=False, repr=False)
    _last_closed_price: float = field(default=0.0, init=False, repr=False)
    _last_processed_candle_start: datetime | None = field(
        default=None, init=False, repr=False
    )
    _last_processed_candle_end: datetime | None = field(
        default=None, init=False, repr=False
    )
    _unified_event_channel: str | None = field(default=None, init=False, repr=False)
    _minute_bar_aggregator: MinuteBarAggregator | None = field(
        default=None, init=False, repr=False
    )
    _minute_bar_source_seconds: int | None = field(
        default=None, init=False, repr=False
    )
    _last_order_block: dict[str, Any] | None = field(default=None, init=False, repr=False)
    breaker_tripped: bool = field(default=False, init=False)
    loss_streak: int = field(default=0, init=False)
    _cooldown_until: float = field(default=0.0, init=False, repr=False)
    _last_signal_monotonic: float = field(default=0.0, init=False, repr=False)
    _last_signal_wall: float | None = field(default=None, init=False, repr=False)
    _market_data_subscription_health: Any | None = field(
        default=None, init=False, repr=False
    )
    _bar_stream_missing: bool = field(default=False, init=False, repr=False)
    _bar_stream_refresh_inflight: bool = field(default=False, init=False, repr=False)
    _health_check_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False
    )
    _health_check_interval: float = field(default=30.0, init=False, repr=False)
    _health_check_enabled: bool = field(default=True, init=False, repr=False)
    _subscription_retry_attempts: int = field(default=0, init=False, repr=False)
    _last_retry_started_at: datetime | None = field(default=None, init=False, repr=False)
    _last_retry_completed_at: datetime | None = field(default=None, init=False, repr=False)
    _subscription_connected_at: datetime | None = field(default=None, init=False, repr=False)
    _subscription_heartbeat_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False
    )
    _subscription_heartbeat_interval: float = field(
        default=15.0, init=False, repr=False
    )
    _history_replay_in_progress: bool = field(default=False, init=False, repr=False)
    _last_bar_received_at: datetime | None = field(default=None, init=False, repr=False)
    _recovering_after_inactivity: bool = field(default=False, init=False, repr=False)
    _inactivity_recovery_inflight: bool = field(default=False, init=False, repr=False)
    _inactivity_recovery_next_attempt: float = field(default=0.0, init=False, repr=False)
    _inactivity_recovery_backoff: float = field(default=30.0, init=False, repr=False)

    COMMON_PARAMETER_DEFAULTS: ClassVar[Mapping[str, float | int]] = {
        "cooldown_seconds": 15.0,
        "max_loss_streak": 3,
        "signal_frequency_seconds": 120.0,
        "health_check_interval": 30.0,
    }

    COMMON_PARAMETER_DEFINITIONS: ClassVar[Mapping[str, Mapping[str, Any]]] = {
        "cooldown_seconds": {
            "type": "float",
            "default": 15.0,
            "min": 0.0,
            "max": 900.0,
            "label": "Signal Cooldown (s)",
            "step": 15.0,
            "description": "Seconds to wait before accepting another order.",
        },
        "max_loss_streak": {
            "type": "int",
            "default": 3,
            "min": 1,
            "max": 10,
            "label": "Breaker (Max Loss Streak)",
            "step": 1,
            "description": "Consecutive losses allowed before tripping the breaker.",
        },
        "signal_frequency_seconds": {
            "type": "float",
            "default": 120.0,
            "min": 0.0,
            "max": 1800.0,
            "label": "Execution Frequency (s)",
            "step": 60.0,
            "description": "Minimum wall-clock spacing between queued orders.",
        },
        "health_check_interval": {
            "type": "float",
            "default": 30.0,
            "min": 10.0,
            "max": 3600.0,
            "label": "Health Check Interval (s)",
            "step": 10.0,
            "description": "Health check interval in seconds between bar stream checks.",
        },
    }

    parameter_definitions: ClassVar[Mapping[str, Mapping[str, Any]]] = {
        "symbol": {
            "type": "str",
            "allow_null": True,
            "default": "",
            "description": "Symbol to subscribe for candle updates.",
        },
        "interval": {
            "type": "str",
            "default": "1m",
            "description": "Bar interval for aggregated candle data.",
        },
        "history_limit": {
            "type": "int",
            "default": 200,
            "min": 1,
            "description": "Number of historical candles to retain.",
        },
        **COMMON_PARAMETER_DEFINITIONS,
    }

    def __post_init__(self) -> None:  # noqa: D401 - defer to parent docstring
        super().__post_init__()
        if self.symbol:
            self.symbol = self.symbol.upper()
        interval_hint = (
            self.interval
            or getattr(self, "timeframe", None)
            or getattr(self, "bar_timeframe", None)
        )
        normalised_interval = _normalize_interval_token(interval_hint) or "1m"
        self.interval = normalised_interval
        try:
            self._interval_delta = _interval_to_delta(self.interval)
        except ValueError:
            self._interval_delta = _interval_to_delta("1m")
            self.interval = "1m"
        self._candles = deque(maxlen=max(1, int(self.history_limit)))
        self._use_unified_data = is_unified_mode(get_data_layer_mode())
        self._sync_required_market_streams()
        defaults = self.COMMON_PARAMETER_DEFAULTS
        try:
            cooldown_value = float(self.cooldown_seconds)
        except (TypeError, ValueError):
            cooldown_value = float(defaults.get("cooldown_seconds", 0.0))
        self.cooldown_seconds = max(0.0, cooldown_value)
        try:
            frequency_value = float(self.signal_frequency_seconds)
        except (TypeError, ValueError):
            frequency_value = float(defaults.get("signal_frequency_seconds", 0.0))
        self.signal_frequency_seconds = max(0.0, frequency_value)
        try:
            streak_value = int(self.max_loss_streak)
        except (TypeError, ValueError):
            streak_value = int(defaults.get("max_loss_streak", 1))
        self.max_loss_streak = max(1, streak_value)
        self._base_exit_dispatched = False
        interval_seconds = max(10.0, self._interval_delta.total_seconds())
        normalized_hc = min(3600.0, interval_seconds)
        self._health_check_interval = normalized_hc
        self.health_check_interval = normalized_hc
        self.loss_streak = 0
        self.breaker_tripped = False
        self._cooldown_until = 0.0
        self._last_signal_monotonic = 0.0
        self._last_signal_wall = None
        existing = self.get_parameter_definitions()
        base_keys = set(self._candle_parameter_definitions().keys())
        retained = {
            key: metadata
            for key, metadata in existing.items()
            if key not in base_keys
        }
        self.set_parameter_definitions(retained)

    # ------------------------------------------------------------------
    def _normalise_parameter_value(self, name: str, value: Any) -> Any:
        if name == "symbol":
            if value is None:
                return ""
            if isinstance(value, str):
                return value.strip().upper()
            return str(value).upper()
        if name == "interval":
            try:
                token = _normalize_interval_token(str(value))
            except Exception:
                token = None
            return token or self.interval or "1m"
        if name == "cooldown_seconds":
            return max(0.0, float(value))
        if name == "signal_frequency_seconds":
            return max(0.0, float(value))
        if name == "max_loss_streak":
            return max(1, int(value))
        if name == "health_check_interval":
            return min(3600.0, max(10.0, float(value)))
        return super()._normalise_parameter_value(name, value)

    # ------------------------------------------------------------------
    def apply_parameter_updates(self, updates: Mapping[str, Any]) -> Dict[str, Any]:
        if not updates:
            return {}
        previous_symbol = self.symbol
        previous_interval = self.interval
        applied = super().apply_parameter_updates(updates)
        interval_changed = "interval" in applied and self.interval != previous_interval
        history_changed = "history_limit" in applied
        if interval_changed:
            normalised = _normalize_interval_token(self.interval) or "1m"
            self.interval = normalised
            try:
                self._interval_delta = _interval_to_delta(self.interval)
            except ValueError:
                self._interval_delta = _interval_to_delta("1m")
                self.interval = "1m"
            self._reset_unified_bucket()
            self._last_processed_candle_start = None
            self._last_processed_candle_end = None
        if history_changed:
            try:
                limit = max(1, int(self.history_limit))
            except Exception:
                limit = 1
            with self._candles_lock:
                self._candles = deque(list(self._candles)[-limit:], maxlen=limit)
        symbol_changed = "symbol" in applied and self.symbol != previous_symbol
        if (symbol_changed or interval_changed) and self.active and self._use_unified_data:
            try:
                self._teardown_unified_subscription()
                self._ensure_unified_subscription()
            except Exception as exc:  # pragma: no cover - defensive logging
                self.logger.error("Failed to resubscribe after parameter change: %s", exc)
        return applied

    # ------------------------------------------------------------------
    def set_dependencies(
        self,
        *,
        pubsub: PubSubChannel | None = None,
        event_dispatcher: Callable[[str, Mapping[str, Any]], Awaitable[None]] | None = None,
        candle_history_loader: _HistoryLoader | None = None,
        data_layer_manager: DataSourceManagerProtocol | None = None,
        connection_manager: Any | None = None,
        runtime_telemetry: DomRuntimeTelemetryService | None = None,
        **dependencies: Any,
    ) -> None:
        """Inject pubsub, dispatcher and optional history loader."""

        super().set_dependencies(**dependencies)
        if pubsub is not None:
            self._pubsub = pubsub
        if event_dispatcher is not None:
            async def _dispatch(payload: Mapping[str, Any]) -> None:
                result = event_dispatcher(self.name, payload)
                if asyncio.iscoroutine(result):
                    await result
            self._dispatch_event = _dispatch
        if candle_history_loader is not None:
            self._history_loader = candle_history_loader
        if data_layer_manager is not None:
            self._data_layer_manager = data_layer_manager
        if connection_manager is not None:
            self._connection_manager = connection_manager
        if runtime_telemetry is not None:
            self.runtime_telemetry = runtime_telemetry
        facade = dependencies.get("market_data_subscription_health")
        if facade is None:
            facade = getattr(self, "market_data_subscription_health", None)
        if facade is not None:
            self._market_data_subscription_health = facade

    # ------------------------------------------------------------------
    def set_parameter_definitions(
        self, definitions: Mapping[str, Mapping[str, Any]]
    ) -> None:  # type: ignore[override]
        merged: dict[str, dict[str, Any]] = {}
        for key, meta in self._candle_parameter_definitions().items():
            merged[key] = dict(meta)
        for name, metadata in (definitions or {}).items():
            if not isinstance(metadata, Mapping):
                continue
            merged[name] = dict(metadata)
        super().set_parameter_definitions(merged)

    # ------------------------------------------------------------------
    def _candle_parameter_definitions(self) -> Mapping[str, Mapping[str, Any]]:
        base: dict[str, dict[str, Any]] = {
            "symbol": {
                "type": "str",
                "allow_null": True,
                "default": (self.symbol or "").upper(),
                "description": "Symbol to subscribe for candle updates.",
            },
            "interval": {
                "type": "str",
                "default": self.interval,
                "description": "Bar interval for aggregated candle data.",
            },
            "history_limit": {
                "type": "int",
                "default": int(self.history_limit),
                "min": 1,
                "description": "Number of historical candles to retain.",
            },
        }
        for name, metadata in self.COMMON_PARAMETER_DEFINITIONS.items():
            entry = dict(metadata)
            current_value: Any
            if hasattr(self, name):
                current_value = getattr(self, name)
            else:
                current_value = self.COMMON_PARAMETER_DEFAULTS.get(name)
            if current_value is None:
                current_value = self.COMMON_PARAMETER_DEFAULTS.get(name)
            if name == "max_loss_streak":
                try:
                    entry["default"] = int(current_value)
                except (TypeError, ValueError):
                    entry["default"] = int(
                        self.COMMON_PARAMETER_DEFAULTS.get(name, 1)
                    )
            else:
                try:
                    entry["default"] = float(current_value)
                except (TypeError, ValueError):
                    entry["default"] = float(
                        self.COMMON_PARAMETER_DEFAULTS.get(name, 0.0)
                    )
            base[name] = entry
        return base

    # ------------------------------------------------------------------
    def _monotonic_now(self) -> float:
        return time.monotonic()

    def _subscription_wait_state_changed(
        self, code: str | None, reason: str | None
    ) -> bool:
        state = (code, reason)
        if self._last_subscription_wait_state == state:
            return False
        self._last_subscription_wait_state = state
        return True

    # ------------------------------------------------------------------
    def _wall_clock_now(self) -> float:
        return datetime.now(timezone.utc).timestamp()

    # ------------------------------------------------------------------
    def _reset_order_guards(self) -> None:
        self._cooldown_until = 0.0
        self._last_signal_monotonic = 0.0
        self._last_signal_wall = None
        self.loss_streak = 0
        self.breaker_tripped = False

    # ------------------------------------------------------------------
    def start(self) -> bool:
        if not self.symbol:
            self.logger.warning("Candle subscription requires a symbol")
            return False
        started = super().start()
        if not started:
            return False
        with self._candles_lock:
            self._candles.clear()
        with self._pending_orders_lock:
            self._pending_orders.clear()
        self._reset_order_guards()
        self._current_candle = None
        self._last_event_ts = None
        self._reset_runtime_counters()
        self._history_backfill_failed = False
        self._history_backfill_completed = False
        self._last_signal_wait_state = None
        self._reset_unified_bucket()
        self._subscription_retry_attempts = 0
        self._last_retry_started_at = None
        self._last_retry_completed_at = None
        self._subscription_connected_at = None
        self._stop_subscription_heartbeat()
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._telemetry_start_session()
        self._telemetry_set_phase_status(
            self._PHASE_AGGREGATION,
            status="idle",
            status_code="awaiting_data",
        )
        self._telemetry_set_phase_status(
            self._PHASE_DISPATCH,
            status="idle",
            status_code="awaiting_data",
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_AGGREGATION,
            tick_count=0,
            last_tick_at=None,
            last_volume=0.0,
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_DISPATCH,
            candle_count=0,
            last_candle_end=None,
            last_close=None,
            last_volume=0.0,
        )
        self._telemetry_set_phase_status(
            self._PHASE_SIGNALS,
            status="waiting",
            status_code="awaiting_data",
            status_reason="Awaiting signal prerequisites",
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_SIGNALS,
            signals_generated=0,
            last_signal_side=None,
        )
        mode = get_data_layer_mode()
        self._use_unified_data = is_unified_mode(mode)
        self._sync_required_market_streams()
        self._telemetry_update_phase_metrics(
            self._PHASE_SUBSCRIPTION,
            mode="unified" if self._use_unified_data else "legacy",
            interval=self.interval,
            symbol=self.symbol or None,
        )
        if self._use_unified_data:
            manager = self._data_layer_manager
            if manager is None:
                try:
                    manager = get_data_source_manager()
                except RuntimeError:
                    manager = None
            if manager is None:
                self.logger.warning(
                    "Unified candle subscription requested but data source manager is unavailable; falling back to legacy pipeline"
                )
                self._telemetry_log(
                    "Unified candle subscription falling back to legacy pipeline",
                    level="WARN",
                    tone="warning",
                )
                self._use_unified_data = False
                self._sync_required_market_streams()
                self._telemetry_update_phase_metrics(
                    self._PHASE_SUBSCRIPTION,
                    mode="legacy",
                    interval=self.interval,
                    symbol=self.symbol or None,
                )
            else:
                self._data_layer_manager = manager
        self._telemetry_set_phase_status(
            self._PHASE_SUBSCRIPTION,
            status="initialising",
            status_code="starting",
        )
        if self._use_unified_data:
            self._telemetry_set_phase_status(
                self._PHASE_SUBSCRIPTION,
                status="waiting",
                status_code="awaiting_market_data_ready",
                status_reason="Waiting for market data service readiness",
                status_cause="Awaiting market data service readiness",
                status_cause_code="awaiting_market_data_ready",
            )
            self._schedule_coroutine(self._await_market_data_ready_and_subscribe())
            return True

        if self._dispatch_event is None or self._pubsub is None:
            self.logger.warning(
                "CandleSubscriptionStrategy missing pubsub/event dispatcher dependencies"
            )
            self._telemetry_log(
                "Legacy candle subscription missing dependencies",
                level="ERROR",
                tone="negative",
            )
            self._telemetry_set_phase_status(
                self._PHASE_SUBSCRIPTION,
                status="failed",
                status_code="error",
                status_reason="Legacy candle subscription missing dependencies",
                status_cause="Legacy candle subscription missing dependencies",
                status_cause_code="subscription_missing_dependencies",
            )
            super().stop()
            self._telemetry_stop_session(
                "Legacy candle subscription missing dependencies"
            )
            return False
        loop = self._loop
        assert loop is not None
        self._listener_task = loop.create_task(
            self._run_listener(), name=f"{self.name}-candle-listener"
        )
        self._telemetry_set_phase_status(
            self._PHASE_SUBSCRIPTION,
            status="connected",
            status_code="listening",
        )
        self._telemetry_log(
            "Legacy candle listener started",
            level="INFO",
            tone="neutral",
        )
        if self._health_check_enabled:
            self._schedule_coroutine(self._schedule_periodic_health_check())
        return True

    # ------------------------------------------------------------------
    def stop(self) -> bool:
        stopped = super().stop()
        if stopped:
            self._cancel_periodic_health_check()
            self._stop_subscription_heartbeat()
            if self._use_unified_data:
                self._cancel_retry_thread()
                self._teardown_unified_subscription()
            else:
                self._cancel_listener()
            self._telemetry_set_phase_status(
                self._PHASE_SUBSCRIPTION,
                status="stopped",
                status_code="stopped",
            )
            self._telemetry_set_phase_status(
                self._PHASE_AGGREGATION,
                status="stopped",
                status_code="inactive",
            )
            self._telemetry_set_phase_status(
                self._PHASE_DISPATCH,
                status="stopped",
                status_code="inactive",
            )
            self._telemetry_log(
                "Candle subscription stopped",
                level="INFO",
                tone="neutral",
            )
            self._telemetry_stop_session("Candle subscription stopped")
        return stopped

    # ------------------------------------------------------------------
    def _cancel_listener(self) -> None:
        task = self._listener_task
        if task is None:
            return
        task.cancel()
        self._listener_task = None

    # ------------------------------------------------------------------
    async def _run_listener(self) -> None:
        assert self._pubsub is not None
        dispatcher = self._dispatch_event
        if dispatcher is None:
            return
        if self._history_loader is not None:
            try:
                self._history_replay_in_progress = True
                history = await self._history_loader(
                    self.symbol, self.interval, self.history_limit
                )
                for candle in history[-self.history_limit :]:
                    normalised = self._normalise_candle(candle, is_closed=True)
                    if normalised is not None:
                        with self._candles_lock:
                            self._candles.append(normalised)
                        self._invoke_candle_handlers(normalised)
            except Exception:  # pragma: no cover - defensive load failure
                self.logger.exception("Failed to load candle history for %s", self.symbol)
            finally:
                self._history_replay_in_progress = False
        channel = self._resolve_channel(self.ticker_channel)
        backoff = 1.0
        try:
            while self.active:
                try:
                    stream = await self._pubsub.listen(channel)
                    self._telemetry_set_phase_status(
                        self._PHASE_SUBSCRIPTION,
                        status="listening",
                        status_code="active",
                    )
                    self._telemetry_log(
                        "Legacy candle subscription listening",
                        level="INFO",
                        tone="neutral",
                    )
                    async for message in stream:
                        if not self.active:
                            break
                        if not isinstance(message, Mapping):
                            continue
                        if not self._accept_tick(message):
                            continue
                        event = await self._process_tick(message)
                        if event is None:
                            continue
                        try:
                            await dispatcher(event)
                            self._telemetry_log(
                                "Candle event dispatched",
                                level="INFO",
                                tone="positive",
                                phase=self._PHASE_DISPATCH,
                                deduplicate=False,
                                details={
                                    "end": event.get("end"),
                                    "close": event.get("close"),
                                    "volume": event.get("volume"),
                                    "is_closed": bool(event.get("is_closed")),
                                },
                            )
                        except Exception:  # pragma: no cover - downstream failures
                            self.logger.exception("Candle event dispatch failed")
                            self._telemetry_log(
                                "Candle event dispatch failed",
                                level="ERROR",
                                tone="negative",
                                phase=self._PHASE_DISPATCH,
                                deduplicate=False,
                            )
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover - runtime listener failure
                    self.logger.exception("Candle listener crashed")
                    self._telemetry_log(
                        "Candle listener crashed",
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
                "Candle listener stopped",
                level="INFO",
                tone="neutral",
                phase=self._PHASE_SUBSCRIPTION,
                deduplicate=False,
            )

    # ------------------------------------------------------------------
    def _resolve_channel(self, name: str) -> str:
        prefix = (self.redis_channel_prefix or "").strip()
        if not prefix:
            return name
        return f"{prefix}{name}"

    # ------------------------------------------------------------------
    def _sync_required_market_streams(self) -> None:
        desired = ("bar",) if self._use_unified_data else ("ticker",)
        if getattr(self, "required_market_data_streams", None) == desired:
            return
        self.required_market_data_streams = desired

    # ------------------------------------------------------------------
    def _resolve_bar_channel(self) -> str:
        base_channel = (getattr(self, "bar_channel", None) or "").strip()
        if base_channel:
            channel = self._resolve_channel(base_channel).rstrip(".")
        else:
            base_channel = (self.data_layer_channel or "").strip() or "market.bar"
            channel = self._resolve_channel(base_channel).rstrip(".")
        if any(token in channel for token in ("*", "?")):
            return channel
        identifier = (self.symbol or "").strip().upper()
        if identifier:
            if channel.upper().endswith(f"-{identifier}"):
                return channel
            if channel.endswith("-"):
                return f"{channel}{identifier}"
            return f"{channel}-{identifier}"
        if channel.endswith("-"):
            return f"{channel}*"
        return f"{channel}-*"

    # ------------------------------------------------------------------
    def _resolve_unified_event_channel(self) -> str:
        base_channel = (getattr(self, "bar_channel", None) or "").strip()
        if not base_channel:
            base_channel = (self.data_layer_channel or "").strip() or "market.bar"
        bare_base = base_channel.rstrip(".")
        symbol = (self.symbol or "").strip().upper()
        if symbol:
            return f"{bare_base}-{symbol}"
        return f"{bare_base}-*"

    # ------------------------------------------------------------------
    def _accept_tick(self, tick: Mapping[str, Any]) -> bool:
        symbol = str(tick.get("symbol", "")).strip().upper()
        if symbol and symbol != (self.symbol or "").upper():
            return False
        return True

    # ------------------------------------------------------------------
    async def _process_tick(self, tick: Mapping[str, Any]) -> Mapping[str, Any] | None:
        price = tick.get("last")
        if price is None:
            price = tick.get("close")
        if price is None:
            price = tick.get("mid_price")
        if price is None:
            return None
        try:
            price_value = float(price)
        except (TypeError, ValueError):
            return None
        size = _coerce_float(tick.get("last_size"), default=0.0)
        timestamp = _parse_timestamp(tick.get("timestamp"))
        bucket = _floor_timestamp(timestamp, self._interval_delta)
        if (
            self._current_candle is not None
            and bucket > self._current_candle["start"]
        ):
            closed = self._finalise_current()
            with self._candles_lock:
                self._candles.append(closed)
            await self._dispatch_closed(closed)
            self._current_candle = None
        candle = self._current_candle
        if candle is None:
            candle = self._create_new_candle(bucket, price_value)
            self._current_candle = candle
        else:
            candle["close"] = price_value
            candle["high"] = max(candle["high"], price_value)
            candle["low"] = min(candle["low"], price_value)
        candle["volume"] += size
        candle["last_timestamp"] = timestamp
        self._telemetry_record_data_event(timestamp, candle.get("volume", 0.0))
        return self._build_event(candle, is_closed=False)

    # ------------------------------------------------------------------
    def _create_new_candle(self, start: datetime, price: float) -> MutableMapping[str, Any]:
        return {
            "start": start,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 0.0,
            "interval": self.interval,
            "symbol": self.symbol,
            "last_timestamp": start,
        }

    # ------------------------------------------------------------------
    def _finalise_current(self) -> Mapping[str, Any]:
        assert self._current_candle is not None
        candle = dict(self._current_candle)
        candle["is_closed"] = True
        candle["end"] = candle["start"] + self._interval_delta
        candle["open_time"] = candle["start"].isoformat()
        candle["close_time"] = candle["end"].isoformat()
        candle.pop("last_timestamp", None)
        return candle

    # ------------------------------------------------------------------
    def _reset_unified_bucket(self) -> None:
        if self._minute_bar_aggregator is not None:
            self._minute_bar_aggregator.reset()
        self._minute_bar_aggregator = None
        self._minute_bar_source_seconds = None

    # ------------------------------------------------------------------
    def _resolve_unified_aggregator(
        self,
        *,
        source_interval_seconds: int | None = None,
    ) -> MinuteBarAggregator:
        aggregator = self._minute_bar_aggregator
        target_interval = self._interval_delta
        source_seconds = source_interval_seconds or 60
        if (
            aggregator is None
            or aggregator.interval != target_interval
            or self._minute_bar_source_seconds != source_seconds
        ):
            aggregator = MinuteBarAggregator(
                target_interval,
                symbol=self.symbol or None,
                interval_label=self.interval,
                source_interval_seconds=source_seconds,
            )
            self._minute_bar_aggregator = aggregator
            self._minute_bar_source_seconds = source_seconds
        else:
            aggregator.symbol = self.symbol or None
            aggregator.interval_label = self.interval
        return aggregator

    # ------------------------------------------------------------------
    def _flush_unified_bucket(
        self, *, close_partial: bool = False
    ) -> list[Mapping[str, Any]]:
        aggregator = self._minute_bar_aggregator
        if aggregator is None:
            return []
        return aggregator.flush(close_partial=close_partial)

    # ------------------------------------------------------------------
    def _build_unified_closed_bar(
        self,
        normalized: Mapping[str, Any],
        *,
        sample_count: int,
        expected_samples: int,
        is_partial: bool,
    ) -> Mapping[str, Any]:
        start = _floor_timestamp(normalized["timestamp"], self._interval_delta)
        end = start + self._interval_delta
        payload: dict[str, Any] = {
            "symbol": self.symbol,
            "interval": self.interval,
            "start": start,
            "end": end,
            "open": normalized["open"],
            "high": normalized["high"],
            "low": normalized["low"],
            "close": normalized["close"],
            "volume": normalized["volume"],
            "is_closed": True,
            "sample_count": sample_count,
            "expected_samples": expected_samples,
            "is_partial": is_partial,
        }
        return payload

    # ------------------------------------------------------------------
    def _extract_candle_end(self, candle: Mapping[str, Any]) -> datetime | None:
        end = self._maybe_parse_timestamp(
            candle.get("end") or candle.get("close_time")
        )
        if end is not None:
            aligned = _floor_timestamp(end, self._interval_delta)
            if end == aligned:
                return end
            return aligned + self._interval_delta
        end = self._maybe_parse_timestamp(candle.get("timestamp"))
        if end is not None:
            aligned = _floor_timestamp(end, self._interval_delta)
            if end == aligned:
                return end
            return aligned + self._interval_delta
        start = self._maybe_parse_timestamp(
            candle.get("start") or candle.get("open_time")
        )
        if start is None:
            return None
        return start + self._interval_delta

    # ------------------------------------------------------------------
    def _is_interval_boundary(
        self, timestamp: datetime | None, interval_delta: timedelta | None = None
    ) -> bool:
        if timestamp is None:
            return False
        aligned = _floor_timestamp(timestamp, interval_delta or self._interval_delta)
        return aligned == timestamp

    # ------------------------------------------------------------------
    def _ingest_bar_payload(
        self, payload: Mapping[str, Any], *, ts_ns: int | None = None
    ) -> Mapping[str, Any] | None:
        record = dict(payload)
        if record.get("timestamp") is None and ts_ns is not None:
            record["timestamp"] = ts_ns / 1_000_000_000
        interval_hint = _normalize_interval_token(record.get("interval"))
        if interval_hint is None:
            interval_hint = _normalize_interval_token(record.get("timeframe"))
        bar_size_hint = _normalize_interval_token(record.get("bar_size"))
        source_interval_hint = interval_hint or bar_size_hint
        source_seconds = _interval_seconds(source_interval_hint)
        close_hint = record.get("is_closed")
        if close_hint is None:
            close_hint = not bool(record.get("is_snapshot"))
        close_bool = bool(close_hint)

        target_interval = self._interval_delta
        target_seconds = int(target_interval.total_seconds())
        if (
            source_seconds is not None
            and source_seconds > 0
            and target_seconds % source_seconds != 0
        ):
            return None
        aggregator = self._resolve_unified_aggregator(
            source_interval_seconds=source_seconds
        )
        target_token = aggregator.interval_token

        if interval_hint is not None and interval_hint == target_token and close_bool:
            normalized = MinuteBarAggregator._normalise_record(record)
            if normalized is None:
                return None
            timestamp = normalized["timestamp"]
            if _floor_timestamp(timestamp, target_interval) != timestamp:
                return None
            expected = aggregator.expected_samples
            closed = self._build_unified_closed_bar(
                normalized,
                sample_count=expected,
                expected_samples=expected,
                is_partial=False,
            )
            self._reset_unified_bucket()
            return closed

        if interval_hint is None and bar_size_hint is None and not (
            record.get("end")
            or record.get("close_time")
            or record.get("start")
            or record.get("open_time")
        ):
            return None

        closed_buckets = aggregator.push(record, close_hint=close_bool)
        if not closed_buckets:
            return None
        return closed_buckets[0]

    # ------------------------------------------------------------------
    async def _dispatch_closed(self, candle: Mapping[str, Any]) -> None:
        dispatcher = self._dispatch_event
        if dispatcher is None:
            return
        payload = dict(candle)
        payload.update(
            {
                "type": "candle",
                "symbol": self.symbol,
                "interval": self.interval,
            }
        )
        self._telemetry_record_closed_candle(
            payload,
            timestamp=candle.get("end") if isinstance(candle, Mapping) else None,
        )
        try:
            await dispatcher(payload)
            self._telemetry_log(
                "Closed candle dispatched",
                level="INFO",
                tone="positive",
                phase=self._PHASE_DISPATCH,
                deduplicate=False,
                details={
                    "end": payload.get("end"),
                    "close": payload.get("close"),
                    "volume": payload.get("volume"),
                },
            )
        except Exception:  # pragma: no cover - downstream failures
            self.logger.exception("Failed to dispatch closed candle for %s", self.symbol)
            self._telemetry_log(
                "Failed to dispatch closed candle",
                level="ERROR",
                tone="negative",
                phase=self._PHASE_DISPATCH,
                deduplicate=False,
            )

    # ------------------------------------------------------------------
    def _build_event(
        self, candle: Mapping[str, Any], *, is_closed: bool
    ) -> Mapping[str, Any]:
        end_time = candle["start"] + self._interval_delta
        event = {
            "type": "candle",
            "symbol": self.symbol,
            "interval": self.interval,
            "start": candle["start"].isoformat(),
            "end": end_time.isoformat(),
            "open": candle["open"],
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
            "volume": candle.get("volume", 0.0),
            "is_closed": is_closed,
            "last_timestamp": candle.get("last_timestamp").isoformat()
            if candle.get("last_timestamp")
            else None,
        }
        return event

    # ------------------------------------------------------------------
    def _record_order_block(
        self,
        code: str,
        *,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"code": code}
        if message:
            payload["message"] = message
        if details:
            payload["details"] = dict(details)
        self._last_order_block = payload

    # ------------------------------------------------------------------
    def pop_last_order_block(self) -> Mapping[str, Any] | None:
        block = self._last_order_block
        self._last_order_block = None
        return block

    # ------------------------------------------------------------------
    def _normalise_candle(
        self, candle: Mapping[str, Any], *, is_closed: bool
    ) -> Mapping[str, Any] | None:
        end_ts = self._maybe_parse_timestamp(
            candle.get("end") or candle.get("close_time") or candle.get("timestamp")
        )
        start = self._maybe_parse_timestamp(
            candle.get("start") or candle.get("open_time")
        )
        close = candle.get("close")
        open_ = candle.get("open")
        high = candle.get("high")
        low = candle.get("low")
        if start is None and end_ts is not None:
            start = end_ts - self._interval_delta
        if start is None:
            return None
        start = _floor_timestamp(start, self._interval_delta)
        if close is None or open_ is None or high is None or low is None:
            return None
        data = {
            "type": "candle",
            "symbol": self.symbol,
            "interval": self.interval,
            "start": start.isoformat(),
            "end": (start + self._interval_delta).isoformat(),
            "open": _coerce_float(open_),
            "high": _coerce_float(high),
            "low": _coerce_float(low),
            "close": _coerce_float(close),
            "volume": _coerce_float(candle.get("volume"), default=0.0),
            "is_closed": is_closed,
        }
        return data

    # ------------------------------------------------------------------
    def get_candles(self) -> list[Mapping[str, Any]]:
        with self._candles_lock:
            return [dict(item) for item in self._candles]

    # ------------------------------------------------------------------
    def _normalise_order_payload(
        self, order: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        if not isinstance(order, Mapping):
            self.logger.warning(
                "Ignoring non-mapping order payload for strategy %s", self.name
            )
            self._record_order_block(
                "invalid_payload",
                message="Order payload must be a mapping",
            )
            return None

        payload: dict[str, Any] = dict(order)
        metadata: dict[str, Any]
        raw_metadata = payload.get("metadata")
        if isinstance(raw_metadata, Mapping):
            metadata = dict(raw_metadata)
        else:
            legacy_meta = payload.pop("meta", None)
            if isinstance(legacy_meta, Mapping):
                metadata = dict(legacy_meta)
            else:
                metadata = {}
        payload["metadata"] = metadata

        if "side" not in payload and "action" in payload:
            payload["side"] = payload.pop("action")
        side_value = payload.get("side")
        if side_value is None:
            self.logger.warning(
                "Skipping order without side for strategy %s", self.name
            )
            self._record_order_block(
                "missing_side",
                message="Order payload missing side/action",
            )
            return None
        try:
            side = str(side_value).strip().upper()
        except Exception:
            self.logger.warning(
                "Skipping order with invalid side for strategy %s: %r",
                self.name,
                side_value,
            )
            self._record_order_block(
                "invalid_side",
                message="Failed to normalise order side",
                details={"side": side_value},
            )
            return None
        if not side:
            self.logger.warning(
                "Skipping order without side for strategy %s", self.name
            )
            self._record_order_block(
                "missing_side",
                message="Order payload missing side/action",
            )
            return None
        payload["side"] = side

        if "quantity" not in payload and "qty" in payload:
            payload["quantity"] = payload.pop("qty")
        quantity_value = payload.get("quantity")
        if quantity_value is None:
            quantity_value = metadata.get("quantity")
        try:
            quantity = float(quantity_value)
        except (TypeError, ValueError):
            self.logger.warning(
                "Skipping order with invalid quantity for strategy %s: %r",
                self.name,
                quantity_value,
            )
            self._record_order_block(
                "invalid_quantity",
                message="Order quantity is not numeric",
                details={"quantity": quantity_value},
            )
            return None
        if not math.isfinite(quantity):
            self.logger.warning(
                "Skipping order with non-finite quantity for strategy %s: %r",
                self.name,
                quantity_value,
            )
            self._record_order_block(
                "invalid_quantity",
                message="Order quantity is not finite",
                details={"quantity": quantity_value},
            )
            return None
        if quantity <= 0:
            self.logger.warning(
                "Skipping non-positive quantity order for strategy %s: %r",
                self.name,
                quantity_value,
            )
            self._record_order_block(
                "invalid_quantity",
                message="Order quantity must be positive",
                details={"quantity": quantity_value},
            )
            return None
        quantity_int = int(quantity)
        if abs(quantity - quantity_int) < 1e-09:
            payload["quantity"] = quantity_int
        else:
            payload["quantity"] = quantity

        if "order_type" not in payload and "type" in payload:
            payload["order_type"] = payload.pop("type")
        order_type_value = payload.get("order_type")
        if isinstance(order_type_value, str) and order_type_value.strip():
            payload["order_type"] = order_type_value.strip().upper()
        else:
            payload["order_type"] = "MARKET"

        for price_key in ("price", "limit_price", "stop_price"):
            value = payload.get(price_key)
            if value is None:
                continue
            try:
                payload[price_key] = float(value)
            except (TypeError, ValueError):
                self.logger.debug(
                    "Dropping invalid %s value for strategy %s order payload: %r",
                    price_key,
                    self.name,
                    value,
                )
                payload.pop(price_key, None)

        symbol_value = payload.get("symbol")
        if not symbol_value:
            symbol_value = metadata.get("symbol") or self.symbol
        if isinstance(symbol_value, str):
            symbol_text = symbol_value.strip().upper()
        elif symbol_value is None:
            symbol_text = ""
        else:
            symbol_text = str(symbol_value).strip().upper()
        if symbol_text:
            payload["symbol"] = symbol_text
            metadata.setdefault("symbol", symbol_text)
        else:
            payload.pop("symbol", None)

        metadata.setdefault("strategy", self.name)
        metadata.setdefault("interval", self.interval)

        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            payload["reason"] = str(reason)

        for key in (
            "exchange",
            "sec_type",
            "account",
            "client_order_id",
            "command_id",
            "rule_id",
        ):
            value = payload.get(key)
            if value is None:
                meta_value = metadata.get(key)
                if meta_value is not None:
                    payload[key] = meta_value

        # Infer missing instrument details from symbol if known
        if symbol_text:
            # Local defaults aligned with other strategies
            instrument_defaults = {
                "ES": {"exchange": "CME", "sec_type": "FUT"},
                "MES": {"exchange": "CME", "sec_type": "FUT"},
                "NQ": {"exchange": "CME", "sec_type": "FUT"},
                "MNQ": {"exchange": "CME", "sec_type": "FUT"},
                "YM": {"exchange": "CBOT", "sec_type": "FUT"},
                "MYM": {"exchange": "CBOT", "sec_type": "FUT"},
                "RTY": {"exchange": "CME", "sec_type": "FUT"},
                "M2K": {"exchange": "CME", "sec_type": "FUT"},
                "VX": {"exchange": "CFE", "sec_type": "FUT"},
            }
            defaults = instrument_defaults.get(symbol_text)
            if defaults:
                payload.setdefault("exchange", defaults.get("exchange"))
                payload.setdefault("sec_type", defaults.get("sec_type"))
                metadata.setdefault("exchange", defaults.get("exchange"))
                metadata.setdefault("sec_type", defaults.get("sec_type"))

        payload.setdefault("strategy", self.name)

        return payload

    # ------------------------------------------------------------------
    def queue_order(self, order: Mapping[str, Any]) -> bool:
        self._last_order_block = None
        now_monotonic = self._monotonic_now()
        side_raw = order.get("side") or order.get("action")
        try:
            side_token = str(side_raw).strip().upper() if side_raw is not None else ""
        except Exception:
            side_token = ""
        try:
            current_position = float(getattr(self, "_position", 0.0))
        except Exception:
            current_position = 0.0
        is_exit_like = False
        if current_position > 0 and side_token == "SELL":
            is_exit_like = True
        elif current_position < 0 and side_token == "BUY":
            is_exit_like = True

        if self.breaker_tripped and not is_exit_like:
            self.logger.warning(
                "Breaker tripped for strategy %s; blocking order queue", self.name
            )
            self._telemetry_log(
                "Breaker tripped due to loss streak",
                level="WARN",
                tone="warning",
            )
            self._record_order_block(
                "breaker_tripped",
                message="Loss breaker active for strategy",
            )
            return False

        cooldown = max(0.0, float(self.cooldown_seconds))
        if cooldown > 0.0 and now_monotonic < self._cooldown_until and not is_exit_like:
            remaining = max(0.0, self._cooldown_until - now_monotonic)
            self.logger.debug(
                "Order suppressed by cooldown guard for strategy %s (remaining=%.2fs)",
                self.name,
                remaining,
            )
            self._telemetry_log(
                "Order suppressed by cooldown guard",
                tone="warning",
                details={"remaining_seconds": round(remaining, 3)},
            )
            self._record_order_block(
                "cooldown_active",
                message="Cooldown guard suppressing new signal",
                details={"remaining_seconds": round(remaining, 3)},
            )
            return False

        frequency = max(0.0, float(self.signal_frequency_seconds))
        now_wall: float | None = None
        last_wall = self._last_signal_wall
        if frequency > 0.0 and last_wall is not None and not is_exit_like:
            now_wall = self._wall_clock_now()
            elapsed = now_wall - last_wall
            if elapsed < frequency:
                remaining = max(0.0, frequency - elapsed)
                self.logger.debug(
                    "Order suppressed by frequency guard for strategy %s (remaining=%.2fs)",
                    self.name,
                    remaining,
                )
                self._telemetry_log(
                    "Order suppressed by frequency guard",
                    tone="warning",
                    details={"remaining_seconds": round(remaining, 3)},
                )
                self._record_order_block(
                    "frequency_guard",
                    message="Signal frequency guard suppressing new order",
                    details={"remaining_seconds": round(remaining, 3)},
                )
                return False

        normalised = self._normalise_order_payload(order)
        if normalised is None:
            return False

        with self._pending_orders_lock:
            self._pending_orders.append(normalised)
            pending_count = len(self._pending_orders)

        self.logger.debug(
            "Queued order for strategy %s (pending=%d): %s",
            self.name,
            pending_count,
            normalised,
        )
        self._telemetry_set_phase_status(
            self._PHASE_DISPATCH,
            status="dispatching",
            status_code="orders_pending",
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_DISPATCH,
            queued_orders=pending_count,
        )
        if now_wall is None:
            now_wall = self._wall_clock_now()
        self._last_signal_monotonic = now_monotonic
        self._last_signal_wall = now_wall
        self._cooldown_until = max(now_monotonic, self._cooldown_until) + cooldown
        self._last_order_block = None

        side_value = normalised.get("side") or normalised.get("action")
        telemetry = self._telemetry()
        if telemetry is not None and isinstance(side_value, str):
            side_token = side_value.strip().upper()
            if side_token in {"BUY", "SELL"}:
                quantity = normalised.get("quantity") or order.get("quantity")
                signal_timestamp = None
                metadata = normalised.get("metadata")
                if isinstance(metadata, Mapping):
                    raw_ts = (
                        metadata.get("interval_end")
                        or metadata.get("timestamp")
                        or metadata.get("bar_end")
                    )
                    if raw_ts is not None:
                        try:
                            signal_timestamp = _parse_timestamp(raw_ts)
                        except Exception:
                            signal_timestamp = None
                try:
                    telemetry.record_signal(
                        self._telemetry_strategy_id(),
                        side_token,
                        stage=self._PHASE_SIGNALS,
                        quantity=quantity,
                        timestamp=signal_timestamp,
                        notes=normalised.get("reason") or order.get("reason"),
                    )
                except KeyError:
                    pass
        recorder = getattr(self, "_telemetry_record_signal", None)
        if callable(recorder) and isinstance(side_value, str):
            side_token = side_value.strip().upper()
            if side_token in {"BUY", "SELL"}:
                try:
                    recorder(side_token)
                except Exception:  # pragma: no cover - defensive telemetry
                    self.logger.debug("Failed to record telemetry signal", exc_info=True)
        return True

    # ------------------------------------------------------------------
    def record_trade_result(self, pnl: float) -> None:
        if pnl < 0:
            self.loss_streak += 1
            threshold = max(1, int(self.max_loss_streak))
            if self.loss_streak >= threshold:
                if not self.breaker_tripped:
                    self.breaker_tripped = True
                    self._telemetry_log(
                        "Loss breaker tripped",
                        level="WARN",
                        tone="warning",
                        details={
                            "loss_streak": self.loss_streak,
                            "threshold": threshold,
                        },
                    )
            else:
                self.breaker_tripped = False
                self._telemetry_log(
                    "Loss streak increased",
                    tone="warning",
                    details={
                        "loss_streak": self.loss_streak,
                        "threshold": threshold,
                    },
                    deduplicate=False,
                )
        else:
            breaker_was_tripped = self.breaker_tripped
            streak_was_active = self.loss_streak > 0
            self.loss_streak = 0
            self.breaker_tripped = False
            if breaker_was_tripped or streak_was_active:
                self._telemetry_log(
                    "Loss breaker reset",
                    tone="neutral",
                    details={"loss_streak": 0},
                )

    # ------------------------------------------------------------------
    async def generate_orders(self) -> Sequence[Mapping[str, Any]]:
        with self._pending_orders_lock:
            pending = list(self._pending_orders)
            self._pending_orders.clear()

        orders: list[Mapping[str, Any]] = []
        for entry in pending:
            payload = dict(entry)
            metadata = payload.get("metadata")
            if isinstance(metadata, Mapping):
                payload["metadata"] = dict(metadata)
            orders.append(payload)

        signal_orders = await super().generate_orders()
        orders.extend(signal_orders)

        self.logger.debug(
            "Dispatching %d queued candle order(s) via strategy runner",
            len(orders),
        )

        if orders:
            last_order = orders[-1]
            self._telemetry_set_phase_status(
                self._PHASE_DISPATCH,
                status="dispatching",
                status_code="orders_ready",
            )
            self._telemetry_update_phase_metrics(
                self._PHASE_DISPATCH,
                queued_orders=0,
                last_order_side=last_order.get("side"),
                last_order_quantity=last_order.get("quantity"),
            )

        return orders

    # ------------------------------------------------------------------
    def _history_replay_config(self) -> HistoryReplayConfig:
        base = HistoryReplayConfig()
        try:
            raw_max = int(self.history_chunk_max_bars)
        except Exception:
            raw_max = base.max_bars_per_request
        max_bars = raw_max if raw_max > 1 else base.max_bars_per_request
        span = self.history_chunk_max_span
        if isinstance(span, (int, float)):
            span = timedelta(seconds=float(span))
        if not isinstance(span, timedelta):
            span = base.max_span
        if span <= timedelta(0):
            span = base.max_span
        try:
            attempts = max(1, int(self.history_retry_attempts))
        except Exception:
            attempts = base.retry_attempts
        try:
            delay = max(0.0, float(self.history_retry_delay))
        except Exception:
            delay = base.retry_delay
        try:
            backoff = max(1.0, float(self.history_retry_backoff))
        except Exception:
            backoff = base.backoff_multiplier
        return HistoryReplayConfig(
            max_bars_per_request=max_bars,
            max_span=span,
            retry_attempts=attempts,
            retry_delay=delay,
            backoff_multiplier=backoff,
        )

    # ------------------------------------------------------------------
    def _ensure_unified_subscription(self) -> None:
        if not self._use_unified_data:
            return
        if self._data_layer_subscription is not None:
            return
        manager = self._data_layer_manager
        if manager is None:
            try:
                manager = get_data_source_manager()
            except RuntimeError:
                self.logger.warning(
                    "Unified candle subscription unavailable because data source manager is not configured"
                )
                self._telemetry_log(
                    "Unified candle subscription unavailable; remaining on legacy pipeline",
                    level="WARN",
                    tone="warning",
                )
                self._use_unified_data = False
                self._sync_required_market_streams()
                self._telemetry_update_phase_metrics(
                    self._PHASE_SUBSCRIPTION,
                    mode="legacy",
                    interval=self.interval,
                    symbol=self.symbol or None,
                )
                return
        self._data_layer_manager = manager
        channel = self._resolve_bar_channel()
        now = datetime.now(timezone.utc)
        delta = self._interval_delta
        # Compute a backfill window based on desired history_limit
        try:
            limit = max(1, int(self.history_limit))
        except Exception:
            limit = 1
        start = _floor_timestamp(now - (delta * limit), delta)
        request = DataSubscriptionRequest(
            channel=channel,
            symbol=self.symbol,
            interval=self.interval,
            options={
                "interval": self.interval,
                "start": start,
                "end": now,
            },
        )
        config = self._history_replay_config()
        history_records: Sequence[Mapping[str, Any]] = []
        if not self._history_backfill_completed:
            try:
                history_records = self._run_coro(
                    load_history_with_backoff(
                        manager,
                        request,
                        start=start,
                        end=now,
                        interval=delta,
                        config=config,
                        logger=self.logger,
                    )
                )
                self._history_backfill_completed = True
                self._history_backfill_failed = False
            except DataSourceError as exc:
                self._history_backfill_failed = True
                self.logger.warning(
                    "Unified candle history unavailable for symbol=%s interval=%s: %s",
                    self.symbol,
                    self.interval,
                    exc,
                )
                try:
                    retry_delay = float(getattr(config, "retry_delay", 0.0))
                    retry_attempts = int(getattr(config, "retry_attempts", 1))
                except Exception:
                    retry_delay = 0.0
                    retry_attempts = 1
                if retry_delay <= 0.0 or retry_attempts <= 1:
                    raise

        history_list = list(history_records or [])
        if history_list:
            anchor = datetime.min.replace(tzinfo=timezone.utc)
            history_list.sort(
                key=lambda item: (
                    self._maybe_parse_timestamp(
                        item.get("end")
                        or item.get("close_time")
                        or item.get("start")
                        or item.get("open_time")
                        or item.get("timestamp")
                    )
                    or anchor
                )
            )
        total_replayed = len(history_list)
        self._telemetry_update_phase_metrics(
            self._PHASE_SUBSCRIPTION,
            history_replay_count=total_replayed,
        )
        if total_replayed:
            aggregated_history: list[Mapping[str, Any]] = []
            self._history_replay_in_progress = True
            try:
                self._reset_unified_bucket()
                for candle in history_list[-self.history_limit :]:
                    if not candle:
                        continue
                    symbol = str(candle.get("symbol") or "").upper()
                    if symbol and symbol != self.symbol:
                        continue
                    closed = self._ingest_bar_payload(candle)
                    if closed is not None:
                        aggregated_history.append(closed)
                leftovers = self._flush_unified_bucket(close_partial=True)
                if leftovers:
                    aggregated_history.extend(leftovers)
                self._reset_unified_bucket()
                if aggregated_history:
                    anchor = datetime.min.replace(tzinfo=timezone.utc)
                    aggregated_history.sort(
                        key=lambda item: self._extract_candle_end(item) or anchor
                    )
                    dispatched: list[Mapping[str, Any]] = []
                    self._last_processed_candle_start = None
                    self._last_processed_candle_end = None
                    with self._candles_lock:
                        self._candles.clear()
                        for candle in aggregated_history[-self.history_limit :]:
                            normalised = self._normalise_candle(candle, is_closed=True)
                            if normalised is not None:
                                self._candles.append(normalised)
                                dispatched.append(normalised)
                    self.logger.info(
                        "Replayed %d unified historical candle(s) for symbol=%s interval=%s",
                        len(aggregated_history),
                        self.symbol,
                        self.interval,
                    )
                    self._telemetry_log(
                        f"Replayed {len(aggregated_history)} historical candle(s) from unified feed",
                        level="INFO",
                        tone="positive",
                        phase=self._PHASE_SUBSCRIPTION,
                        deduplicate=False,
                    )
                    for snapshot in dispatched:
                        self._invoke_candle_handlers(snapshot)
            finally:
                self._history_replay_in_progress = False
        # preserve failure flag when historical backfill was unavailable; it will be cleared on first live candle
        self._data_layer_subscription = self._run_coro(manager.subscribe(request))
        pattern = self._resolve_unified_event_channel()
        self._unified_event_channel = pattern
        token = manager.event_bus.subscribe(pattern, self._handle_candle_event)
        self._event_bus_token = token
        self.logger.info(
            "Subscribed to unified candle stream symbol=%s interval=%s",
            self.symbol,
            self.interval,
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_SUBSCRIPTION,
            subscription_channel=request.channel,
            event_pattern=pattern,
        )
        self._run_coro(self._check_bar_stream_health())
        if self._health_check_enabled:
            self._schedule_coroutine(self._schedule_periodic_health_check())
        if self._history_backfill_failed:
            self._schedule_coroutine(self._retry_history_backfill_after_delay())
        if not self._bar_stream_missing and not self._history_backfill_failed:
            self._on_subscription_connected(
                reason="Unified candle subscription established",
                cause="Unified candle subscription active",
                cause_code="subscription_connected",
                details={
                    "history_replay_count": total_replayed,
                    "subscription_channel": request.channel,
                },
            )

    # ------------------------------------------------------------------
    def _teardown_unified_subscription(self) -> None:
        manager = self._data_layer_manager
        if manager is None:
            self._data_layer_subscription = None
            self._event_bus_token = None
            return
        if self._event_bus_token is not None:
            try:
                manager.event_bus.unsubscribe(self._event_bus_token)
            except Exception:  # pragma: no cover - defensive logging
                self.logger.debug("Failed to unsubscribe candle event bus", exc_info=True)
        if self._data_layer_subscription is not None:
            try:
                self._run_coro(manager.unsubscribe(self._data_layer_subscription))
            except Exception:  # pragma: no cover - defensive logging
                self.logger.debug("Failed to cancel candle data subscription", exc_info=True)
        self._data_layer_subscription = None
        self._event_bus_token = None
        disconnected_at = datetime.now(timezone.utc)
        connected_since = self._subscription_connected_at
        self._subscription_connected_at = None
        self._stop_subscription_heartbeat()
        self._telemetry_set_phase_status(
            self._PHASE_SUBSCRIPTION,
            status="disconnected",
            status_code="stopped",
            status_reason="Unified candle subscription torn down",
            status_cause="Strategy stopped or symbol configuration updated",
            status_cause_code="subscription_stopped",
            status_details={
                "disconnected_at": self._telemetry_format_value(disconnected_at),
                "connected_since": self._telemetry_format_value(connected_since),
                "last_retry_started_at": self._telemetry_format_value(
                    self._last_retry_started_at
                ),
                "last_retry_completed_at": self._telemetry_format_value(
                    self._last_retry_completed_at
                ),
            },
            timestamp=disconnected_at,
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_SUBSCRIPTION,
            connected_since=None,
            last_heartbeat=None,
        )
        self._telemetry_log(
            "Unified candle subscription torn down",
            level="INFO",
            tone="neutral",
        )

    # ------------------------------------------------------------------
    def _handle_candle_event(self, envelope: EventEnvelope) -> None:
        payload = envelope.payload or {}
        ts_ns = envelope.ts_ns
        self._handle_runner_bar_payload(
            payload, ts_ns=ts_ns, symbol_hint=envelope.symbol
        )

    # ------------------------------------------------------------------
    def _handle_runner_bar_payload(
        self,
        payload: Mapping[str, Any],
        *,
        ts_ns: int | None = None,
        symbol_hint: str | None = None,
    ) -> None:
        symbol = (payload.get("symbol") or symbol_hint or "").upper()
        if symbol and symbol != self.symbol:
            try:
                self._telemetry_log(
                    "Incoming bar symbol mismatch; skipping",
                    level="WARN",
                    tone="warning",
                    phase=self._PHASE_SUBSCRIPTION,
                    deduplicate=True,
                    details={
                        "incoming_symbol": symbol,
                        "strategy_symbol": self.symbol,
                    },
                )
            except Exception:
                pass
            try:
                self.logger.warning(
                    "Incoming bar symbol mismatch; skipping",
                    extra={
                        "event": "strategy.subscription.symbol_mismatch",
                        "strategy": self.name,
                        "incoming_symbol": symbol,
                        "strategy_symbol": self.symbol,
                    },
                )
            except Exception:
                pass
            return

        candidates: list[Mapping[str, Any]] = []
        bar_payload = payload.get("bar")
        payload_is_snapshot = bool(payload.get("is_snapshot"))
        if isinstance(bar_payload, Mapping):
            enriched_bar = dict(bar_payload)
            for key in (
                "symbol",
                "volume",
                "timestamp",
                "interval",
                "timeframe",
                "bar_size",
            ):
                if key not in enriched_bar and key in payload:
                    enriched_bar[key] = payload.get(key)
            if "is_snapshot" not in enriched_bar and "is_snapshot" in payload:
                enriched_bar["is_snapshot"] = payload.get("is_snapshot")
            if "is_closed" not in enriched_bar:
                enriched_bar["is_closed"] = True if payload_is_snapshot else not bool(
                    enriched_bar.get("is_snapshot")
                )
            closed_bar = self._ingest_bar_payload(enriched_bar, ts_ns=ts_ns)
            if closed_bar is not None:
                candidates.append(closed_bar)
        elif isinstance(payload.get("bars"), Sequence):
            for bar in payload["bars"]:  # type: ignore[index]
                if not isinstance(bar, Mapping):
                    continue
                enriched_bar = dict(bar)
                if "symbol" not in enriched_bar and symbol:
                    enriched_bar["symbol"] = symbol
                for key in ("interval", "timeframe", "bar_size"):
                    if key not in enriched_bar and key in payload:
                        enriched_bar[key] = payload.get(key)
                if payload_is_snapshot and "is_closed" not in enriched_bar:
                    enriched_bar["is_closed"] = True
                closed_bar = self._ingest_bar_payload(enriched_bar, ts_ns=ts_ns)
                if closed_bar is not None:
                    candidates.append(closed_bar)
        else:
            enriched_payload = dict(payload)
            if symbol:
                enriched_payload.setdefault("symbol", symbol)
            candidates.append(enriched_payload)

        target_token = _normalize_interval_token(self.interval)
        dated_candidates: dict[datetime, Mapping[str, Any]] = {}
        for candidate in candidates:
            interval_hint = _normalize_interval_token(
                candidate.get("interval")
                or candidate.get("timeframe")
                or candidate.get("bar_size")
            )
            source_seconds = _interval_seconds(interval_hint)
            if interval_hint is not None and target_token is not None:
                if interval_hint != target_token:
                    target_seconds = int(self._interval_delta.total_seconds())
                    if (
                        source_seconds is None
                        or source_seconds > target_seconds
                        or target_seconds % source_seconds != 0
                    ):
                        continue

            is_closed = bool(candidate.get("is_closed"))
            if not is_closed:
                continue

            alignment_delta = self._interval_delta
            if source_seconds is not None and source_seconds > 0:
                alignment_delta = timedelta(seconds=source_seconds)

            raw_end = self._maybe_parse_timestamp(
                candidate.get("end")
                or candidate.get("close_time")
                or candidate.get("timestamp")
            )
            raw_start = self._maybe_parse_timestamp(
                candidate.get("start") or candidate.get("open_time")
            )
            if raw_end is not None and not self._is_interval_boundary(
                raw_end, alignment_delta
            ):
                continue
            if raw_end is None and raw_start is not None and not self._is_interval_boundary(
                raw_start, alignment_delta
            ):
                continue

            end_ts = self._extract_candle_end(candidate)
            if end_ts is None:
                continue
            dated_candidates[end_ts] = candidate

        if not dated_candidates:
            return

        ordered = sorted(dated_candidates.items(), key=lambda item: item[0])
        for end_ts, candidate in ordered:
            if end_ts is not None and self._last_processed_candle_end is not None:
                if end_ts <= self._last_processed_candle_end:
                    continue

            if self._history_backfill_failed:
                self._history_backfill_failed = False
                self._telemetry_set_phase_status(
                    self._PHASE_SUBSCRIPTION,
                    status="connected",
                    status_code="subscribed",
                )
                self._telemetry_log(
                    "Unified candle subscription established from live stream",
                    level="INFO",
                    tone="positive",
                    phase=self._PHASE_SUBSCRIPTION,
                    deduplicate=False,
                )

            normalised = self._normalise_candle(candidate, is_closed=True)
            if normalised is None:
                continue
            self._telemetry_record_unified_candle(normalised)
            log_entry = self._format_unified_candle_log(normalised)
            if log_entry is not None:
                message, details = log_entry
                log_details: Mapping[str, Any] | None = None
                if isinstance(details, Mapping):
                    log_details = dict(details)
                self._telemetry_log(
                    message,
                    level="INFO",
                    tone="positive",
                    phase=self._PHASE_AGGREGATION,
                    deduplicate=False,
                    details=log_details,
                )
            with self._candles_lock:
                if self._candles:
                    last = self._candles[-1]
                    if last.get("end") == normalised.get("end"):
                        self._candles[-1] = normalised
                    else:
                        self._candles.append(normalised)
                else:
                    self._candles.append(normalised)
            self._last_event_ts = ts_ns
            self._invoke_candle_handlers(normalised)

    # ------------------------------------------------------------------
    async def on_market_event(self, event: Mapping[str, Any]) -> None:
        if not isinstance(event, Mapping):
            return
        bar_payload = event.get("bar") if isinstance(event.get("bar"), Mapping) else None
        interval_hint = (
            (bar_payload or event).get("interval")
            or (bar_payload or event).get("timeframe")
            or (bar_payload or event).get("bar_size")
        )
        target_interval = _normalize_interval_token(self.interval)
        if interval_hint is not None and target_interval is not None:
            hint_token = _normalize_interval_token(str(interval_hint))
            if hint_token is not None and hint_token != target_interval:
                source_seconds = _interval_seconds(hint_token)
                target_seconds = int(self._interval_delta.total_seconds())
                if (
                    source_seconds is not None
                    and source_seconds <= target_seconds
                    and target_seconds % source_seconds == 0
                ):
                    pass
                else:
                    try:
                        self._telemetry_log(
                            "Incoming bar interval mismatch; skipping",
                            level="WARN",
                            tone="warning",
                            phase=self._PHASE_SUBSCRIPTION,
                            deduplicate=True,
                            details={
                                "incoming_interval": str(interval_hint),
                                "normalized_incoming": hint_token,
                                "strategy_interval": target_interval,
                            },
                        )
                    except Exception:
                        pass
                    try:
                        self.logger.warning(
                            "Incoming bar interval mismatch; skipping",
                            extra={
                                "event": "strategy.subscription.interval_mismatch",
                                "strategy": self.name,
                                "incoming_interval": str(interval_hint),
                                "normalized_incoming": hint_token,
                                "strategy_interval": target_interval,
                            },
                        )
                    except Exception:
                        pass
                    return

        if event.get("is_closed") is False:
            return
        if bar_payload is not None and bar_payload.get("is_closed") is False:
            return

        self._handle_runner_bar_payload(event or {})

    # ------------------------------------------------------------------
    def _format_unified_candle_log(
        self, candle: Mapping[str, Any]
    ) -> tuple[str, Mapping[str, Any] | None] | None:
        """Return optional aggregation log details for a closed unified candle."""

        return None

    # ------------------------------------------------------------------
    def _invoke_candle_handlers(self, candle: Mapping[str, Any]) -> None:
        snapshot = dict(candle)
        start = self._maybe_parse_timestamp(
            snapshot.get("start") or snapshot.get("open_time")
        )
        end = self._maybe_parse_timestamp(
            snapshot.get("end") or snapshot.get("close_time")
        )
        if end is None and start is not None:
            end = start + self._interval_delta
        if end is not None and self._last_processed_candle_end is not None:
            if end <= self._last_processed_candle_end:
                return
        self._last_processed_candle_start = start
        self._last_processed_candle_end = end
        try:
            self.on_candle(dict(snapshot))
        except Exception:  # pragma: no cover - defensive logging
            self.logger.exception("Candle handler failed for %s", self.symbol)
        try:
            self._maybe_execute_base_exit(snapshot)
        except Exception:  # pragma: no cover - defensive logging
            self.logger.exception("Base exit evaluation failed for %s", self.symbol)
        dispatcher = self._dispatch_event
        if dispatcher is None:
            return
        coroutine = dispatcher(dict(snapshot))
        if asyncio.iscoroutine(coroutine):
            queued_orders = 0
            last_order_side: str | None = None
            last_order_quantity: float | int | None = None
            last_order_type: str | None = None
            with self._pending_orders_lock:
                queued_orders = len(self._pending_orders)
                if queued_orders:
                    last_order = self._pending_orders[-1]
                    if isinstance(last_order, Mapping):
                        last_order_side = last_order.get("side")
                        last_order_quantity = last_order.get("quantity")
                        last_order_type = last_order.get("type")
            if queued_orders:
                self._telemetry_log(
                    "Dispatching candle with queued orders",
                    level="INFO",
                    tone="positive",
                    phase=self._PHASE_DISPATCH,
                    deduplicate=False,
                    details={
                        "end": snapshot.get("end"),
                        "close": snapshot.get("close"),
                        "queued_orders": queued_orders,
                        "last_order_side": last_order_side,
                        "last_order_quantity": last_order_quantity,
                        "last_order_type": last_order_type,
                    },
                )
            self._schedule_coroutine(coroutine)

    # ------------------------------------------------------------------
    def _schedule_coroutine(self, coro: Awaitable[Any]) -> None:
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            asyncio.create_task(coro)
            return
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, loop)
            return
        asyncio.run(coro)

    # ------------------------------------------------------------------
    def _maybe_execute_base_exit(self, candle: Mapping[str, Any]) -> None:
        if not getattr(self, "use_base_exit", True):
            return
        if getattr(self, "use_risk_service_exit", False):
            return
        if getattr(self, "_history_replay_in_progress", False):
            return
        price_raw = candle.get("close")
        if price_raw is None:
            return
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            return
        if not math.isfinite(price):
            return

        position, entry_price = self._resolve_position_state()

        if abs(position) <= 1e-9:
            self._base_exit_dispatched = False
            return

        exit_targets = self.evaluate_exit_signal(
            position=float(position),
            entry_price=entry_price,
            account_equity=getattr(self, "account_equity", None),
            bar=candle,
            is_dom=False,
        )
        if exit_targets is None:
            return
        stop_price = exit_targets.stop_loss
        take_profit = exit_targets.take_profit
        if stop_price is None and take_profit is None:
            return

        direction = 1.0 if position > 0 else -1.0
        triggered_sl = False
        triggered_tp = False
        if direction > 0:
            if stop_price is not None and price <= stop_price:
                triggered_sl = True
            if take_profit is not None and price >= take_profit:
                triggered_tp = True
        else:
            if stop_price is not None and price >= stop_price:
                triggered_sl = True
            if take_profit is not None and price <= take_profit:
                triggered_tp = True
        if not (triggered_sl or triggered_tp):
            return
        if self._base_exit_dispatched:
            return

        order_payload, exit_label = self._build_exit_order_payload(
            position=position,
            entry_price=entry_price,
            exit_targets=exit_targets,
            triggered_tp=triggered_tp,
            triggered_sl=triggered_sl,
        )
        try:
            setattr(self, "_position", float(position))
        except Exception:
            pass
        if self.queue_order(order_payload):
            self._base_exit_dispatched = True
            self._telemetry_log(
                exit_label,
                level="INFO",
                tone="positive",
                phase=self._PHASE_SIGNALS,
                details={
                    "side": order_payload.get("side"),
                    "quantity": float(order_payload.get("quantity") or 0.0),
                    "trigger": "take_profit" if triggered_tp else "stop_loss",
                },
                deduplicate=False,
            )

    async def _retry_history_backfill_after_delay(self) -> None:
        if not self._use_unified_data:
            return
        manager = self._data_layer_manager
        if manager is None:
            try:
                manager = get_data_source_manager()
            except RuntimeError:
                return
        # Abort retry when live bar stream is active or backfill already recovered
        if not self._history_backfill_failed:
            return
        if not self._bar_stream_missing:
            return
        config = self._history_replay_config()
        try:
            delay = max(0.0, float(self.history_retry_delay))
        except Exception:
            delay = 0.0
        if delay > 0.0:
            await asyncio.sleep(delay)
        now = datetime.now(timezone.utc)
        delta = self._interval_delta
        try:
            limit = max(1, int(self.history_limit))
        except Exception:
            limit = 1
        start = _floor_timestamp(now - (delta * limit), delta)
        request = DataSubscriptionRequest(
            channel=self._resolve_bar_channel(),
            symbol=self.symbol,
            interval=self.interval,
            options={
                "interval": self.interval,
                "start": start,
                "end": now,
            },
        )
        records: Sequence[Mapping[str, Any]] = []
        try:
            records = await load_history_with_backoff(
                manager,
                request,
                start=start,
                end=now,
                interval=delta,
                config=config,
                logger=self.logger,
            )
        except Exception:
            self.logger.warning(
                "Unified candle backfill retry failed",
                extra={
                    "event": "strategy.history.retry_failed",
                    "strategy": self.name,
                    "symbol": self.symbol,
                    "interval": self.interval,
                },
            )
            return
        self._history_backfill_completed = True
        ordered_records = list(records or [])
        if ordered_records:
            anchor = datetime.min.replace(tzinfo=timezone.utc)
            ordered_records.sort(
                key=lambda item: (
                    self._maybe_parse_timestamp(
                        item.get("end")
                        or item.get("close_time")
                        or item.get("start")
                        or item.get("open_time")
                        or item.get("timestamp")
                    )
                    or anchor
                )
            )
        aggregated: list[Mapping[str, Any]] = []
        self._reset_unified_bucket()
        for item in ordered_records[-self.history_limit :]:
            if not item:
                continue
            closed = self._ingest_bar_payload(item)
            if closed is not None:
                aggregated.append(closed)
        leftovers = self._flush_unified_bucket(close_partial=True)
        if leftovers:
            aggregated.extend(leftovers)
        if aggregated:
            anchor = datetime.min.replace(tzinfo=timezone.utc)
            aggregated.sort(key=lambda item: self._extract_candle_end(item) or anchor)
        dispatched: list[Mapping[str, Any]] = []
        with self._candles_lock:
            for candle in aggregated[-self.history_limit :]:
                normalised = self._normalise_candle(candle, is_closed=True)
                if normalised is not None:
                    dispatched.append(normalised)
        for snapshot in dispatched:
            self._invoke_candle_handlers(snapshot)
        self._history_backfill_failed = False
        if dispatched:
            self._telemetry_log(
                f"Replayed {len(dispatched)} historical candle(s) after backfill retry",
                level="INFO",
                tone="positive",
                phase=self._PHASE_SUBSCRIPTION,
                deduplicate=False,
            )

    # ------------------------------------------------------------------
    def _is_connection_unavailable_error(self, exc: Exception) -> bool:
        message = str(exc).strip().lower()
        return "ibkr connection is not active" in message or "ibkr connection not active" in message

    # ------------------------------------------------------------------
    def _start_retry_thread(self) -> None:
        if self._subscription_retry_thread and self._subscription_retry_thread.is_alive():
            return
        self._subscription_retry_stop.clear()
        thread = threading.Thread(
            target=self._retry_subscription_loop,
            name=f"{self.name}-candle-retry",
            daemon=True,
        )
        self._subscription_retry_thread = thread
        self._subscription_retry_attempts = 0
        self._last_retry_started_at = None
        self._last_retry_completed_at = None
        self._subscription_connected_at = None
        self._stop_subscription_heartbeat()
        now = datetime.now(timezone.utc)
        self._telemetry_set_phase_status(
            self._PHASE_SUBSCRIPTION,
            status="waiting",
            status_code="retrying",
            status_reason="Waiting for IBKR connection to restore candle subscription",
            status_cause="Unified candle subscription disconnected; awaiting reconnect",
            status_cause_code="subscription_retry_waiting",
            status_details={
                "retry_attempt": 0,
                "retry_started_at": self._telemetry_format_value(now),
                "last_retry_completed_at": self._telemetry_format_value(
                    self._last_retry_completed_at
                ),
            },
            timestamp=now,
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_SUBSCRIPTION,
            connected_since=None,
            last_heartbeat=None,
        )
        self._telemetry_log(
            "Waiting for IBKR connection to restore candle subscription",
            level="WARN",
            tone="warning",
        )
        thread.start()

    # ------------------------------------------------------------------
    def _cancel_retry_thread(self) -> None:
        thread = self._subscription_retry_thread
        if not thread:
            self._subscription_retry_stop.clear()
            return
        self._subscription_retry_stop.set()
        try:
            thread.join(timeout=2.0)
        except Exception:  # pragma: no cover - defensive
            pass
        self._subscription_retry_thread = None
        self._subscription_retry_stop = Event()

    # ------------------------------------------------------------------
    def _retry_subscription_loop(self) -> None:
        delay = 2.0
        while not self._subscription_retry_stop.is_set():
            if not self.active or not self.enabled:
                break
            attempt = self._subscription_retry_attempts + 1
            attempt_started_at = datetime.now(timezone.utc)
            self._subscription_retry_attempts = attempt
            self._last_retry_started_at = attempt_started_at
            manager = self._connection_manager
            is_connected = False
            if manager is not None:
                try:
                    is_connected = bool(manager.is_connected())
                except Exception:  # pragma: no cover - defensive
                    is_connected = False
            awaiting_connection = manager is not None and not is_connected
            status_details: dict[str, Any] = {
                "retry_attempt": attempt,
                "retry_started_at": self._telemetry_format_value(attempt_started_at),
                "last_retry_completed_at": self._telemetry_format_value(
                    self._last_retry_completed_at
                ),
            }
            status_cause = (
                "Attempting to resubscribe to unified candle feed"
                if not awaiting_connection
                else "Awaiting IBKR connection before retrying"
            )
            status_code = "retrying"
            self._telemetry_set_phase_status(
                self._PHASE_SUBSCRIPTION,
                status="waiting",
                status_code=status_code,
                status_reason=f"Retrying unified candle subscription (attempt {attempt})",
                status_cause=status_cause,
                status_cause_code="subscription_retry_pending",
                status_details=status_details,
                timestamp=attempt_started_at,
            )
            self._telemetry_update_phase_metrics(
                self._PHASE_SUBSCRIPTION,
                retry_attempt=attempt,
                last_retry_started_at=attempt_started_at,
                last_retry_completed_at=self._last_retry_completed_at,
            )
            if manager is None or not awaiting_connection:
                try:
                    self._ensure_unified_subscription()
                    completed_at = datetime.now(timezone.utc)
                    self._last_retry_completed_at = completed_at
                    status_details["retry_completed_at"] = self._telemetry_format_value(
                        completed_at
                    )
                    self.logger.info(
                        "Candle subscription restored after IBKR connection became available symbol=%s interval=%s",
                        self.symbol,
                        self.interval,
                    )
                    self._on_subscription_connected(
                        reason="Unified candle subscription restored",
                        cause=f"Subscription restored after retry attempt {attempt}",
                        cause_code="subscription_reconnected",
                        details=status_details,
                        timestamp=completed_at,
                    )
                    self._telemetry_log(
                        "Unified candle subscription restored",
                        level="INFO",
                        tone="positive",
                    )
                    return
                except DataSourceError as exc:
                    completed_at = datetime.now(timezone.utc)
                    self._last_retry_completed_at = completed_at
                    status_details["retry_completed_at"] = self._telemetry_format_value(
                        completed_at
                    )
                    if self._is_connection_unavailable_error(exc):
                        self._telemetry_set_phase_status(
                            self._PHASE_SUBSCRIPTION,
                            status="waiting",
                            status_code="awaiting_connection",
                            status_reason="IBKR connection unavailable during retry",
                            status_cause=str(exc),
                            status_cause_code="subscription_retry_connection_wait",
                            status_details=status_details,
                            timestamp=completed_at,
                        )
                        continue
                    self.logger.error(
                        "Failed to restore candle subscription after reconnect: %s",
                        exc,
                    )
                    failure_message = f"Failed to restore candle subscription: {exc}"
                    self._telemetry_set_phase_status(
                        self._PHASE_SUBSCRIPTION,
                        status="failed",
                        status_code="error",
                        status_reason="Failed to restore candle subscription",
                        status_cause=failure_message,
                        status_cause_code="subscription_failed",
                        status_details=status_details,
                        timestamp=completed_at,
                    )
                    self._telemetry_log(
                        failure_message,
                        level="ERROR",
                        tone="negative",
                    )
                    return
                except Exception as exc:
                    completed_at = datetime.now(timezone.utc)
                    self._last_retry_completed_at = completed_at
                    status_details["retry_completed_at"] = self._telemetry_format_value(
                        completed_at
                    )
                    self.logger.exception(
                        "Unexpected error while restoring candle subscription: %s",
                        exc,
                    )
                    failure_message = "Unexpected error while restoring candle subscription"
                    self._telemetry_set_phase_status(
                        self._PHASE_SUBSCRIPTION,
                        status="failed",
                        status_code="error",
                        status_reason=failure_message,
                        status_cause=failure_message,
                        status_cause_code="subscription_error",
                        status_details=status_details,
                        timestamp=completed_at,
                    )
                    self._telemetry_log(
                        failure_message,
                        level="ERROR",
                        tone="negative",
                    )
                    return
            if self._subscription_retry_stop.wait(delay):
                break
            delay = min(delay * 1.5, 30.0)
        self._subscription_retry_thread = None
        now = datetime.now(timezone.utc)
        self._telemetry_set_phase_status(
            self._PHASE_SUBSCRIPTION,
            status="waiting",
            status_code="retrying",
            status_reason="Subscription retry loop exited before reconnect",
            status_cause="Retry loop cancelled or strategy stopped",
            status_cause_code="subscription_retry_cancelled",
            status_details={
                "retry_attempt": self._subscription_retry_attempts,
                "last_retry_started_at": self._telemetry_format_value(
                    self._last_retry_started_at
                ),
                "last_retry_completed_at": self._telemetry_format_value(
                    self._last_retry_completed_at
                ),
            },
            timestamp=now,
        )

    # ------------------------------------------------------------------
    def _on_subscription_connected(
        self,
        *,
        reason: str,
        cause: str,
        cause_code: str,
        details: Mapping[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        connected_at = timestamp or datetime.now(timezone.utc)
        self._subscription_connected_at = connected_at
        self._subscription_retry_attempts = 0
        self._last_retry_started_at = None
        self._last_retry_completed_at = connected_at
        detail_payload: dict[str, Any] = {
            "connected_at": self._telemetry_format_value(connected_at),
            "last_retry_completed_at": self._telemetry_format_value(
                self._last_retry_completed_at
            ),
        }
        if details:
            detail_payload.update(details)
        self._telemetry_set_phase_status(
            self._PHASE_SUBSCRIPTION,
            status="connected",
            status_code="subscribed",
            status_reason=reason,
            status_cause=cause,
            status_cause_code=cause_code,
            status_details=detail_payload,
            timestamp=connected_at,
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_SUBSCRIPTION,
            connected_since=connected_at,
            last_heartbeat=connected_at,
        )
        self._start_subscription_heartbeat()

    # ------------------------------------------------------------------
    def _start_subscription_heartbeat(self) -> None:
        task = self._subscription_heartbeat_task
        if task is not None and not task.done():
            return
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self.logger.debug(
                    "Cannot schedule subscription heartbeat: no running loop"
                )
                return
            self._loop = loop
        if not loop.is_running():
            self.logger.debug(
                "Cannot schedule subscription heartbeat: event loop not running"
            )
            return

        async def heartbeat() -> None:
            while (
                self.active
                and self.enabled
                and self._data_layer_subscription is not None
                and self._subscription_connected_at is not None
            ):
                now = datetime.now(timezone.utc)
                self._telemetry_update_phase_metrics(
                    self._PHASE_SUBSCRIPTION,
                    connected_since=self._subscription_connected_at,
                    last_heartbeat=now,
                )
                try:
                    await asyncio.sleep(self._subscription_heartbeat_interval)
                except asyncio.CancelledError:
                    break

        try:
            self._subscription_heartbeat_task = loop.create_task(
                heartbeat(),
                name=f"{self.name}-subscription-heartbeat",
            )
        except Exception:  # pragma: no cover - defensive
            self.logger.exception("Failed to start subscription heartbeat task")

    # ------------------------------------------------------------------
    def _stop_subscription_heartbeat(self) -> None:
        task = self._subscription_heartbeat_task
        if task is None:
            return
        if not task.done():
            task.cancel()
        self._subscription_heartbeat_task = None

    # ------------------------------------------------------------------
    def _reset_runtime_counters(self) -> None:
        self._tick_count = 0
        self._closed_candle_count = 0
        self._last_tick_timestamp = None
        self._current_candle_volume = 0.0
        self._last_closed_timestamp = None
        self._last_closed_volume = 0.0
        self._last_closed_price = 0.0
        self._last_runtime_status = None

    # ------------------------------------------------------------------
    def _telemetry(self) -> DomRuntimeTelemetryService | None:
        return self.runtime_telemetry

    def _telemetry_strategy_id(self) -> StrategyIdentifier:
        identifier = getattr(self, "identifier", None)
        if identifier is not None:
            return identifier
        symbol = (self.symbol or "").strip().upper() or "UNKNOWN"
        interval = (self.interval or "").strip() or "1m"
        return f"{self.name}:{symbol}:{interval}"

    def _telemetry_start_session(self) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        try:
            telemetry.start_session(
                self._telemetry_strategy_id(),
                subscription_id=self.interval,
                symbol=self.symbol or None,
            )
            telemetry.set_initial_status(
                self._telemetry_strategy_id(),
                reason="Awaiting first candle/bar data",
                cause="Awaiting first candle/bar data",
                data_label="Candle",
                stale_template="No candle/bar data received for {seconds} seconds",
            )
        except Exception:  # pragma: no cover - defensive
            self.logger.exception("Failed to start telemetry session")

    def _telemetry_stop_session(self, reason: str | None = None) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        try:
            telemetry.stop_session(self._telemetry_strategy_id(), reason=reason)
        except KeyError:
            return
        except Exception:  # pragma: no cover - defensive
            self.logger.exception("Failed to stop telemetry session")

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
        try:
            telemetry.set_phase_status(
                self._telemetry_strategy_id(),
                phase,
                status=status,
                status_code=status_code,
                status_reason=status_reason,
                status_cause=status_cause,
                status_cause_code=status_cause_code,
                status_details=status_details,
                timestamp=timestamp,
            )
        except KeyError:
            return
        except Exception:  # pragma: no cover - defensive
            self.logger.exception("Failed to set telemetry phase status")

    def _telemetry_update_phase_metrics(
        self,
        phase: str,
        updates: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        combined: dict[str, Any] = {}
        if updates:
            combined.update(dict(updates))
        if kwargs:
            combined.update(kwargs)
        if not combined:
            return
        formatted = {
            key: self._telemetry_format_value(value) for key, value in combined.items()
        }
        try:
            telemetry.update_phase_metrics(
                self._telemetry_strategy_id(), phase, formatted
            )
        except KeyError:
            return
        except Exception:  # pragma: no cover - defensive
            self.logger.exception("Failed to update telemetry metrics")

    def _telemetry_log(
        self,
        message: str,
        *,
        level: str = "INFO",
        tone: str = "neutral",
        phase: str | None = None,
        deduplicate: bool = True,
        details: Mapping[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        if phase is None and deduplicate and message == self._last_runtime_status:
            return
        message_to_log = message
        if phase == self._PHASE_SUBSCRIPTION:
            tags: list[str] = []
            if self.name:
                tags.append(f"strategy={self.name}")
            if self.symbol:
                tags.append(f"symbol={self.symbol}")
            if self.interval:
                tags.append(f"interval={self.interval}")
            if tags:
                suffix = f" ({', '.join(tags)})"
                if suffix not in message_to_log:
                    message_to_log = f"{message_to_log}{suffix}"
            if details is None:
                details = {
                    "strategy": self.name,
                    "symbol": self.symbol,
                    "interval": self.interval,
                }
        timestamp = timestamp or self._telemetry_default_timestamp()
        try:
            telemetry.log_event(
                self._telemetry_strategy_id(),
                message_to_log,
                level=level,
                tone=tone,
                details=details,
                phase=phase,
                timestamp=timestamp,
            )
            if phase is None:
                self._last_runtime_status = message if deduplicate else None
        except KeyError:
            return
        except Exception:  # pragma: no cover - defensive
            self.logger.exception("Failed to log telemetry event")
        if isinstance(details, Mapping):
            self._telemetry_record_condition_evaluations(details, phase)

    def _telemetry_log_signal_waiting(
        self,
        *,
        step: str,
        reason: str,
        metric: float | str | None = None,
        threshold: float | str | None = None,
        comparison: str | None = None,
        details: Mapping[str, Any] | None = None,
        status_code: str = "awaiting_data",
        timestamp: datetime | None = None,
    ) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        stage = self._PHASE_SIGNALS
        timestamp = timestamp or self._telemetry_default_timestamp()
        normalized_details = dict(details) if isinstance(details, Mapping) else None
        self._telemetry_set_phase_status(
            stage,
            status="waiting",
            status_code=status_code,
            status_reason=reason,
            status_details=normalized_details,
            timestamp=timestamp,
        )
        fingerprint = (
            step,
            reason,
            metric,
            threshold,
            comparison,
            tuple(sorted(normalized_details.items())) if normalized_details else None,
        )
        if self._last_signal_wait_state == fingerprint:
            return
        self._last_signal_wait_state = fingerprint
        self._telemetry_log_processing_step(
            step=step,
            metric=metric,
            threshold=threshold,
            comparison=comparison or "status",
            passed=False,
            stage=stage,
            details=normalized_details,
            timestamp=timestamp,
        )
        self._telemetry_log_phase_detail(
            phase=stage,
            message=reason,
            level="INFO",
            tone="neutral",
            details=normalized_details,
            timestamp=timestamp,
        )

    def _telemetry_log_processing_step(
        self,
        *,
        step: str,
        metric: float | str | None = None,
        threshold: float | str | None = None,
        comparison: str | None = None,
        passed: bool | None = None,
        stage: str | None = None,
        details: Mapping[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        stage_name = stage or self._PHASE_DISPATCH
        timestamp = timestamp or self._telemetry_default_timestamp()
        try:
            telemetry.log_processing_step(
                self._telemetry_strategy_id(),
                step=step,
                metric=metric,
                threshold=threshold,
                comparison=comparison,
                passed=passed,
                stage=stage_name,
                details=details,
                timestamp=timestamp,
            )
        except KeyError:
            return
        except Exception:  # pragma: no cover - defensive
            self.logger.exception("Failed to record telemetry processing step")

    def _telemetry_log_phase_detail(
        self,
        *,
        phase: str,
        message: str,
        level: str = "INFO",
        tone: str = "neutral",
        details: Mapping[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        timestamp = timestamp or self._telemetry_default_timestamp()
        try:
            telemetry.log_event(
                self._telemetry_strategy_id(),
                message,
                level=level,
                tone=tone,
                details=details,
                phase=phase,
                timestamp=timestamp,
            )
        except KeyError:
            return
        except Exception:  # pragma: no cover - defensive
            self.logger.exception("Failed to append telemetry phase detail")

    def _telemetry_default_timestamp(self) -> datetime | None:
        return self._last_closed_timestamp or self._last_bar_received_at or datetime.now(timezone.utc)

    def _coordinator_facade(self) -> Any | None:
        facade = getattr(self, "market_data_subscription_health", None)
        if facade is not None:
            return facade
        return self._market_data_subscription_health

    def _telemetry_record_condition_evaluations(
        self,
        details: Mapping[str, Any],
        phase: str | None,
    ) -> None:
        evaluations = details.get("evaluations")
        if not isinstance(evaluations, Sequence):
            return
        stage_name = phase or self._PHASE_SIGNALS
        symbol = getattr(self, "symbol", "") or ""
        interval = getattr(self, "interval", "") or ""
        for index, evaluation in enumerate(evaluations):
            if not isinstance(evaluation, Mapping):
                continue
            condition = evaluation.get("condition") or evaluation.get("label")
            condition_text = str(condition or f"condition_{index + 1}")
            step_name = condition_text
            comparison = evaluation.get("comparison")
            threshold_value = evaluation.get("threshold")
            current_value = evaluation.get("current")
            passed = evaluation.get("passed")
            if comparison is None and isinstance(condition_text, str):
                for operator in ("<=", ">=", "==", "!=", "<", ">"):
                    if operator in condition_text:
                        lhs, _, rhs = condition_text.partition(operator)
                        if not comparison:
                            comparison = operator
                        step_name = lhs.strip() or condition_text
                        rhs_value = rhs.strip()
                        if threshold_value is None and rhs_value:
                            parsed_rhs = self._telemetry_normalise_metric_value(rhs_value)
                            if parsed_rhs is not None:
                                threshold_value = parsed_rhs
                        break
            metric_scalar = self._telemetry_normalise_metric_value(current_value)
            threshold_scalar = self._telemetry_normalise_metric_value(threshold_value)
            extra_details: dict[str, Any] = {
                "symbol": symbol,
                "interval": interval,
                "condition": condition_text,
            }
            if isinstance(current_value, Mapping):
                for key, value in current_value.items():
                    extra_details[f"current.{key}"] = value
            elif current_value is not None:
                extra_details["current"] = current_value
            if threshold_value is not None:
                if isinstance(threshold_value, Mapping):
                    for key, value in threshold_value.items():
                        extra_details[f"threshold.{key}"] = value
                else:
                    extra_details.setdefault("threshold", threshold_value)
            context_details = evaluation.get("details")
            if isinstance(context_details, Mapping):
                for key, value in context_details.items():
                    extra_details.setdefault(str(key), value)
            self._telemetry_log_processing_step(
                step=step_name,
                metric=metric_scalar,
                threshold=threshold_scalar,
                comparison=comparison,
                passed=passed if isinstance(passed, bool) else None,
                stage=stage_name,
                details=extra_details,
            )

    @staticmethod
    def _telemetry_normalise_metric_value(value: Any) -> float | str | None:
        if isinstance(value, (int, float)):
            numeric = float(value)
            if not math.isfinite(numeric):
                return str(value)
            return numeric
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                parsed = float(text)
            except ValueError:
                return text
            if math.isfinite(parsed):
                return parsed
            return text
        return None

    # ------------------------------------------------------------------
    def _bar_inactivity_threshold_seconds(self) -> float:
        base_interval = max(10.0, float(getattr(self, "_health_check_interval", 30.0)))
        threshold = max(base_interval * 2.0, 60.0)
        try:
            interval_seconds = float(self._interval_delta.total_seconds())
        except Exception:
            interval_seconds = 0.0
        if interval_seconds > 0:
            # Require at least two full bars (or 120s) before declaring inactivity.
            threshold = max(threshold, interval_seconds * 2.0, 120.0)
        return threshold

    # ------------------------------------------------------------------
    def _restart_bar_listener(self) -> None:
        if self._use_unified_data:
            return
        try:
            self._cancel_listener()
        except Exception:
            pass
        loop = self._loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        if loop is None or not loop.is_running():
            return
        try:
            self._listener_task = loop.create_task(
                self._run_listener(), name=f"{self.name}-candle-listener"
            )
        except Exception:
            self.logger.exception("Failed to restart candle listener after inactivity")
            self._telemetry_log(
                "Failed to restart candle listener",
                level="ERROR",
                tone="negative",
                phase=self._PHASE_SUBSCRIPTION,
                deduplicate=False,
            )

    # ------------------------------------------------------------------
    async def _schedule_periodic_health_check(self) -> None:
        """Schedule periodic health checks for the bar stream."""
        if not self._health_check_enabled:
            return
        task = self._health_check_task
        existing_interval = getattr(self, "_health_check_task_interval", None)
        if task is not None and not task.done() and existing_interval == self._health_check_interval:
            return
        
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self.logger.warning("Cannot schedule periodic health check: no event loop available")
                return
        
        if not loop.is_running():
            self.logger.warning("Cannot schedule periodic health check: event loop not running")
            return
        
        self._cancel_periodic_health_check()
        
        async def periodic_health_check() -> None:
            """Run periodic health checks while strategy is active."""
            try:
                await self._check_bar_stream_health()
            except Exception as exc:
                self.logger.exception("Initial health check failed: %s", exc)
            while self.active and self.enabled and self._health_check_enabled:
                try:
                    await asyncio.sleep(self._health_check_interval)
                    if self.active and self.enabled and self._health_check_enabled:
                        await self._check_bar_stream_health()
                except asyncio.CancelledError:
                    self.logger.debug("Periodic health check cancelled")
                    break
                except Exception as exc:
                    self.logger.exception("Periodic health check failed: %s", exc)
                    self._telemetry_log(
                        f"Periodic health check failed: {exc}",
                        level="ERROR",
                        tone="negative",
                        phase=self._PHASE_SUBSCRIPTION,
                    )
        
        try:
            task = loop.create_task(
                periodic_health_check(),
                name=f"{self.name}-health-check",
            )
            self._health_check_task = task
            self._health_check_task_interval = self._health_check_interval
            self.logger.info(
                "Scheduled periodic health checks every %.1f seconds for %s",
                self._health_check_interval,
                self.symbol,
            )
            self._telemetry_log(
                f"Scheduled periodic health checks every {self._health_check_interval:.1f}s",
                level="INFO",
                tone="positive",
                phase=self._PHASE_SUBSCRIPTION,
            )
        except Exception as exc:
            self.logger.exception("Failed to schedule periodic health check: %s", exc)
        await asyncio.sleep(0)

    # ------------------------------------------------------------------
    def _cancel_periodic_health_check(self) -> None:
        """Cancel the periodic health check task."""
        task = self._health_check_task
        if task is not None and not task.done():
            task.cancel()
            self._health_check_task = None
            self.logger.debug("Cancelled periodic health check task")

    async def _await_market_data_ready_and_subscribe(self) -> None:
        """Wait until the strategy is registered and market data is ready before subscribing."""
        facade = self._coordinator_facade()
        registration_checker = getattr(facade, "is_strategy_registered", None) if facade else None
        health_checker = getattr(facade, "market_data_ready", None) if facade else None
        registrar = getattr(facade, "ensure_bar_subscription", None) if facade else None
        wait_interval = 5.0
        while self.active and self.enabled and self._use_unified_data:
            ready = True
            health_reason = None
            if callable(health_checker):
                try:
                    ready, health_reason = await health_checker(self.name)
                except Exception:
                    ready = False
                    health_reason = "market_data_health_check_failed"
            if not ready:
                if self._subscription_wait_state_changed(
                    "awaiting_market_data_ready", health_reason
                ):
                    self._telemetry_set_phase_status(
                        self._PHASE_SUBSCRIPTION,
                        status="waiting",
                        status_code="awaiting_market_data_ready",
                        status_reason="Waiting for market data service readiness",
                        status_cause=health_reason or "Market data service not ready",
                        status_cause_code="awaiting_market_data_ready",
                    )
                    self._telemetry_log(
                        "Awaiting market data service readiness",
                        level="WARN",
                        tone="warning",
                        phase=self._PHASE_SUBSCRIPTION,
                        deduplicate=False,
                        details={"health_reason": health_reason},
                    )
                await asyncio.sleep(wait_interval)
                continue

            registered = True
            if callable(registrar) and self.symbol:
                try:
                    registered = await registrar(
                        self.name,
                        symbol=self.symbol,
                        timeframe=self.interval,
                        channel=self._resolve_unified_event_channel(),
                        metadata={
                            "strategy": self.name,
                            "symbol": self.symbol,
                            "interval": self.interval,
                        },
                    )
                except Exception:
                    registered = False
            if not registered and callable(registration_checker):
                try:
                    registered = await registration_checker(self.name)
                except Exception:
                    registered = False
            if not registered:
                if self._subscription_wait_state_changed(
                    "awaiting_registration", None
                ):
                    self._telemetry_set_phase_status(
                        self._PHASE_SUBSCRIPTION,
                        status="waiting",
                        status_code="awaiting_registration",
                        status_reason="Waiting for subscription registration",
                        status_cause="Awaiting coordinator registration",
                        status_cause_code="awaiting_registration",
                    )
                    self._telemetry_log(
                        "Awaiting coordinator subscription registration",
                        level="WARN",
                        tone="warning",
                        phase=self._PHASE_SUBSCRIPTION,
                        deduplicate=False,
                    )
                await asyncio.sleep(wait_interval)
                continue

            break

        if not (self.active and self.enabled and self._use_unified_data):
            return
        self._start_unified_subscription()

    def _start_unified_subscription(self) -> bool:
        try:
            self._telemetry_set_phase_status(
                self._PHASE_SUBSCRIPTION,
                status="connecting",
                status_code="initialising",
            )
            if self._health_check_enabled:
                self._schedule_coroutine(self._schedule_periodic_health_check())
            self._ensure_unified_subscription()
            self._cancel_retry_thread()
            if self._history_backfill_failed:
                warning_message = "Historical backfill unavailable; continuing with live subscription"
                self._telemetry_set_phase_status(
                    self._PHASE_SUBSCRIPTION,
                    status="warning",
                    status_code="historical_backfill_unavailable",
                    status_reason=warning_message,
                    status_cause=warning_message,
                    status_cause_code="historical_backfill_unavailable",
                )
                self._telemetry_log(
                    warning_message,
                    level="WARN",
                    tone="warning",
                )
            else:
                self._telemetry_log(
                    "Unified candle subscription established",
                    level="INFO",
                    tone="neutral",
                )
            return True
        except DataSourceError as exc:
            if self._is_connection_unavailable_error(exc):
                self.logger.warning(
                    "Failed to initialise candle subscription because IBKR is not connected; will retry automatically (symbol=%s interval=%s)",
                    self.symbol,
                    self.interval,
                )
                self._telemetry_set_phase_status(
                    self._PHASE_SUBSCRIPTION,
                    status="waiting",
                    status_code="awaiting_connection",
                    status_reason="Awaiting IBKR connection",
                    status_cause="Unified candle subscription awaiting IBKR connection",
                    status_cause_code="subscription_awaiting_connection",
                )
                self._telemetry_log(
                    "Unified candle subscription awaiting IBKR connection",
                    level="WARN",
                    tone="warning",
                )
                self._start_retry_thread()
                return True
            self.logger.error("Failed to initialise candle subscription: %s", exc)
            failure_message = (
                "Historical backfill failed"
                if self._history_backfill_failed
                else f"Unified candle subscription failed: {exc}"
            )
            self._telemetry_set_phase_status(
                self._PHASE_SUBSCRIPTION,
                status="failed",
                status_code="error",
                status_reason=(
                    "Historical backfill failed"
                    if self._history_backfill_failed
                    else "Unified candle subscription failed"
                ),
                status_cause=failure_message,
                status_cause_code="subscription_failed",
            )
            self._telemetry_log(
                failure_message,
                level="ERROR",
                tone="negative",
            )
            super().stop()
            self._telemetry_stop_session("Unified candle subscription failed")
            return False
        except Exception as exc:
            self.logger.exception("Failed to initialise candle subscription: %s", exc)
            failure_message = "Unexpected error during unified candle subscription initialisation"
            self._telemetry_set_phase_status(
                self._PHASE_SUBSCRIPTION,
                status="failed",
                status_code="error",
                status_reason=failure_message,
                status_cause=failure_message,
                status_cause_code="subscription_error",
            )
            self._telemetry_log(
                failure_message,
                level="ERROR",
                tone="negative",
            )
            super().stop()
            self._telemetry_stop_session(
                "Unified candle subscription failed during initialisation"
            )
            return False

    async def _recover_from_bar_inactivity(
        self,
        *,
        inactive_seconds: float,
        threshold_seconds: float,
        refresher: Callable[[str], Awaitable[bool]] | None,
    ) -> bool:
        if self._inactivity_recovery_inflight:
            return True
        facade = self._coordinator_facade()
        now = datetime.now(timezone.utc)
        now_monotonic = self._monotonic_now()
        if now_monotonic < self._inactivity_recovery_next_attempt:
            self._telemetry_update_phase_metrics(
                self._PHASE_SUBSCRIPTION,
                inactive_seconds=round(inactive_seconds, 1),
                inactivity_threshold_seconds=threshold_seconds,
                last_bar_at=self._telemetry_format_value(self._last_bar_received_at),
                next_recovery_in_seconds=round(
                    max(0.0, self._inactivity_recovery_next_attempt - now_monotonic), 1
                ),
            )
            return False
        self._inactivity_recovery_inflight = True
        self._recovering_after_inactivity = True
        details = {
            "inactive_seconds": round(inactive_seconds, 1),
            "threshold_seconds": round(threshold_seconds, 1),
            "last_bar_at": self._telemetry_format_value(self._last_bar_received_at),
        }
        self._telemetry_set_phase_status(
            self._PHASE_SUBSCRIPTION,
            status="recovering",
            status_code="bar_stream_recovering",
            status_reason="Recovering after inactivity",
            status_details=details,
        )
        self._telemetry_set_phase_status(
            self._PHASE_SIGNALS,
            status="waiting",
            status_code="awaiting_data",
            status_reason="等待K线数据恢复",
            status_details=details,
            timestamp=now,
        )
        self._telemetry_log_phase_detail(
            phase=self._PHASE_SIGNALS,
            message="等待K线数据恢复",
            level="WARN",
            tone="warning",
            details=details,
            timestamp=now,
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_SUBSCRIPTION,
            stream_status="recovering_after_inactivity",
            inactive_seconds=details["inactive_seconds"],
            inactivity_threshold_seconds=details["threshold_seconds"],
            last_bar_at=details["last_bar_at"],
        )
        self._telemetry_log(
            "Bar stream inactivity detected; attempting recovery",
            level="WARN",
            tone="warning",
            phase=self._PHASE_SUBSCRIPTION,
            deduplicate=False,
            details=details,
            timestamp=now,
        )
        refreshed_ok = False
        refresh_reason = None
        health_fetcher = getattr(facade, "get_subscription_health", None)
        if callable(health_fetcher):
            try:
                health = await health_fetcher(self.name)
            except Exception:
                health = None
            if isinstance(health, Mapping):
                refresh_reason = health.get("last_failure_reason")
        try:
            if callable(refresher) and refresh_reason != "strategy_not_registered":
                try:
                    self._bar_stream_refresh_inflight = True
                    refreshed = await refresher(self.name)
                    refreshed_ok = bool(refreshed)
                except Exception:
                    self.logger.exception(
                        "Coordinator refresh failed during bar inactivity recovery"
                    )
                    self._telemetry_log(
                        "Coordinator refresh failed during bar inactivity recovery",
                        level="ERROR",
                        tone="negative",
                        phase=self._PHASE_SUBSCRIPTION,
                        deduplicate=False,
                        details=details,
                        timestamp=now,
                    )
                else:
                    refresh_reason = None
                    if not refreshed:
                        health_fetcher = getattr(facade, "get_subscription_health", None)
                        if callable(health_fetcher):
                            try:
                                health = await health_fetcher(self.name)
                            except Exception:
                                health = None
                            if isinstance(health, Mapping):
                                refresh_reason = health.get("last_failure_reason")
                    detail_payload = dict(details)
                    detail_payload["refresh_accepted"] = bool(refreshed)
                    if refresh_reason:
                        detail_payload["refresh_reason"] = refresh_reason
                    self._telemetry_log(
                        "Coordinator refresh requested after bar inactivity",
                        level="INFO" if refreshed else "WARN",
                        tone="neutral" if refreshed else "warning",
                        phase=self._PHASE_SUBSCRIPTION,
                        deduplicate=False,
                        details=detail_payload,
                        timestamp=now,
                    )
                finally:
                    self._bar_stream_refresh_inflight = False
            elif refresh_reason == "strategy_not_registered":
                detail_payload = dict(details)
                detail_payload["refresh_reason"] = refresh_reason
                self._telemetry_log(
                    "Coordinator refresh skipped; strategy not registered",
                    level="WARN",
                    tone="warning",
                    phase=self._PHASE_SUBSCRIPTION,
                    deduplicate=False,
                    details=detail_payload,
                    timestamp=now,
                )

            if self._use_unified_data:
                if refreshed_ok and self._data_layer_subscription is not None:
                    self._telemetry_log(
                        "Unified bar stream refresh requested; restarting unified subscription due to inactivity",
                        level="INFO",
                        tone="neutral",
                        phase=self._PHASE_SUBSCRIPTION,
                        deduplicate=False,
                        details=details,
                    )
                try:
                    self._teardown_unified_subscription()
                except Exception:
                    self.logger.exception(
                        "Failed to teardown unified subscription during inactivity recovery"
                    )
                try:
                    self._ensure_unified_subscription()
                except Exception:
                    self.logger.exception(
                        "Failed to re-establish unified subscription after inactivity"
                    )
                    self._telemetry_log(
                        "Failed to re-establish unified candle subscription after inactivity",
                        level="ERROR",
                        tone="negative",
                        phase=self._PHASE_SUBSCRIPTION,
                        deduplicate=False,
                        details=details,
                        timestamp=now,
                    )
            else:
                self._restart_bar_listener()
        finally:
            self._inactivity_recovery_inflight = False
            self._inactivity_recovery_backoff = min(
                300.0, max(30.0, self._inactivity_recovery_backoff * 2.0)
            )
            self._inactivity_recovery_next_attempt = now_monotonic + self._inactivity_recovery_backoff
        return True

    async def _check_bar_stream_health(self) -> None:
        facade = self._coordinator_facade()
        checker = getattr(facade, "ensure_stream_active", None) if facade else None
        refresher = getattr(facade, "refresh_subscription", None) if facade else None
        now = datetime.now(timezone.utc)
        inactive_seconds: float | None = None
        if self._last_bar_received_at is not None:
            inactive_seconds = max(
                0.0, (now - self._last_bar_received_at).total_seconds()
            )
        threshold_seconds = self._bar_inactivity_threshold_seconds()
        data_recent = (
            inactive_seconds is not None and inactive_seconds < threshold_seconds
        )
        if data_recent:
            if self._bar_stream_missing or self._bar_stream_refresh_inflight:
                self._bar_stream_missing = False
                self._bar_stream_refresh_inflight = False
                self._inactivity_recovery_next_attempt = 0.0
                self._inactivity_recovery_backoff = 30.0
                self._telemetry_set_phase_status(
                    self._PHASE_SUBSCRIPTION,
                    status="connected",
                    status_code="subscribed",
                )
                self._telemetry_update_phase_metrics(
                    self._PHASE_SUBSCRIPTION,
                    stream_status="active",
                    missing_stream=None,
                )
                self._telemetry_log(
                    "Unified bar stream restored",
                    level="INFO",
                    tone="positive",
                    phase=self._PHASE_SUBSCRIPTION,
                    deduplicate=False,
                    timestamp=now,
                )
            return
        is_active: bool | None = None
        if checker is not None and callable(checker):
            try:
                is_active = await checker(self.name, "bar")
            except Exception:
                self.logger.exception(
                    "Failed to verify unified bar stream health via coordinator"
                )
                is_active = None
        recovery_triggered = False
        if inactive_seconds is not None and inactive_seconds >= threshold_seconds:
            recovery_triggered = await self._recover_from_bar_inactivity(
                inactive_seconds=inactive_seconds,
                threshold_seconds=threshold_seconds,
                refresher=refresher if callable(refresher) else None,
            )
            if self._recovering_after_inactivity:
                return
        if is_active:
            if self._bar_stream_missing or self._bar_stream_refresh_inflight:
                self._bar_stream_missing = False
                self._bar_stream_refresh_inflight = False
                self._inactivity_recovery_next_attempt = 0.0
                self._inactivity_recovery_backoff = 30.0
                self._telemetry_set_phase_status(
                    self._PHASE_SUBSCRIPTION,
                    status="connected",
                    status_code="subscribed",
                )
                self._telemetry_update_phase_metrics(
                    self._PHASE_SUBSCRIPTION,
                    stream_status="active",
                    missing_stream=None,
                )
                self._telemetry_log(
                    "Unified bar stream restored",
                    level="INFO",
                    tone="positive",
                    phase=self._PHASE_SUBSCRIPTION,
                    deduplicate=False,
                    timestamp=now,
                )
            return
        if is_active is None:
            if inactive_seconds is not None:
                self._telemetry_update_phase_metrics(
                    self._PHASE_SUBSCRIPTION,
                    inactive_seconds=round(inactive_seconds, 1),
                    inactivity_threshold_seconds=threshold_seconds,
                    last_bar_at=self._telemetry_format_value(self._last_bar_received_at),
                )
            return
        if recovery_triggered:
            return
        if not self._bar_stream_missing:
            self._telemetry_set_phase_status(
                self._PHASE_SUBSCRIPTION,
                status="waiting",
                status_code="awaiting_bar_stream",
                status_details={"stream": "bar"},
            )
            self._telemetry_update_phase_metrics(
                self._PHASE_SUBSCRIPTION,
                stream_status="missing",
                missing_stream="bar",
            )
            self._telemetry_log(
                "Unified bar stream inactive; awaiting coordinator refresh",
                level="WARN",
                tone="warning",
                phase=self._PHASE_SUBSCRIPTION,
                deduplicate=False,
                timestamp=now,
            )
        self._bar_stream_missing = True
        refresh_reason = None
        health_fetcher = getattr(facade, "get_subscription_health", None)
        if callable(health_fetcher):
            try:
                health = await health_fetcher(self.name)
            except Exception:
                health = None
            if isinstance(health, Mapping):
                refresh_reason = health.get("last_failure_reason")
        if (
            callable(refresher)
            and refresh_reason != "strategy_not_registered"
            and not self._bar_stream_refresh_inflight
            and self._monotonic_now() >= self._inactivity_recovery_next_attempt
        ):
            self._bar_stream_refresh_inflight = True
            refreshed = False
            try:
                refreshed = await refresher(self.name)
            except Exception:
                self.logger.exception(
                    "Failed to request unified bar stream refresh via coordinator"
                )
                self._telemetry_log(
                    "Unified bar stream refresh request failed",
                    level="ERROR",
                    tone="negative",
                    phase=self._PHASE_SUBSCRIPTION,
                    deduplicate=False,
                    timestamp=now,
                )
            else:
                if refreshed:
                    self._telemetry_log(
                        "Requested unified bar stream refresh via coordinator",
                        level="INFO",
                        tone="neutral",
                        phase=self._PHASE_SUBSCRIPTION,
                        deduplicate=False,
                        timestamp=now,
                    )
                else:
                    if not refresh_reason:
                        health_fetcher = getattr(facade, "get_subscription_health", None)
                        if callable(health_fetcher):
                            try:
                                health = await health_fetcher(self.name)
                            except Exception:
                                health = None
                            if isinstance(health, Mapping):
                                refresh_reason = health.get("last_failure_reason")
                    if not refresh_reason:
                        refresh_reason = "refresh_rejected"
                    self._telemetry_log(
                        "Unified bar stream refresh request rejected",
                        level="ERROR",
                        tone="negative",
                        phase=self._PHASE_SUBSCRIPTION,
                        deduplicate=False,
                        details=(
                            {"refresh_reason": refresh_reason}
                            if refresh_reason
                            else None
                        ),
                        timestamp=now,
                    )
            finally:
                self._bar_stream_refresh_inflight = False
                self._inactivity_recovery_backoff = min(
                    300.0, max(30.0, self._inactivity_recovery_backoff * 2.0)
                )
                self._inactivity_recovery_next_attempt = (
                    self._monotonic_now() + self._inactivity_recovery_backoff
                )
        elif refresh_reason == "strategy_not_registered":
            self._telemetry_log(
                "Unified bar stream refresh skipped; strategy not registered",
                level="WARN",
                tone="warning",
                phase=self._PHASE_SUBSCRIPTION,
                deduplicate=False,
                details={"refresh_reason": refresh_reason},
                timestamp=now,
            )

    def _telemetry_record_data_event(
        self, timestamp: datetime | None, volume: float | int | None
    ) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        event_ts = self._maybe_parse_timestamp(timestamp)
        try:
            normalised = telemetry.record_data_event(
                self._telemetry_strategy_id(),
                timestamp=event_ts,
                subscription_id=self.interval,
                symbol=self.symbol or None,
            )
        except KeyError:
            return
        except Exception:  # pragma: no cover - defensive
            self.logger.exception("Failed to record candle telemetry data event")
            return
        seen_at: datetime | None
        if isinstance(normalised, datetime):
            seen_at = normalised
        else:
            seen_at = datetime.now(timezone.utc)
        if event_ts is None:
            event_ts = seen_at
        self._last_bar_received_at = seen_at
        self._tick_count += 1
        self._last_tick_timestamp = seen_at
        self._current_candle_volume = float(volume or 0.0)
        last_bar_value = self._telemetry_format_value(event_ts)
        self._telemetry_update_phase_metrics(
            self._PHASE_SUBSCRIPTION,
            last_bar_at=last_bar_value,
            stream_status="active" if not self._recovering_after_inactivity else "recovering_after_inactivity",
        )
        if self._recovering_after_inactivity:
            self._recovering_after_inactivity = False
            self._bar_stream_missing = False
            self._bar_stream_refresh_inflight = False
            self._telemetry_set_phase_status(
                self._PHASE_SUBSCRIPTION,
                status="connected",
                status_code="subscribed",
                status_reason="Bar stream recovered after inactivity",
                status_details={"last_bar_at": last_bar_value},
                timestamp=seen_at,
            )
            self._telemetry_log(
                "Bar stream recovered after inactivity",
                level="INFO",
                tone="positive",
                phase=self._PHASE_SUBSCRIPTION,
                deduplicate=False,
                details={"last_bar_at": last_bar_value},
            )
        self._telemetry_set_phase_status(
            self._PHASE_AGGREGATION,
            status="receiving",
            status_code="active",
            timestamp=seen_at,
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_AGGREGATION,
            tick_count=self._tick_count,
            last_tick_at=seen_at,
            last_volume=self._current_candle_volume,
        )

    def _telemetry_record_closed_candle(
        self,
        candle: Mapping[str, Any],
        *,
        timestamp: datetime | None = None,
    ) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        ts = timestamp or self._maybe_parse_timestamp(
            candle.get("end") or candle.get("close_time")
        )
        volume = _coerce_float(candle.get("volume"), default=0.0)
        close_price = _coerce_float(candle.get("close"), default=0.0)
        self._closed_candle_count += 1
        self._last_closed_timestamp = ts
        self._last_closed_volume = volume
        self._last_closed_price = close_price
        self._telemetry_set_phase_status(
            self._PHASE_DISPATCH,
            status="dispatching",
            status_code="active",
            timestamp=ts,
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_DISPATCH,
            candle_count=self._closed_candle_count,
            last_candle_end=ts,
            last_volume=volume,
            last_close=close_price,
        )

    def _telemetry_record_unified_candle(
        self, candle: Mapping[str, Any]
    ) -> None:
        timestamp = self._maybe_parse_timestamp(
            candle.get("end") or candle.get("close_time") or candle.get("start")
        )
        self._telemetry_record_data_event(timestamp, candle.get("volume"))
        self._telemetry_record_closed_candle(candle, timestamp=timestamp)

    @staticmethod
    def _telemetry_format_value(value: Any) -> Any:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            else:
                value = value.astimezone(timezone.utc)
            return value.isoformat()
        return value

    @staticmethod
    def _maybe_parse_timestamp(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        try:
            return _parse_timestamp(value)
        except Exception:  # pragma: no cover - defensive
            return None

    # ------------------------------------------------------------------
    def _run_coro(self, coro: Awaitable[Any]) -> Any:
        try:
            loop = asyncio.get_running_loop()
            self._loop = loop
        except RuntimeError:
            loop = self._loop

        if loop and loop.is_running():
            shared_loop = getattr(self, "background_event_loop", None)
            target_loop: asyncio.AbstractEventLoop | None = None
            if shared_loop is not None and hasattr(shared_loop, "is_running") and shared_loop.is_running():
                target_loop = shared_loop
            elif self._coro_event_loop is not None and not self._coro_event_loop.is_closed():
                target_loop = self._coro_event_loop
            if target_loop is None:
                target_loop = asyncio.new_event_loop()
                self._coro_event_loop = target_loop
                def _run_loop(target: asyncio.AbstractEventLoop) -> None:
                    asyncio.set_event_loop(target)
                    target.run_forever()
                thread = threading.Thread(
                    target=_run_loop,
                    name=f"{self.name}-candle-coro-loop",
                    args=(target_loop,),
                    daemon=True,
                )
                thread.start()
                self._coro_event_loop_thread = thread
            future = asyncio.run_coroutine_threadsafe(coro, target_loop)  # type: ignore[arg-type]
            return future.result()

        return asyncio.run(coro)

CandleStrategy = CandleSubscriptionStrategy
