"""Candle helpers for market data driven strategies."""

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
    Dict,
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
from src.common.market_data.history import HistoryReplayConfig
from src.common.market_data.history_chunks import load_history_with_backoff
from src.common.symbols import build_symbol_aliases
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
            try:
                seconds = float(text)
            except Exception:
                ts = datetime.now(timezone.utc)
            else:
                if abs(seconds) > 1_000_000_000_000:
                    seconds /= 1000.0
                ts = datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        try:
            seconds = float(value)
            if abs(seconds) > 1_000_000_000_000:
                seconds /= 1000.0
            ts = datetime.fromtimestamp(seconds, tz=timezone.utc)
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


def _is_transient_market_data_health_reason(reason: str | None) -> bool:
    token = str(reason or "").strip().lower()
    return token in {
        "market_data_health_check_failed",
        "market_data_health_request_failed",
        "market_data_unknown",
        "market_data_streams_degraded",
    }


@dataclass
class CandleSubscriptionStrategy(BaseStrategy):
    """Base strategy that consumes closed candles from market data."""

    strategy_type: ClassVar[str] = "CandleSubscriptionStrategy"
    is_kline_strategy: ClassVar[bool] = True
    data_feed_mode: ClassVar[str] = "kline"
    use_base_exit: ClassVar[bool] = False
    _PHASE_SUBSCRIPTION: ClassVar[str] = "subscription"
    _PHASE_AGGREGATION: ClassVar[str] = "aggregation"
    _PHASE_DISPATCH: ClassVar[str] = "dispatch"
    _PHASE_SIGNALS: ClassVar[str] = "signal_generation"
    _PHASE_EXECUTION: ClassVar[str] = "execution"
    symbol: str = ""
    interval: str = "1m"
    subscription_interval: str | None = None
    intervals: list[str] = field(default_factory=list)
    dispatch_history_candles: bool = True
    future_bar_guard_seconds: float = 2.0
    allow_subscription_snapshots: bool = True
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
    max_loss_streak: int = 10
    health_check_interval: float = 30.0

    _pubsub: PubSubChannel | None = field(default=None, init=False, repr=False)
    _dispatch_event: _Dispatcher | None = field(default=None, init=False, repr=False)
    _history_loader: _HistoryLoader | None = field(default=None, init=False, repr=False)
    _listener_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False
    )
    _candles: dict[str, Deque[Mapping[str, Any]]] = field(default_factory=dict, init=False, repr=False)
    _multi_aggregators: dict[str, MinuteBarAggregator] = field(default_factory=dict, init=False, repr=False)
    _current_candle: dict[str, MutableMapping[str, Any] | None] = field(
        default_factory=dict, init=False, repr=False
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
    _data_layer_subscriptions: list[DataSubscriptionToken] = field(
        default_factory=list, init=False, repr=False
    )
    _event_bus_token: BusSubscriptionToken | None = field(
        default=None, init=False, repr=False
    )
    _event_bus_tokens: list[BusSubscriptionToken] = field(
        default_factory=list, init=False, repr=False
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
    _signals_generated: int = field(default=0, init=False, repr=False)
    _buy_signals_generated: int = field(default=0, init=False, repr=False)
    _sell_signals_generated: int = field(default=0, init=False, repr=False)
    _risk_evaluation_count: int = field(default=0, init=False, repr=False)
    _risk_blocked_count: int = field(default=0, init=False, repr=False)
    _local_order_block_count: int = field(default=0, init=False, repr=False)
    _orders_submitted_count: int = field(default=0, init=False, repr=False)
    _orders_rejected_count: int = field(default=0, init=False, repr=False)
    _orders_failed_count: int = field(default=0, init=False, repr=False)
    _orders_delegated_count: int = field(default=0, init=False, repr=False)
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
    _last_processed_candle_start_by_interval: dict[str, datetime | None] = field(
        default_factory=dict, init=False, repr=False
    )
    _last_processed_candle_end_by_interval: dict[str, datetime | None] = field(
        default_factory=dict, init=False, repr=False
    )
    _unified_event_channel: str | None = field(default=None, init=False, repr=False)
    _unified_event_channels: list[str] = field(
        default_factory=list, init=False, repr=False
    )
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
    _initial_backfill_requested: bool = field(default=False, init=False, repr=False)
    _history_replay_in_progress: bool = field(default=False, init=False, repr=False)
    _last_bar_received_at: datetime | None = field(default=None, init=False, repr=False)
    _last_bar_received_at_by_interval: dict[str, datetime] = field(
        default_factory=dict, init=False, repr=False
    )
    _last_bar_skip_log_at: datetime | None = field(default=None, init=False, repr=False)
    _last_closed_candle_log_at: datetime | None = field(default=None, init=False, repr=False)
    _closed_candle_counts: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _recovering_after_inactivity: bool = field(default=False, init=False, repr=False)
    _inactivity_recovery_inflight: bool = field(default=False, init=False, repr=False)
    _inactivity_recovery_next_attempt: float = field(default=0.0, init=False, repr=False)
    _inactivity_recovery_backoff: float = field(default=30.0, init=False, repr=False)
    _suppress_order_queue: bool = field(default=False, init=False, repr=False)
    _symbol_aliases: set[str] = field(default_factory=set, init=False, repr=False)
    allow_history_replay_orders: bool = field(default=False, init=False, repr=False)

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
        self._symbol_aliases = set(build_symbol_aliases(self.symbol))
        
        # Normalize self.interval first
        interval_hint = (
            self.interval
            or getattr(self, "timeframe", None)
            or getattr(self, "bar_timeframe", None)
        )
        normalised_interval = _normalize_interval_token(interval_hint) or "1m"
        self.interval = normalised_interval
        if self.subscription_interval:
            normalized_subscription = _normalize_interval_token(
                self.subscription_interval
            )
            self.subscription_interval = (
                normalized_subscription if normalized_subscription else None
            )

        # Ensure intervals list is populated
        if not self.intervals:
            self.intervals = [self.interval]
        elif self.interval not in self.intervals:
            if self.interval:
                 self.intervals.append(self.interval)
        
        # Normalize all intervals in the list
        self.intervals = [
            _normalize_interval_token(i) or "1m" for i in self.intervals
        ]
        # De-duplicate
        self.intervals = list(dict.fromkeys(self.intervals))
        
        try:
            self._interval_delta = _interval_to_delta(self.interval)
        except ValueError:
            self._interval_delta = _interval_to_delta("1m")
            self.interval = "1m"
            
        # Initialize storage
        self._candles = {}
        self._current_candle = {}
        self._candles_lock = RLock()
        self._pending_orders = deque()
        self._pending_orders_lock = RLock()
        self._subscription_retry_stop = Event()
        limit = max(1, int(self.history_limit))
        for interval in self.intervals:
             self._candles[interval] = deque(maxlen=limit)
             self._current_candle[interval] = None
             self._last_processed_candle_start_by_interval[interval] = None
             self._last_processed_candle_end_by_interval[interval] = None

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


    def get_candles(self, interval: str | None = None) -> Deque[Mapping[str, Any]]:
        """Return the deque of candles for the specified interval."""
        target = interval or self.interval
        if target not in self._candles:
            # Fallback or initialization safety
            self._candles[target] = deque(maxlen=max(1, int(self.history_limit)))
        return self._candles[target]

    def _channel_interval_token(self, interval: str) -> str:
        token = _normalize_interval_token(interval) or interval
        if token.endswith("mo") and token[:-2].isdigit():
            return f"{token[:-2]}M"
        return token

    def _resolve_bar_source_interval(self, interval: str) -> str:
        token = _normalize_interval_token(interval) or interval
        return token

    def _resolve_subscription_intervals(self) -> list[str]:
        source_intervals: list[str] = []
        for interval in self.intervals or [self.interval]:
            token = self._resolve_bar_source_interval(interval)
            if token not in source_intervals:
                source_intervals.append(token)
        return source_intervals

    def _force_runtime_intervals(
        self,
        *,
        interval: str,
        intervals: Sequence[str] | None = None,
    ) -> None:
        """Override persisted interval drift with strategy-owned runtime intervals."""

        normalized_interval = _normalize_interval_token(interval) or "1m"
        normalized_intervals = [
            _normalize_interval_token(item) or normalized_interval
            for item in (intervals or [normalized_interval])
        ]
        if normalized_interval not in normalized_intervals:
            normalized_intervals.insert(0, normalized_interval)
        self.interval = normalized_interval
        self.intervals = list(dict.fromkeys(normalized_intervals))
        try:
            self._interval_delta = _interval_to_delta(self.interval)
        except ValueError:
            self.interval = "1m"
            self._interval_delta = _interval_to_delta(self.interval)
        self._reset_unified_bucket()
        self._last_processed_candle_start = None
        self._last_processed_candle_end = None
        self._refresh_interval_state()

    def _aggregate_history_records(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        target_interval: str,
        source_interval: str,
    ) -> list[Mapping[str, Any]]:
        source_seconds = _interval_seconds(source_interval)
        if source_seconds is None or source_seconds <= 0:
            return []
        aggregator = MinuteBarAggregator(
            target_interval,
            symbol=self.symbol or None,
            interval_label=target_interval,
            source_interval_seconds=source_seconds,
        )
        closed: list[Mapping[str, Any]] = []
        for record in records:
            buckets = aggregator.push(record, close_hint=True)
            if not buckets:
                continue
            for bucket in buckets:
                bucket["interval"] = target_interval
                bucket["is_closed"] = True
                if self.symbol:
                    bucket["symbol"] = self.symbol
                closed.append(dict(bucket))
        return closed

    def _resolve_channel(self, base: str, interval: str | None = None) -> str:
        name = base
        if self.redis_channel_prefix:
            name = f"{self.redis_channel_prefix}{name}"
        
        # If this is a bar channel and we have a specific interval, append it
        if interval:
            channel = name.rstrip(".")
            interval_token = self._channel_interval_token(interval)
            if self.symbol:
                return f"{channel}-{self.symbol}-{interval_token}"
            return f"{channel}-*-{interval_token}"
                 
        if self.symbol:
            name = f"{name}-{self.symbol}"
        return name

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
        if "intervals" in updates:
            updates = dict(updates)
            updates.pop("intervals", None)
        if not updates:
            return {}
        previous_symbol = self.symbol
        previous_interval = self.interval
        previous_interval_token = _normalize_interval_token(previous_interval) or previous_interval
        applied = super().apply_parameter_updates(updates)
        interval_changed = "interval" in applied and self.interval != previous_interval
        history_changed = "history_limit" in applied
        if interval_changed:
            normalised = _normalize_interval_token(self.interval) or "1m"
            self.interval = normalised
            if (
                len(self.intervals) == 1
                and (_normalize_interval_token(self.intervals[0]) or self.intervals[0])
                == previous_interval_token
            ):
                self.intervals = [self.interval]
            self.intervals = list(
                dict.fromkeys(
                    [_normalize_interval_token(item) or item for item in self.intervals]
                )
            )
            try:
                self._interval_delta = _interval_to_delta(self.interval)
            except ValueError:
                self._interval_delta = _interval_to_delta("1m")
                self.interval = "1m"
            self._reset_unified_bucket()
            self._last_processed_candle_start = None
            self._last_processed_candle_end = None
            self._last_processed_candle_start_by_interval = {
                interval: None for interval in self.intervals
            }
            self._last_processed_candle_end_by_interval = {
                interval: None for interval in self.intervals
            }
            with self._candles_lock:
                limit = max(1, int(self.history_limit))
                for interval in self.intervals:
                    if interval not in self._candles:
                        self._candles[interval] = deque(maxlen=limit)
                    if interval not in self._current_candle:
                        self._current_candle[interval] = None
                for interval in list(self._candles.keys()):
                    if interval not in self.intervals:
                        self._candles.pop(interval, None)
                        self._current_candle.pop(interval, None)
                        self._last_processed_candle_start_by_interval.pop(interval, None)
                        self._last_processed_candle_end_by_interval.pop(interval, None)
        if history_changed:
            try:
                limit = max(1, int(self.history_limit))
            except Exception:
                limit = 1
            with self._candles_lock:
                for interval, history in self._candles.items():
                    self._candles[interval] = deque(history, maxlen=limit)
        symbol_changed = "symbol" in applied and self.symbol != previous_symbol
        if symbol_changed:
            normalized_symbol = (self.symbol or "").strip().upper()
            self.symbol = normalized_symbol
            self._symbol_aliases = set(build_symbol_aliases(normalized_symbol))
        if (symbol_changed or interval_changed) and self.active and self._use_unified_data:
            self._schedule_coroutine(self._resubscribe_unified(reason="parameter_change"))
        return applied

    def _refresh_interval_state(self) -> None:
        self.intervals = list(
            dict.fromkeys(
                [_normalize_interval_token(item) or item for item in self.intervals]
            )
        )
        self._last_processed_candle_start_by_interval = {
            interval: None for interval in self.intervals
        }
        self._last_processed_candle_end_by_interval = {
            interval: None for interval in self.intervals
        }
        with self._candles_lock:
            limit = max(1, int(self.history_limit))
            for interval in self.intervals:
                if interval not in self._candles:
                    self._candles[interval] = deque(maxlen=limit)
                if interval not in self._current_candle:
                    self._current_candle[interval] = None
            for interval in list(self._candles.keys()):
                if interval not in self.intervals:
                    self._candles.pop(interval, None)
                    self._current_candle.pop(interval, None)
                    self._last_processed_candle_start_by_interval.pop(interval, None)
                    self._last_processed_candle_end_by_interval.pop(interval, None)

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
            "intervals": {
                "type": "list",
                "default": list(self.intervals),
                "description": "Subscribed bar intervals (read-only).",
                "readonly": True,
            },
            "history_limit": {
                "type": "int",
                "default": int(self.history_limit),
                "min": 1,
                "description": "Number of historical candles to retain.",
            },
            "dispatch_history_candles": {
                "type": "bool",
                "default": bool(self.dispatch_history_candles),
                "description": "Whether to dispatch historical candles on startup replay.",
            },
            "allow_subscription_snapshots": {
                "type": "bool",
                "default": bool(self.allow_subscription_snapshots),
                "description": "Whether to accept subscription snapshot payloads.",
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
        self._current_candle = {}
        self._last_event_ts = None
        self._last_processed_candle_start_by_interval = {
            interval: None for interval in self.intervals
        }
        self._last_processed_candle_end_by_interval = {
            interval: None for interval in self.intervals
        }
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
            buy_signals=0,
            sell_signals=0,
            last_signal_side=None,
        )
        self._telemetry_set_phase_status(
            self._PHASE_EXECUTION,
            status="idle",
            status_code="awaiting_signal",
            status_reason="Awaiting generated orders for risk evaluation",
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_EXECUTION,
            risk_evaluation_count=0,
            risk_blocked_count=0,
            local_blocked_count=0,
            orders_submitted_count=0,
            orders_rejected_count=0,
            orders_failed_count=0,
            orders_delegated_count=0,
            last_risk_reason=None,
            last_order_status=None,
            last_order_reason=None,
            last_order_at=None,
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
            manager = getattr(self, "strategy_manager", None)
            dependencies = getattr(manager, "_dependencies", None)
            if isinstance(dependencies, Mapping):
                updates: dict[str, Any] = {}
                for key in (
                    "pubsub",
                    "event_dispatcher",
                    "candle_history_loader",
                    "data_layer_manager",
                    "background_event_loop",
                    "market_data_subscription_health",
                ):
                    value = dependencies.get(key)
                    if value is not None:
                        updates[key] = value
                if updates:
                    self.set_dependencies(**updates)
        
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
                self._schedule_coroutine(self._teardown_unified_subscription())
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
    def _clear_listener_task(self, task: asyncio.Task[None] | None) -> None:
        if self._listener_task is task:
            self._listener_task = None

    # ------------------------------------------------------------------
    async def _run_listener(self) -> None:
        self.logger.debug(f"DEBUG: _run_listener STARTED for {self.name} {self.symbol} {self.intervals}")
        assert self._pubsub is not None
        listener_task = asyncio.current_task()
        
        # History loader logic needs to handle multiple intervals if applicable
        # Current logic only loads for self.interval
        if self._history_loader is not None:
            try:
                self._history_replay_in_progress = True
                history_by_interval: dict[str, list[Mapping[str, Any]]] = {}
                for interval in self._resolve_subscription_intervals():
                    history = await self._history_loader(
                        self.symbol, interval, self.history_limit
                    )
                    history_by_interval[interval] = list(history or [])

                for interval in self.intervals:
                    source_interval = self._resolve_bar_source_interval(interval)
                    source_records = history_by_interval.get(source_interval, [])
                    if not source_records:
                        continue
                    if source_interval == interval:
                        candidates = source_records
                    else:
                        candidates = self._aggregate_history_records(
                            source_records,
                            target_interval=interval,
                            source_interval=source_interval,
                        )
                    for candle in candidates[-self.history_limit :]:
                        normalised = self._normalise_candle(
                            candle, is_closed=True, interval_label=interval
                        )
                        if normalised is not None:
                            with self._candles_lock:
                                self._candles[interval].append(normalised)
                            await self._handle_history_snapshot(normalised)
            except Exception:  # pragma: no cover - defensive load failure
                self.logger.exception("Failed to load candle history for %s", self.symbol)
            finally:
                self._history_replay_in_progress = False

        backoff = 1.0
        try:
            while self.active:
                try:
                    channels: list[str] = []
                    if self.ticker_channel:
                        channels.append(self._resolve_channel(self.ticker_channel))
                    for interval in self._resolve_subscription_intervals():
                        channels.append(
                            self._resolve_channel(self.data_layer_channel, interval)
                        )
                    if not channels:
                        await asyncio.sleep(backoff)
                        continue

                    stream = await self._pubsub.listen(channels)
                    self._telemetry_set_phase_status(
                        self._PHASE_SUBSCRIPTION,
                        status="listening",
                        status_code="active",
                    )
                    self._telemetry_log(
                        f"Legacy candle subscription listening on {channels}",
                        level="INFO",
                        tone="neutral",
                    )
                    # Initialize downstream phases to idle (ready)
                    self._telemetry_set_phase_status(self._PHASE_AGGREGATION, status="idle", status_code="active")
                    self._telemetry_set_phase_status(self._PHASE_DISPATCH, status="idle", status_code="active")
                    self._telemetry_set_phase_status(self._PHASE_SIGNALS, status="idle", status_code="active")

                    async for message in stream:
                        if not self.active:
                            break
                        if not isinstance(message, Mapping):
                            continue
                        if not self._accept_tick(message):
                            continue
                        result = await self._process_tick(message)
                        if result is None:
                            continue
                        events = result if isinstance(result, list) else [result]
                        for event in events:
                            if event.get("is_closed") is False:
                                continue
                            await self._invoke_candle_handlers(event)
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
            self._clear_listener_task(listener_task)
            self._telemetry_log(
                "Candle listener stopped",
                level="INFO",
                tone="neutral",
                phase=self._PHASE_SUBSCRIPTION,
                deduplicate=False,
            )


    # ------------------------------------------------------------------
    def _resolve_subscription_interval(self) -> str:
        if self.subscription_interval:
            return self.subscription_interval
        return self._resolve_bar_source_interval(self.interval)

    # ------------------------------------------------------------------
    def _sync_required_market_streams(self) -> None:
        desired = ("bar",) if self._use_unified_data else ("ticker",)
        if getattr(self, "required_market_data_streams", None) == desired:
            return
        self.required_market_data_streams = desired

    # ------------------------------------------------------------------
    def _resolve_bar_channel(self) -> str:
        interval = self._resolve_subscription_interval()
        return self._resolve_bar_channel_for_interval(interval)

    # ------------------------------------------------------------------
    def _resolve_bar_channel_for_interval(self, interval: str) -> str:
        base_channel = (getattr(self, "bar_channel", None) or "").strip()
        if base_channel:
            return self._resolve_channel(base_channel, interval).rstrip(".")
        base_channel = (self.data_layer_channel or "").strip() or "market.bar"
        return self._resolve_channel(base_channel, interval).rstrip(".")

    # ------------------------------------------------------------------
    def _resolve_unified_event_channel(self) -> str:
        interval = self._resolve_subscription_interval()
        return self._resolve_unified_event_channel_for_interval(interval)

    # ------------------------------------------------------------------
    def _resolve_unified_event_channel_for_interval(self, interval: str) -> str:
        interval_token = _normalize_interval_token(interval) or interval
        base_channel = (getattr(self, "bar_channel", None) or "").strip()
        if not base_channel:
            base_channel = (self.data_layer_channel or "").strip() or "market.bar"
        channel = base_channel.rstrip(".")
        interval_suffix = self._channel_interval_token(interval_token)
        symbol = (self.symbol or "").strip().upper()
        if symbol:
            return f"{channel}-{symbol}-{interval_suffix}"
        return f"{channel}-*-{interval_suffix}"

    def _infer_interval_from_topic(self, topic: str | None) -> str | None:
        if not topic:
            return None
        lowered = topic.lower()
        candidates: list[tuple[str, str]] = []
        for interval in self._resolve_subscription_intervals():
            token = self._channel_interval_token(interval)
            candidates.append((token, interval))
        for interval in self.intervals or [self.interval]:
            token = self._channel_interval_token(interval)
            candidates.append((token, interval))
        for token, interval in candidates:
            if lowered.endswith(f"-{token.lower()}"):
                return _normalize_interval_token(interval) or interval
        return None

    # ------------------------------------------------------------------
    def _accept_tick(self, tick: Mapping[str, Any]) -> bool:
        symbol = str(tick.get("symbol", "")).strip().upper()
        if symbol and not self._matches_symbol(symbol):
            return False
        return True

    # ------------------------------------------------------------------
    def _matches_symbol(self, symbol: str | None) -> bool:
        strategy_symbol = (self.symbol or "").strip().upper()
        if not strategy_symbol:
            return True
        incoming_aliases = set(build_symbol_aliases(symbol))
        if not incoming_aliases:
            return False
        aliases = self._symbol_aliases or set(build_symbol_aliases(strategy_symbol))
        return not aliases.isdisjoint(incoming_aliases)

    # ------------------------------------------------------------------
    async def _process_tick(self, tick: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
        if tick.get("interval") or tick.get("timeframe"):
            return self._ingest_bar_payload(tick)

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

        # Update aggregation status for legacy tick path
        self._telemetry_set_phase_status(self._PHASE_AGGREGATION, status="processing", status_code="active")

        events = []
        for interval in self.intervals:
            delta = _interval_to_delta(interval)
            bucket = _floor_timestamp(timestamp, delta)

            current = self._current_candle.get(interval)

            if (
                current is not None
                and bucket > current["start"]
            ):
                closed = self._finalise_current(interval)
                with self._candles_lock:
                    self._candles[interval].append(closed)
                await self._dispatch_closed(closed)
                self._current_candle[interval] = None
                current = None

            if current is None:
                current = self._create_new_candle(bucket, price_value, interval)
                self._current_candle[interval] = current
            else:
                current["close"] = price_value
                current["high"] = max(current["high"], price_value)
                current["low"] = min(current["low"], price_value)

            current["volume"] += size
            current["last_timestamp"] = timestamp

            if interval == self.interval:
                self._telemetry_record_data_event(
                    timestamp, current.get("volume", 0.0), interval=interval
                )
                events.append(self._build_event(current, is_closed=False))

        return events

    # ------------------------------------------------------------------
    def _create_new_candle(self, start: datetime, price: float, interval: str | None = None) -> MutableMapping[str, Any]:
        return {
            "start": start,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 0.0,
            "interval": interval or self.interval,
            "symbol": self.symbol,
            "last_timestamp": start,
        }

    # ------------------------------------------------------------------
    def _finalise_current(self, interval: str | None = None) -> Mapping[str, Any]:
        target = interval or self.interval
        current = self._current_candle.get(target)
        assert current is not None
        candle = dict(current)
        candle["is_closed"] = True
        
        # Calculate delta based on interval
        delta = _interval_to_delta(target)
        
        candle["end"] = candle["start"] + delta
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
    def _extract_candle_end(
        self,
        candle: Mapping[str, Any],
        *,
        interval_delta: timedelta | None = None,
    ) -> datetime | None:
        delta = interval_delta or self._interval_delta
        end = self._maybe_parse_timestamp(
            candle.get("end") or candle.get("close_time")
        )
        if end is not None:
            return end
        end = self._maybe_parse_timestamp(candle.get("timestamp"))
        if end is not None:
            return end
        start = self._maybe_parse_timestamp(
            candle.get("start") or candle.get("open_time")
        )
        if start is not None:
            aligned = _floor_timestamp(start, delta)
            return aligned + delta
        start = self._maybe_parse_timestamp(
            candle.get("start") or candle.get("open_time")
        )
        if start is None:
            return None
        return start + delta

    # ------------------------------------------------------------------
    def _is_interval_boundary(
        self, timestamp: datetime | None, interval_delta: timedelta | None = None
    ) -> bool:
        if timestamp is None:
            return False
        aligned = _floor_timestamp(timestamp, interval_delta or self._interval_delta)
        return aligned == timestamp

    # ------------------------------------------------------------------
    def _resolve_multi_aggregator(
        self,
        target_interval: str,
        source_interval_seconds: int,
    ) -> MinuteBarAggregator:
        if not hasattr(self, "_multi_aggregators"):
            self._multi_aggregators = {}

        aggregator = self._multi_aggregators.get(target_interval)
        target_delta = _interval_to_delta(target_interval)

        if aggregator is None or aggregator.interval != target_delta:
            aggregator = MinuteBarAggregator(
                target_delta,
                symbol=self.symbol or None,
                interval_label=target_interval,
                source_interval_seconds=source_interval_seconds,
            )
            self._multi_aggregators[target_interval] = aggregator
        else:
            aggregator.symbol = self.symbol or None
            aggregator.interval_label = target_interval

        return aggregator

    # ------------------------------------------------------------------
    def _ingest_bar_payload(
        self, payload: Mapping[str, Any], *, ts_ns: int | None = None
    ) -> list[Mapping[str, Any]]:
        record = dict(payload)
        if record.get("timestamp") is None and ts_ns is not None:
            record["timestamp"] = ts_ns / 1_000_000_000
        interval_hint = _normalize_interval_token(record.get("interval"))
        if interval_hint is None:
            interval_hint = _normalize_interval_token(record.get("timeframe"))
        bar_size_hint = _normalize_interval_token(record.get("bar_size"))
        if interval_hint is None and bar_size_hint is not None:
            interval_hint = bar_size_hint
        source_interval_hint = interval_hint or bar_size_hint
        source_seconds = _interval_seconds(source_interval_hint)
        close_hint = record.get("is_closed")
        if close_hint is None:
            close_hint = not bool(record.get("is_snapshot"))
        close_bool = bool(close_hint)
        if not close_bool:
            start_ts = record.get("start") or record.get("open_time")
            if start_ts is not None:
                record["timestamp"] = start_ts
                record.pop("end", None)
                record.pop("close_time", None)

        self.logger.debug(f"DEBUG: Ingest {self.symbol} | hint={interval_hint} | close={close_bool} | candidates={self.intervals or [self.interval]}")
        self._telemetry_set_phase_status(self._PHASE_AGGREGATION, status="processing", status_code="active")
        # Ensure downstream phases are not stuck in awaiting_data if we are receiving data
        self._telemetry_set_phase_status(self._PHASE_DISPATCH, status="monitoring", status_code="active")
        self._telemetry_set_phase_status(self._PHASE_SIGNALS, status="monitoring", status_code="active")

        events: list[Mapping[str, Any]] = []
        candidates = self.intervals if self.intervals else [self.interval]

        for target_interval in candidates:
            # 1. Direct Match
            if interval_hint == target_interval and close_bool:
                normalized_candle = self._normalise_candle(
                    record,
                    is_closed=True,
                    interval_label=target_interval,
                )
                if normalized_candle is not None:
                    if (
                        hasattr(self, "_multi_aggregators")
                        and target_interval in self._multi_aggregators
                    ):
                        self._multi_aggregators[target_interval].reset()
                    event = dict(normalized_candle)
                    event["symbol"] = self.symbol
                    event["interval"] = target_interval
                    event["is_partial"] = False
                    events.append(event)

            # 2. Aggregation
            target_delta = _interval_to_delta(target_interval)
            target_seconds = int(target_delta.total_seconds())

            if (
                source_seconds is not None
                and source_seconds > 0
                and target_seconds > source_seconds
                and target_seconds % source_seconds == 0
            ):
                aggregator = self._resolve_multi_aggregator(
                    target_interval, source_seconds
                )
                closed_buckets = aggregator.push(record, close_hint=close_bool)
                for bucket in closed_buckets:
                    bucket_event = dict(bucket)
                    bucket_event["interval"] = target_interval
                    bucket_event["symbol"] = self.symbol
                    events.append(bucket_event)

        return events

    # ------------------------------------------------------------------
    async def _dispatch_closed(self, candle: Mapping[str, Any]) -> None:
        payload = dict(candle)
        interval = _normalize_interval_token(candle.get("interval")) or self.interval
        payload.update(
            {
                "type": "candle",
                "symbol": self.symbol,
                "interval": interval,
            }
        )
        self._telemetry_record_closed_candle(
            payload,
            timestamp=candle.get("end") if isinstance(candle, Mapping) else None,
        )
        await self._invoke_candle_handlers(payload)

    # ------------------------------------------------------------------
    def _build_event(
        self, candle: Mapping[str, Any], *, is_closed: bool
    ) -> Mapping[str, Any]:
        interval = candle.get("interval") or self.interval
        delta = _interval_to_delta(interval)
        end_time = candle["start"] + delta
        event = {
            "type": "candle",
            "symbol": self.symbol,
            "interval": interval,
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
        try:
            extra: dict[str, Any] = {
                "event": "strategy.order.blocked_local",
                "strategy": self.name,
                "symbol": self.symbol,
                "block_code": code,
            }
            if message:
                extra["block_message"] = message
            if details:
                extra["block_details"] = dict(details)
            is_backtest = str(getattr(self, "runtime_mode", "") or "").strip().lower() == "backtest"
            log_method = self.logger.info if code == "history_replay" or is_backtest else self.logger.warning
            log_method(
                "Order blocked for strategy %s (%s)", self.name, code, extra=extra
            )
        except Exception:
            pass
        self._telemetry_log(
            "Order blocked",
            level="WARN",
            tone="warning",
            phase=self._PHASE_DISPATCH,
            deduplicate=False,
            details=payload,
        )
        self._telemetry_record_local_block(
            code,
            message=message,
            details=details,
        )

    # ------------------------------------------------------------------
    def pop_last_order_block(self) -> Mapping[str, Any] | None:
        block = self._last_order_block
        self._last_order_block = None
        return block

    # ------------------------------------------------------------------
    def _normalise_candle(
        self,
        raw: Mapping[str, Any],
        *,
        is_closed: bool = True,
        interval_label: str | None = None,
    ) -> Mapping[str, Any] | None:
        try:
            open_ = float(raw["open"])
            high = float(raw["high"])
            low = float(raw["low"])
            close = float(raw["close"])
            volume = float(raw.get("volume", 0.0))
        except (ValueError, KeyError, TypeError):
            return None
        
        target_interval = interval_label or raw.get("interval") or self.interval
        # Ensure we have a valid interval logic
        # Some payloads might miss interval, defaulting to self.interval is risky if multiple streams are open
        # But if interval_label is passed from _ingest, it's safe.
        
        try:
             delta = _interval_to_delta(target_interval)
        except ValueError:
             # Fallback
             delta = self._interval_delta
             
        start_value = raw.get("start") or raw.get("open_time")
        end_value = raw.get("end") or raw.get("close_time")
        timestamp_value = raw.get("timestamp") or raw.get("time")

        if start_value is not None:
            start = _floor_timestamp(_parse_timestamp(start_value), delta)
        elif end_value is not None:
            end = _parse_timestamp(end_value)
            start = _floor_timestamp(end - delta, delta)
        else:
            start = _floor_timestamp(_parse_timestamp(timestamp_value), delta)
        
        return {
            "start": start,
            "end": start + delta,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "interval": target_interval,
            "symbol": (self.symbol or "").upper(),
            "is_closed": is_closed,
            "open_time": start.isoformat(),
            "close_time": (start + delta).isoformat(),
        }

    # ------------------------------------------------------------------
    def _record_last_processed(
        self,
        interval: str,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> None:
        interval_key = _normalize_interval_token(interval) or self.interval
        self._last_processed_candle_start_by_interval[interval_key] = start
        self._last_processed_candle_end_by_interval[interval_key] = end
        if interval_key == self.interval:
            self._last_processed_candle_start = start
            self._last_processed_candle_end = end

    def _should_dispatch_history(self) -> bool:
        return bool(getattr(self, "dispatch_history_candles", False))

    async def _handle_history_snapshot(self, candle: Mapping[str, Any]) -> None:
        if self._should_dispatch_history():
            await self._invoke_candle_handlers(candle)
            return
        interval = _normalize_interval_token(candle.get("interval")) or self.interval
        start = self._maybe_parse_timestamp(
            candle.get("start") or candle.get("open_time")
        )
        end = self._maybe_parse_timestamp(
            candle.get("end") or candle.get("close_time")
        )
        if end is None and start is not None:
            try:
                delta = _interval_to_delta(interval)
            except ValueError:
                delta = self._interval_delta
            end = start + delta
        self._record_last_processed(interval, start=start, end=end)

    # ------------------------------------------------------------------
    # Deprecated or overloaded get_candles signature handling in subclass
    # We replaced get_candles earlier with one taking optional interval.
    # The original method took no args.
    # We should ensure compatibility if someone calls it without args.
    # (The replacement earlier handled default=None).


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
        history_replay = bool(getattr(self, "_history_replay_in_progress", False))
        suppress_queue = bool(getattr(self, "_suppress_order_queue", False))
        allow_replay_orders = bool(getattr(self, "allow_history_replay_orders", False))
        if (history_replay or suppress_queue) and not allow_replay_orders:
            self._record_order_block(
                "history_replay",
                message="Order queue suppressed during history replay",
            )
            return False
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

        is_replay = bool(getattr(self, "_history_replay_in_progress", False))
        allow_replay_orders = bool(getattr(self, "allow_history_replay_orders", False))
        if is_replay and not allow_replay_orders:
            self.logger.warning(
                "Order suppressed during history replay for strategy %s", self.name
            )
            self._telemetry_log(
                "Order suppressed during history replay",
                tone="warning",
            )
            self._record_order_block(
                "history_replay",
                message="History replay suppressing new order",
            )
            return False

        cooldown = max(0.0, float(self.cooldown_seconds))
        if not is_replay and cooldown > 0.0 and now_monotonic < self._cooldown_until and not is_exit_like:
            remaining = max(0.0, self._cooldown_until - now_monotonic)
            self.logger.warning(
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
        if not is_replay and frequency > 0.0 and last_wall is not None and not is_exit_like:
            now_wall = self._wall_clock_now()
            elapsed = now_wall - last_wall
            if elapsed < frequency:
                remaining = max(0.0, frequency - elapsed)
                self.logger.warning(
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
            self.logger.warning(
                "Order rejected during normalization for strategy %s. Payload: %s",
                self.name,
                order
            )
            self._record_order_block(
                "normalization_failed",
                message="Order normalization failed",
                details={"original_payload": str(order)},
            )
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

        self._telemetry_record_generated_signal(
            normalised.get("side") or normalised.get("action"),
            quantity=normalised.get("quantity") or order.get("quantity"),
            reason=normalised.get("reason") or order.get("reason"),
            metadata=normalised.get("metadata"),
        )
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

    def _resolve_market_data_client(self) -> Any | None:
        bootstrapper = getattr(self, "market_data_bootstrapper", None)
        if bootstrapper is not None:
            candidate = getattr(bootstrapper, "_client", None)
            if candidate is not None:
                return candidate
        candidate = getattr(self, "market_data_client", None)
        if candidate is not None:
            return candidate
        return None

    async def _fetch_history_from_market_data_service(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        interval: timedelta,
        force_ib: bool = False,
        cached_only: bool = False,
    ) -> list[Mapping[str, Any]]:
        if not symbol:
            return []
        client = self._resolve_market_data_client()
        if client is None:
            return []
        try:
            response = await client.fetch_historical(
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                source="cache" if cached_only else None,
                force_ib=force_ib if force_ib else None,
            )
        except Exception:
            self.logger.debug(
                "Market data history request failed",
                extra={
                    "timeframe": timeframe,
                    "symbol": symbol,
                    "force_ib": force_ib,
                    "cached_only": cached_only,
                },
                exc_info=True,
            )
            return []
        items = getattr(response, "items", None)
        if not items:
            return []
        records: list[Mapping[str, Any]] = []
        for item in list(items):
            ts = getattr(item, "timestamp", None)
            if not isinstance(ts, datetime):
                continue
            records.append(
                {
                    "symbol": symbol,
                    "interval": timeframe,
                    "start": ts.isoformat(),
                    "open": getattr(item, "open", None),
                    "high": getattr(item, "high", None),
                    "low": getattr(item, "low", None),
                    "close": getattr(item, "close", None),
                    "volume": getattr(item, "volume", None) or 0.0,
                    "is_closed": True,
                }
            )
        return records

    async def _load_history_records(
        self,
        *,
        request: DataSubscriptionRequest,
        start: datetime,
        end: datetime,
        interval: timedelta,
        config: HistoryReplayConfig | None = None,
        force_ib: bool = False,
        cached_only: bool = False,
        raise_on_failure: bool = False,
    ) -> list[Mapping[str, Any]]:
        if config is None:
            config = self._history_replay_config()
        manager = self._data_layer_manager
        if manager is None:
            try:
                manager = get_data_source_manager()
            except RuntimeError:
                manager = None
        if manager is not None:
            try:
                if force_ib or cached_only:
                    options = dict(request.options or {})
                    if force_ib:
                        options["force_ib"] = True
                    if cached_only:
                        options["source"] = "cache"
                    effective_request = DataSubscriptionRequest(
                        channel=request.channel,
                        symbol=request.symbol,
                        interval=request.interval,
                        options=options,
                    )
                else:
                    effective_request = request
                records = await load_history_with_backoff(
                    manager,
                    effective_request,
                    start=start,
                    end=end,
                    interval=interval,
                    config=config,
                    logger=self.logger,
                )
                if records or not force_ib or cached_only:
                    return records
                self.logger.debug(
                    "Data layer backfill returned empty result with force_ib; falling back to market data service",
                    extra={"symbol": request.symbol, "interval": request.interval},
                )
            except DataSourceError:
                if cached_only:
                    return []
                if self._resolve_market_data_client() is None and raise_on_failure:
                    raise
                self.logger.debug(
                    "Data layer backfill failed; falling back to market data service",
                    extra={"symbol": request.symbol, "interval": request.interval},
                    exc_info=True,
                )
            except Exception:
                if cached_only:
                    return []
                if self._resolve_market_data_client() is None and raise_on_failure:
                    raise DataSourceError("historical market data unavailable")
                self.logger.debug(
                    "Data layer backfill failed; falling back to market data service",
                    extra={"symbol": request.symbol, "interval": request.interval},
                    exc_info=True,
                )

        symbol = (request.symbol or self.symbol or "").strip().upper()
        timeframe = str(
            request.interval
            or (request.options or {}).get("interval")
            or self.interval
        )
        records = await self._fetch_history_from_market_data_service(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            interval=interval,
            force_ib=force_ib,
            cached_only=cached_only,
        )
        if records or not raise_on_failure:
            return records
        raise DataSourceError("historical market data unavailable")

    # ------------------------------------------------------------------
    async def _ensure_unified_subscription(self) -> None:
        if not self._use_unified_data:
            return
        if self._data_layer_subscriptions or self._data_layer_subscription is not None:
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
        now = datetime.now(timezone.utc)
        target_intervals: list[str] = []
        for interval in self.intervals or [self.interval]:
            token = _normalize_interval_token(interval) or self.interval
            if token not in target_intervals:
                target_intervals.append(token)
        source_intervals = self._resolve_subscription_intervals()
        history_intervals = list(dict.fromkeys(source_intervals))
        config = self._history_replay_config()
        history_by_interval: dict[str, list[Mapping[str, Any]]] = {}
        total_replayed = 0
        if not self._history_backfill_completed:
            history_failed = False
            try:
                limit = max(1, int(self.history_limit))
            except Exception:
                limit = 1
            for interval in history_intervals:
                try:
                    delta = _interval_to_delta(interval)
                except ValueError:
                    delta = self._interval_delta
                start = _floor_timestamp(now - (delta * limit), delta)
                request = DataSubscriptionRequest(
                    channel=self._resolve_bar_channel_for_interval(interval),
                    symbol=self.symbol,
                    interval=interval,
                    options={
                        "interval": interval,
                        "start": start,
                        "end": now,
                    },
                )
                try:
                    history_records = await self._load_history_records(
                        request=request,
                        start=start,
                        end=now,
                        interval=delta,
                        config=config,
                        raise_on_failure=True,
                        # Startup should only consult local/cache-backed history.
                        # If cache is cold, proceed with live subscription first
                        # and let the background retry path perform heavier
                        # historical recovery later.
                        cached_only=True,
                        force_ib=False,
                    )
                    history_list = list(history_records or [])
                    history_by_interval[interval] = history_list
                    total_replayed += len(history_list)
                except DataSourceError as exc:
                    history_failed = True
                    self.logger.warning(
                        "Unified candle history unavailable for symbol=%s interval=%s (continuing with live stream): %s",
                        self.symbol,
                        interval,
                        exc,
                    )
                    # We intentionally suppress the exception here to allow the strategy
                    # to proceed with live data subscription even if backfill fails.
                    # The _history_backfill_failed flag will trigger a background retry.
                    pass
            self._history_backfill_completed = True
            self._history_backfill_failed = history_failed

        if history_by_interval:
            self._telemetry_update_phase_metrics(
                self._PHASE_SUBSCRIPTION,
                history_replay_count=total_replayed,
            )
            self._history_replay_in_progress = True
            try:
                self._last_processed_candle_start = None
                self._last_processed_candle_end = None
                self._last_processed_candle_start_by_interval = {
                    interval: None for interval in target_intervals
                }
                self._last_processed_candle_end_by_interval = {
                    interval: None for interval in target_intervals
                }
                with self._candles_lock:
                    self._candles.clear()
                aggregated_history_by_interval: dict[str, list[Mapping[str, Any]]] = {}
                for interval in target_intervals:
                    source_interval = self._resolve_bar_source_interval(interval)
                    source_records = history_by_interval.get(source_interval, [])
                    if not source_records:
                        continue
                    if source_interval == interval:
                        aggregated_history_by_interval[interval] = list(source_records)
                        continue
                    aggregated_history_by_interval[interval] = self._aggregate_history_records(
                        source_records,
                        target_interval=interval,
                        source_interval=source_interval,
                    )

                for interval, history_list in aggregated_history_by_interval.items():
                    if not history_list:
                        continue
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
                    dispatched: list[Mapping[str, Any]] = []
                    with self._candles_lock:
                        target_deque = self.get_candles(interval)
                        for candle in history_list[-self.history_limit :]:
                            if not candle:
                                continue
                            symbol = str(candle.get("symbol") or "").upper()
                            if symbol and not self._matches_symbol(symbol):
                                continue
                            normalised = self._normalise_candle(
                                candle, is_closed=True, interval_label=interval
                            )
                            if normalised is not None:
                                target_deque.append(normalised)
                                dispatched.append(normalised)
                    if dispatched:
                        self.logger.info(
                            "Replayed %d unified historical candle(s) for symbol=%s interval=%s",
                            len(dispatched),
                            self.symbol,
                            interval,
                        )
                        self._telemetry_log(
                            f"Replayed {len(dispatched)} historical candle(s) from unified feed",
                            level="INFO",
                            tone="positive",
                            phase=self._PHASE_SUBSCRIPTION,
                            deduplicate=False,
                            details={"interval": interval},
                        )
                        for snapshot in dispatched:
                            await self._handle_history_snapshot(snapshot)
            finally:
                self._history_replay_in_progress = False
        elif total_replayed:
            self._telemetry_update_phase_metrics(
                self._PHASE_SUBSCRIPTION,
                history_replay_count=total_replayed,
            )

        self._data_layer_subscriptions = []
        self._event_bus_tokens = []
        self._unified_event_channels = []
        primary_source_interval = self._resolve_bar_source_interval(self.interval)
        primary_subscription_channel: str | None = None
        primary_event_pattern: str | None = None
        for interval in source_intervals:
            channel = self._resolve_bar_channel_for_interval(interval)
            request = DataSubscriptionRequest(
                channel=channel,
                symbol=self.symbol,
                interval=interval,
                options={"interval": interval},
            )
            subscription = await manager.subscribe(request)
            self._data_layer_subscriptions.append(subscription)
            pattern = self._resolve_unified_event_channel_for_interval(interval)
            token = manager.event_bus.subscribe(pattern, self._handle_candle_event)
            self._event_bus_tokens.append(token)
            self._unified_event_channels.append(pattern)
            if self._data_layer_subscription is None:
                self._data_layer_subscription = subscription
            if self._event_bus_token is None:
                self._event_bus_token = token
            if self._unified_event_channel is None:
                self._unified_event_channel = pattern
            if primary_subscription_channel is None or interval == primary_source_interval:
                primary_subscription_channel = channel
                primary_event_pattern = pattern
            self.logger.info(
                "Subscribed to unified candle stream symbol=%s interval=%s",
                self.symbol,
                interval,
            )
        self._telemetry_update_phase_metrics(
            self._PHASE_SUBSCRIPTION,
            subscription_channel=primary_subscription_channel,
            event_pattern=primary_event_pattern,
        )
        self.logger.debug(
            "Unified candle subscription configured",
            extra={
                "event": "strategy.candle.subscription_configured",
                "strategy": self.name,
                "symbol": self.symbol,
                "target_intervals": target_intervals,
                "source_intervals": source_intervals,
                "channels": list(self._unified_event_channels),
            },
        )
        self._schedule_coroutine(self._check_bar_stream_health())
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
                    "subscription_channel": primary_subscription_channel,
                },
            )

    # ------------------------------------------------------------------
    async def _teardown_unified_subscription(self) -> None:
        manager = self._data_layer_manager
        if manager is None:
            self._data_layer_subscription = None
            self._data_layer_subscriptions = []
            self._event_bus_token = None
            self._event_bus_tokens = []
            return
        if self._event_bus_tokens:
            for token in list(self._event_bus_tokens):
                try:
                    manager.event_bus.unsubscribe(token)
                except Exception:  # pragma: no cover - defensive logging
                    self.logger.debug("Failed to unsubscribe candle event bus", exc_info=True)
        elif self._event_bus_token is not None:
            try:
                manager.event_bus.unsubscribe(self._event_bus_token)
            except Exception:  # pragma: no cover - defensive logging
                self.logger.debug("Failed to unsubscribe candle event bus", exc_info=True)
        if self._data_layer_subscriptions:
            for subscription in list(self._data_layer_subscriptions):
                try:
                    await manager.unsubscribe(subscription)
                except Exception:  # pragma: no cover - defensive logging
                    self.logger.debug("Failed to cancel candle data subscription", exc_info=True)
        elif self._data_layer_subscription is not None:
            try:
                await manager.unsubscribe(self._data_layer_subscription)
            except Exception:  # pragma: no cover - defensive logging
                self.logger.debug("Failed to cancel candle data subscription", exc_info=True)
        self._data_layer_subscription = None
        self._data_layer_subscriptions = []
        self._event_bus_token = None
        self._event_bus_tokens = []
        self._unified_event_channel = None
        self._unified_event_channels = []
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

    async def _resubscribe_unified(self, *, reason: str) -> None:
        try:
            await self._teardown_unified_subscription()
            await self._ensure_unified_subscription()
        except Exception as exc:  # pragma: no cover - defensive logging
            self.logger.error(
                "Failed to resubscribe after %s: %s",
                reason,
                exc,
            )

    # ------------------------------------------------------------------
    def _handle_candle_event(self, envelope: EventEnvelope) -> None:
        payload = dict(envelope.payload or {})
        payload.setdefault("stream_topic", envelope.topic)
        if not (
            payload.get("interval")
            or payload.get("timeframe")
            or payload.get("bar_size")
        ):
            try:
                self.logger.warning(
                    "Candle event missing interval metadata",
                    extra={
                        "event": "strategy.candle.missing_interval",
                        "strategy": self.name,
                        "symbol": self.symbol,
                        "topic": envelope.topic,
                        "payload_keys": list(payload.keys()),
                    },
                )
            except Exception:
                pass
        ts_ns = envelope.ts_ns
        self._schedule_coroutine(
            self._handle_runner_bar_payload(
                payload, ts_ns=ts_ns, symbol_hint=envelope.symbol
            )
        )

    # ------------------------------------------------------------------
    async def _handle_runner_bar_payload(
        self,
        payload: Mapping[str, Any],
        *,
        ts_ns: int | None = None,
        symbol_hint: str | None = None,
    ) -> None:
        symbol = (payload.get("symbol") or symbol_hint or "").upper()
        if symbol and not self._matches_symbol(symbol):
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

        # Update subscription status to connected on successful bar processing
        self._telemetry_set_phase_status(
            self._PHASE_SUBSCRIPTION,
            status="connected",
            status_code="data_received",
            status_reason="Successfully processed incoming bar",
            status_details={"symbol": self.symbol},
            timestamp=datetime.now(timezone.utc),
        )

        candidates: list[Mapping[str, Any]] = []
        bar_payload = payload.get("bar")
        payload_is_snapshot = bool(payload.get("is_snapshot"))
        if payload_is_snapshot and not self.allow_subscription_snapshots:
            return
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
                if payload_is_snapshot:
                    enriched_bar["is_closed"] = True
                else:
                    end_candidate = self._maybe_parse_timestamp(
                        enriched_bar.get("end")
                        or enriched_bar.get("close_time")
                        or enriched_bar.get("timestamp")
                    )
                    if end_candidate is None:
                        enriched_bar["is_closed"] = False
                    else:
                        now = datetime.now(timezone.utc)
                        guard_seconds = max(0.0, float(self.future_bar_guard_seconds))
                        enriched_bar["is_closed"] = end_candidate <= now + timedelta(
                            seconds=guard_seconds
                        )
            closed_bar = self._ingest_bar_payload(enriched_bar, ts_ns=ts_ns)
            if closed_bar is not None:
                if isinstance(closed_bar, list):
                    candidates.extend(closed_bar)
                else:
                    candidates.append(closed_bar)
            elif enriched_bar.get("is_closed") is False:
                candidates.append(enriched_bar)
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
                    if isinstance(closed_bar, list):
                        candidates.extend(closed_bar)
                    else:
                        candidates.append(closed_bar)
                elif enriched_bar.get("is_closed") is False:
                    candidates.append(enriched_bar)
        else:
            enriched_payload = dict(payload)
            if symbol:
                enriched_payload.setdefault("symbol", symbol)
            candidates.append(enriched_payload)

        target_token = _normalize_interval_token(self.interval)
        target_intervals = {
            _normalize_interval_token(item) or item
            for item in (self.intervals or [self.interval])
        }
        source_intervals = set(self._resolve_subscription_intervals())
        dated_candidates: list[tuple[datetime, str, Mapping[str, Any], bool]] = []
        saw_open_candidate = False
        saw_relevant_candidate = False
        skip_reason: str | None = None
        skip_details: dict[str, Any] = {}
        for candidate in candidates:
            interval_hint = _normalize_interval_token(
                candidate.get("interval")
                or candidate.get("timeframe")
                or candidate.get("bar_size")
            )
            if interval_hint is None:
                interval_hint = self.interval
            if interval_hint not in target_intervals and interval_hint not in source_intervals:
                if target_token is None or interval_hint != target_token:
                    skip_reason = "interval_mismatch"
                    skip_details = {
                        "interval_hint": interval_hint,
                        "target_intervals": list(target_intervals),
                        "source_intervals": list(source_intervals),
                    }
                    continue
            saw_relevant_candidate = True
            source_seconds = _interval_seconds(interval_hint)
            if interval_hint is not None and target_token is not None:
                if interval_hint != target_token and interval_hint not in target_intervals:
                    target_seconds = int(self._interval_delta.total_seconds())
                    if (
                        source_seconds is None
                        or source_seconds > target_seconds
                        or target_seconds % source_seconds != 0
                    ):
                        skip_reason = "interval_divisor_mismatch"
                        skip_details = {
                            "interval_hint": interval_hint,
                            "target_interval": target_token,
                            "source_seconds": source_seconds,
                            "target_seconds": target_seconds,
                        }
                        continue

            is_closed = bool(candidate.get("is_closed"))
            if not is_closed:
                saw_open_candidate = True
                skip_reason = "bar_not_closed"
                skip_details = {
                    "interval_hint": interval_hint,
                    "is_closed": candidate.get("is_closed"),
                }
                continue

            alignment_delta = self._interval_delta
            if source_seconds is not None and source_seconds > 0:
                alignment_delta = timedelta(seconds=source_seconds)
            interval_seconds = source_seconds or int(self._interval_delta.total_seconds())
            tolerance_seconds = max(
                0.0,
                float(self.future_bar_guard_seconds),
                min(10.0, float(interval_seconds) * 0.2),
            )

            def _within_boundary(ts: datetime, delta: timedelta) -> bool:
                aligned = _floor_timestamp(ts, delta)
                if aligned == ts:
                    return True
                if tolerance_seconds <= 0:
                    return False
                return (
                    abs((ts - aligned).total_seconds()) <= tolerance_seconds
                    or abs((ts - (aligned + delta)).total_seconds()) <= tolerance_seconds
                )

            raw_end = self._maybe_parse_timestamp(
                candidate.get("end")
                or candidate.get("close_time")
                or candidate.get("timestamp")
            )
            raw_start = self._maybe_parse_timestamp(
                candidate.get("start") or candidate.get("open_time")
            )
            if raw_end is not None and not _within_boundary(raw_end, alignment_delta):
                skip_reason = "end_not_on_boundary"
                skip_details = {
                    "interval_hint": interval_hint,
                    "raw_end": raw_end.isoformat(),
                    "alignment_seconds": int(alignment_delta.total_seconds()),
                }
                continue
            if raw_end is None and raw_start is not None and not _within_boundary(
                raw_start, alignment_delta
            ):
                skip_reason = "start_not_on_boundary"
                skip_details = {
                    "interval_hint": interval_hint,
                    "raw_start": raw_start.isoformat(),
                    "alignment_seconds": int(alignment_delta.total_seconds()),
                }
                continue

            try:
                interval_delta = _interval_to_delta(interval_hint)
            except ValueError:
                interval_delta = self._interval_delta

            end_ts = self._extract_candle_end(
                candidate, interval_delta=interval_delta
            )
            if end_ts is None:
                skip_reason = "end_ts_missing"
                skip_details = {
                    "interval_hint": interval_hint,
                    "raw_end": raw_end.isoformat() if raw_end else None,
                    "raw_start": raw_start.isoformat() if raw_start else None,
                }
                continue
            now = datetime.now(timezone.utc)
            guard_seconds = max(0.0, float(self.future_bar_guard_seconds), tolerance_seconds)
            if end_ts > now + timedelta(seconds=guard_seconds):
                skip_reason = "bar_in_future"
                skip_details = {
                    "interval_hint": interval_hint,
                    "end_ts": end_ts.isoformat(),
                    "now": now.isoformat(),
                    "guard_seconds": guard_seconds,
                }
                continue
            candidate_is_snapshot = bool(candidate.get("is_snapshot")) or payload_is_snapshot
            dated_candidates.append((end_ts, interval_hint, candidate, candidate_is_snapshot))

        if not dated_candidates:
            if saw_open_candidate:
                return
            try:
                now = datetime.now(timezone.utc)
                last_log = self._last_bar_skip_log_at
                if last_log is None or (now - last_log).total_seconds() >= 60.0:
                    self._last_bar_skip_log_at = now
                    if skip_reason is None and not skip_details:
                        try:
                            self.logger.debug(
                                "Bar payload pending aggregation",
                                extra={
                                    "event": "strategy.candle.payload_pending",
                                    "strategy": self.name,
                                    "symbol": self.symbol,
                                    "incoming_topic": (
                                        payload.get("stream_topic")
                                        if isinstance(payload, Mapping)
                                        else None
                                    )
                                    or (
                                        bar_payload.get("stream_topic")
                                        if isinstance(bar_payload, Mapping)
                                        else None
                                    ),
                                    "target_intervals": list(self.intervals or []),
                                    "source_intervals": self._resolve_subscription_intervals(),
                                },
                            )
                        except Exception:
                            pass
                        return
                    interval_hint = (
                        (bar_payload or payload).get("interval")
                        or (bar_payload or payload).get("timeframe")
                        or (bar_payload or payload).get("bar_size")
                    )
                    runtime_details = {
                        "event": "strategy.candle.payload_skipped",
                        "strategy": self.name,
                        "symbol": self.symbol,
                        "incoming_interval": str(interval_hint) if interval_hint is not None else None,
                        "incoming_topic": (
                            payload.get("stream_topic")
                            if isinstance(payload, Mapping)
                            else None
                        )
                        or (
                            bar_payload.get("stream_topic")
                            if isinstance(bar_payload, Mapping)
                            else None
                        ),
                        "target_intervals": list(self.intervals or []),
                        "source_intervals": self._resolve_subscription_intervals(),
                        "skip_reason": skip_reason,
                        "skip_details": skip_details or None,
                    }
                    self._telemetry_log(
                        "Skipped bar payload after validation",
                        level="INFO",
                        tone="neutral",
                        details=runtime_details,
                    )
                    self.logger.info(
                        "Skipped bar payload after validation",
                        extra=runtime_details,
                    )
            except Exception:
                pass
            return

        ordered = sorted(dated_candidates, key=lambda item: item[0])
        for end_ts, interval_hint, candidate, is_snapshot_candidate in ordered:
            last_end = self._last_processed_candle_end_by_interval.get(interval_hint)
            if end_ts is not None and last_end is not None:
                if end_ts <= last_end:
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

            normalised = self._normalise_candle(
                candidate, is_closed=True, interval_label=interval_hint
            )
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
                target_deque = self.get_candles(interval_hint)
                while target_deque and not isinstance(target_deque[-1], Mapping):
                    target_deque.pop()
                if target_deque:
                    last = target_deque[-1]
                    if last.get("end") == normalised.get("end"):
                        target_deque[-1] = normalised
                    else:
                        target_deque.append(normalised)
                else:
                    target_deque.append(normalised)
            self._last_event_ts = ts_ns
            self._telemetry_set_phase_status(
                self._PHASE_DISPATCH,
                status="running",
                status_code="receiving_data",
                status_reason="Receiving live candle data",
                timestamp=end_ts,
            )
            self._record_closed_candle_summary(interval_hint, end_ts)
            # Snapshot payloads are historical catch-up bars emitted after (re)subscription.
            # Keep indicators up to date, but only suppress order generation for stale replay bars.
            previous_replay_state = bool(getattr(self, "_history_replay_in_progress", False))
            treat_snapshot_as_replay = bool(
                is_snapshot_candidate
                and self._should_treat_snapshot_as_history_replay(
                    end_ts=end_ts,
                    interval_hint=interval_hint,
                )
            )
            if treat_snapshot_as_replay:
                self._history_replay_in_progress = True
            try:
                await self._invoke_candle_handlers(normalised)
            finally:
                if treat_snapshot_as_replay:
                    self._history_replay_in_progress = previous_replay_state

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
        allowed_intervals: set[str] = set()
        for item in self.intervals:
            token = _normalize_interval_token(item) or item
            if token:
                allowed_intervals.add(token)
        if interval_hint is not None and target_interval is not None:
            hint_token = _normalize_interval_token(str(interval_hint))
            if hint_token is not None and hint_token not in allowed_intervals:
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

        await self._handle_runner_bar_payload(event or {})

    # ------------------------------------------------------------------
    def _format_unified_candle_log(
        self, candle: Mapping[str, Any]
    ) -> tuple[str, Mapping[str, Any] | None] | None:
        """Return optional aggregation log details for a closed unified candle."""

        return None

    # ------------------------------------------------------------------
    def _should_treat_snapshot_as_history_replay(
        self,
        *,
        end_ts: datetime,
        interval_hint: str,
    ) -> bool:
        """Only stale catch-up snapshot bars should suppress order generation."""

        if end_ts.tzinfo is None:
            normalized_end = end_ts.replace(tzinfo=timezone.utc)
        else:
            normalized_end = end_ts.astimezone(timezone.utc)
        interval_seconds = _interval_seconds(interval_hint)
        if interval_seconds is None:
            try:
                interval_seconds = int(_interval_to_delta(interval_hint).total_seconds())
            except ValueError:
                interval_seconds = int(self._interval_delta.total_seconds())
        allowed_lag = max(float(interval_seconds) * 2.0, 300.0)
        try:
            now = datetime.fromtimestamp(self._wall_clock_now(), tz=timezone.utc)
        except Exception:
            now = datetime.now(timezone.utc)
        return now - normalized_end > timedelta(seconds=allowed_lag)

    # ------------------------------------------------------------------
    async def _invoke_candle_handlers(self, candle: Mapping[str, Any]) -> None:
        snapshot = dict(candle)
        is_backtest = getattr(self, "runtime_mode", "") == "backtest"
        interval = _normalize_interval_token(snapshot.get("interval")) or self.interval
        start = self._maybe_parse_timestamp(
            snapshot.get("start") or snapshot.get("open_time")
        )
        end = self._maybe_parse_timestamp(
            snapshot.get("end") or snapshot.get("close_time")
        )
        if end is None and start is not None:
            try:
                delta = _interval_to_delta(interval)
            except ValueError:
                delta = self._interval_delta
            end = start + delta
        stale_replay = False
        if end is not None:
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            try:
                now = datetime.fromtimestamp(self._wall_clock_now(), tz=timezone.utc)
            except Exception:
                now = datetime.now(timezone.utc)
            interval_seconds = _interval_seconds(interval)
            if interval_seconds is None:
                interval_seconds = int(self._interval_delta.total_seconds())
            allowed_lag = max(float(interval_seconds) * 2.0, 300.0)
            if now - end > timedelta(seconds=allowed_lag):
                stale_replay = True
        last_end = self._last_processed_candle_end_by_interval.get(interval)
        if end is not None and last_end is not None:
            if end <= last_end:
                return
        self._record_last_processed(interval, start=start, end=end)
        previous_replay_state = bool(getattr(self, "_history_replay_in_progress", False))
        if stale_replay and not previous_replay_state:
            self._history_replay_in_progress = True
        history_replay = bool(getattr(self, "_history_replay_in_progress", False))
        if history_replay:
            snapshot["is_history"] = True
        else:
            snapshot["is_history"] = False
        previous_suppress_state = bool(getattr(self, "_suppress_order_queue", False))
        if history_replay and not bool(getattr(self, "allow_history_replay_orders", False)):
            self._suppress_order_queue = True
        try:
            res = self.on_candle(dict(snapshot))
            if asyncio.iscoroutine(res):
                await res
            if not is_backtest:
                self._telemetry_set_phase_status(
                    self._PHASE_AGGREGATION,
                    status="active",
                    status_code="aggregated",
                    status_reason="Candle data aggregated",
                )
                self._telemetry_set_phase_status(
                    self._PHASE_DISPATCH,
                    status="active",
                    status_code="dispatched",
                    status_reason="Candle data dispatched",
                )
                self._telemetry_set_phase_status(
                    self._PHASE_SIGNALS,
                    status="active",
                    status_code="monitoring",
                    status_reason="Monitoring for signals",
                )
        except Exception:  # pragma: no cover - defensive logging
            self.logger.exception("Candle handler failed for %s", self.symbol)
        try:
            self._maybe_execute_base_exit(snapshot)
        except Exception:  # pragma: no cover - defensive logging
            self.logger.exception("Base exit evaluation failed for %s", self.symbol)
        if history_replay and not bool(getattr(self, "allow_history_replay_orders", False)):
            self._suppress_order_queue = previous_suppress_state
        if stale_replay and not previous_replay_state:
            self._history_replay_in_progress = previous_replay_state
        dispatcher = self._dispatch_event
        if dispatcher is None:
            return

        # Update dispatch status to active
        if not is_backtest:
            self._telemetry_set_phase_status(
                self._PHASE_DISPATCH,
                status="active",
                status_code="dispatching",
                status_reason="Dispatching candle event",
            )

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
            if queued_orders and not is_backtest:
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

    def _record_closed_candle_summary(
        self, interval: str, end_ts: datetime | None
    ) -> None:
        counts = self._closed_candle_counts
        counts[interval] = counts.get(interval, 0) + 1
        now = datetime.now(timezone.utc)
        last_log = self._last_closed_candle_log_at
        if last_log is not None and (now - last_log).total_seconds() < 300.0:
            return
        if not counts:
            return
        self._last_closed_candle_log_at = now
        summary = dict(sorted(counts.items(), key=lambda item: item[0]))
        self._closed_candle_counts = {}
        try:
            self.logger.info(
                "Closed candle summary",
                extra={
                    "event": "strategy.candle.closed_summary",
                    "strategy": self.name,
                    "symbol": self.symbol,
                    "window_seconds": 300,
                    "counts": summary,
                    "last_end": end_ts.isoformat() if end_ts else None,
                    "breaker_tripped": self.breaker_tripped,
                    "loss_streak": self.loss_streak,
                    "max_loss_streak": int(self.max_loss_streak),
                },
            )
        except Exception:
            pass

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
    def can_open_new_trade(self, side: str, quantity: float = 0.0) -> tuple[bool, str]:
        """Check if a new trade can be opened based on risk manager and current position."""
        # 1. Check current position (prevent pyramiding if not supported)
        # Use _resolve_position_state which is standard in StrategyTemplate
        current_pos, _ = self._resolve_position_state()
        if abs(current_pos) <= 1e-9:
            current_position_getter = getattr(self, "_get_current_position", None)
            if callable(current_position_getter):
                try:
                    current_pos = float(current_position_getter() or 0.0)
                except Exception:
                    current_pos = 0.0
        if abs(current_pos) > 1e-9:
            allow_pyramiding = bool(getattr(self, "allow_pyramiding", True))
            forbid_pyramiding = bool(getattr(self, "forbid_pyramiding", False)) or not allow_pyramiding
            if forbid_pyramiding:
                side_upper = str(side or "").strip().upper()
                same_direction = (current_pos > 0 and side_upper == "BUY") or (
                    current_pos < 0 and side_upper == "SELL"
                )
                if same_direction:
                    return False, f"Existing position {current_pos}"

        # 2. Check risk manager circuit breaker
        risk_manager = getattr(self, "risk_manager", None)
        if risk_manager is not None:
            checker = getattr(risk_manager, "check_circuit_breaker_before_new_order", None)
            if callable(checker):
                try:
                    ok, reason = checker()
                    if not ok:
                        return False, f"Risk breaker: {reason}"
                except Exception as e:
                    self.logger.exception("Risk manager check failed")
                    return False, f"Risk check error: {e}"

        # 3. Check cooldown (optional, usually handled by strategy logic but can be enforced here if needed)
        # For now, we leave specific cooldown logic to the strategy unless standardized

        return True, "OK"

    def check_exit_conditions(
        self,
        candle: Mapping[str, Any],
        position: float,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> tuple[bool, bool, float | None]:
        """
        Check if SL or TP is hit based on High/Low/Close of the candle.
        Returns: (triggered_sl, triggered_tp, exit_price)
        """
        if abs(position) <= 1e-9:
            return False, False, None

        open_price = _coerce_float(candle.get("open"))
        high_price = _coerce_float(candle.get("high"))
        low_price = _coerce_float(candle.get("low"))
        close_price = _coerce_float(candle.get("close"))

        # Fallback if High/Low are invalid (e.g. NaN), use Close
        if not (math.isfinite(high_price) and math.isfinite(low_price)):
            high_price = close_price
            low_price = close_price

        direction = 1.0 if position > 0 else -1.0
        triggered_sl = False
        triggered_tp = False
        exit_price = None

        if direction > 0:  # Long
            # Check SL (Low <= SL)
            if stop_loss is not None and low_price <= stop_loss:
                triggered_sl = True
                exit_price = stop_loss  # Assume filled at SL (slippage handled by sim engine usually, or worse)
            
            # Check TP (High >= TP)
            if take_profit is not None and high_price >= take_profit:
                triggered_tp = True
                # If both hit, check which one is closer to Open or assume SL first for safety
                if triggered_sl:
                    # Conservative: If Open is below SL, we gapped down -> SL at Open (or SL price).
                    # If Open is between SL and TP, we don't know which happened first.
                    # Conservative assumption: SL happened first.
                    triggered_tp = False
                    exit_price = stop_loss 
                    # Refinement: If Open < SL, actual fill is Open.
                    if open_price < stop_loss:
                        exit_price = open_price
                else:
                    exit_price = take_profit
                    # If Open > TP, we gapped up -> TP at Open
                    if open_price > take_profit:
                        exit_price = open_price

        else:  # Short
            # Check SL (High >= SL)
            if stop_loss is not None and high_price >= stop_loss:
                triggered_sl = True
                exit_price = stop_loss
            
            # Check TP (Low <= TP)
            if take_profit is not None and low_price <= take_profit:
                triggered_tp = True
                if triggered_sl:
                    triggered_tp = False
                    exit_price = stop_loss
                    if open_price > stop_loss:
                         exit_price = open_price
                else:
                    exit_price = take_profit
                    if open_price < take_profit:
                        exit_price = open_price

        return triggered_sl, triggered_tp, exit_price

    def _maybe_execute_base_exit(self, candle: Mapping[str, Any]) -> None:
        if not getattr(self, "use_base_exit", True):
            return
        if (
            getattr(self, "_history_replay_in_progress", False)
            and not bool(getattr(self, "allow_history_replay_orders", False))
        ):
            return
        
        # We need valid prices
        if not all(k in candle for k in ("open", "high", "low", "close")):
             # Fallback to checking close only if full candle not available
             price_raw = candle.get("close")
             if price_raw is None: 
                 return
        
        position, entry_price = self._resolve_position_state()

        # Guard against stale strategy-local position state. A stale non-zero
        # position can produce phantom exit signals (e.g. BUY to close short)
        # that never progress to risk/order logs when the account is actually flat.
        account_position: float | None = None
        current_position = getattr(self, "_current_position", None)
        if callable(current_position):
            try:
                observed = self._resolve_maybe_awaitable_float(
                    current_position(),
                    default=None,
                    label=f"{self.name}.current_position",
                )
            except Exception:
                observed = None
            if observed is not None:
                account_position = float(observed)

        runtime_mode = str(getattr(self, "runtime_mode", "") or "").strip().lower()
        if (
            runtime_mode != "backtest"
            and (
            account_position is not None
            and abs(account_position) <= 1e-9
            and abs(position) > 1e-9
            )
        ):
            self.logger.info(
                "Skipping base exit due to stale position state for %s",
                self.symbol,
                extra={
                    "event": "strategy.exit.position_mismatch",
                    "strategy": self.name,
                    "symbol": self.symbol,
                    "strategy_position": float(position),
                    "account_position": float(account_position),
                },
            )
            try:
                setattr(self, "_position", 0.0)
                setattr(self, "_entry_price", None)
            except Exception:
                pass
            self._base_exit_dispatched = False
            return

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
        try:
            setattr(self, "_position", float(position))
        except Exception:
            pass
        self._process_exit_trigger(
            position=float(position),
            entry_price=entry_price,
            exit_targets=exit_targets,
            trigger_evaluator=lambda: self.check_exit_conditions(
                candle,
                float(position),
                exit_targets.stop_loss,
                exit_targets.take_profit,
            ),
            dispatch_flag_attr="_base_exit_dispatched",
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
                closed_events = closed if isinstance(closed, list) else [closed]
                for event in closed_events:
                    if isinstance(event, Mapping):
                        aggregated.append(event)
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
            await self._handle_history_snapshot(snapshot)
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
                    self._run_coro(self._ensure_unified_subscription())
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
        self._last_bar_received_at = None
        self._last_bar_received_at_by_interval.clear()
        self._last_closed_volume = 0.0
        self._last_closed_price = 0.0
        self._last_runtime_status = None
        self._signals_generated = 0
        self._buy_signals_generated = 0
        self._sell_signals_generated = 0
        self._risk_evaluation_count = 0
        self._risk_blocked_count = 0
        self._local_order_block_count = 0
        self._orders_submitted_count = 0
        self._orders_rejected_count = 0
        self._orders_failed_count = 0
        self._orders_delegated_count = 0

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

    def _telemetry_record_generated_signal(
        self,
        side: Any,
        *,
        quantity: Any = None,
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(side, str):
            return
        side_token = side.strip().upper()
        if side_token not in {"BUY", "SELL"}:
            return
        self._signals_generated += 1
        if side_token == "BUY":
            self._buy_signals_generated += 1
        else:
            self._sell_signals_generated += 1
        signal_timestamp = None
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
        telemetry = self._telemetry()
        if telemetry is not None:
            try:
                telemetry.record_signal(
                    self._telemetry_strategy_id(),
                    side_token,
                    stage=self._PHASE_SIGNALS,
                    quantity=quantity,
                    timestamp=signal_timestamp,
                    notes=reason,
                )
            except KeyError:
                pass
            except Exception:  # pragma: no cover - defensive
                self.logger.debug("Failed to record telemetry signal", exc_info=True)
        self._telemetry_set_phase_status(
            self._PHASE_SIGNALS,
            status="ready",
            status_code="signal_generated",
        )
        self._telemetry_set_phase_status(
            self._PHASE_EXECUTION,
            status="running",
            status_code="awaiting_risk",
            status_reason="Generated signal awaiting risk evaluation and order submission",
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_SIGNALS,
            signals_generated=self._signals_generated,
            buy_signals=self._buy_signals_generated,
            sell_signals=self._sell_signals_generated,
            last_signal_side=side_token,
            last_signal_quantity=quantity,
            last_signal_reason=reason,
            last_signal_at=signal_timestamp or datetime.now(timezone.utc),
        )

    def _telemetry_record_local_block(
        self,
        code: str,
        *,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self._local_order_block_count += 1
        self._telemetry_set_phase_status(
            self._PHASE_EXECUTION,
            status="blocked",
            status_code="local_guard_blocked",
            status_reason=message or code,
            status_details=details,
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_EXECUTION,
            local_blocked_count=self._local_order_block_count,
            last_order_status="blocked_local",
            last_order_reason=message or code,
            last_order_at=datetime.now(timezone.utc),
        )

    def _telemetry_record_risk_outcome(
        self,
        *,
        permitted: bool,
        reason: str | None = None,
        order: Mapping[str, Any] | None = None,
    ) -> None:
        self._risk_evaluation_count += 1
        if not permitted:
            self._risk_blocked_count += 1
        self._telemetry_set_phase_status(
            self._PHASE_EXECUTION,
            status="running" if permitted else "blocked",
            status_code="risk_permitted" if permitted else "risk_blocked",
            status_reason=reason,
            status_details={"order": dict(order)} if isinstance(order, Mapping) else None,
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_EXECUTION,
            risk_evaluation_count=self._risk_evaluation_count,
            risk_blocked_count=self._risk_blocked_count,
            last_risk_reason=reason,
            last_order_status="risk_permitted" if permitted else "risk_blocked",
            last_order_reason=reason,
            last_order_at=datetime.now(timezone.utc),
        )

    def _telemetry_record_order_submission(
        self,
        status: str,
        *,
        reason: str | None = None,
        order: Mapping[str, Any] | None = None,
        response: Mapping[str, Any] | None = None,
    ) -> None:
        status_token = str(status).strip().lower()
        if status_token == "submitted":
            self._orders_submitted_count += 1
        elif status_token == "rejected":
            self._orders_rejected_count += 1
        elif status_token == "failed":
            self._orders_failed_count += 1
        elif status_token == "delegated":
            self._orders_delegated_count += 1
        phase_status = "ready" if status_token == "submitted" else "blocked"
        phase_code = f"order_{status_token}" if status_token else "order_update"
        details: dict[str, Any] = {}
        if isinstance(order, Mapping):
            details["order"] = dict(order)
        if isinstance(response, Mapping):
            details["response"] = dict(response)
        self._telemetry_set_phase_status(
            self._PHASE_EXECUTION,
            status=phase_status,
            status_code=phase_code,
            status_reason=reason,
            status_details=details or None,
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_EXECUTION,
            orders_submitted_count=self._orders_submitted_count,
            orders_rejected_count=self._orders_rejected_count,
            orders_failed_count=self._orders_failed_count,
            orders_delegated_count=self._orders_delegated_count,
            last_order_status=status_token or None,
            last_order_reason=reason,
            last_order_at=datetime.now(timezone.utc),
        )

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
    def _bar_inactivity_threshold_seconds(self, interval: str | None = None) -> float:
        base_interval = max(10.0, float(getattr(self, "_health_check_interval", 30.0)))
        threshold = max(base_interval * 2.0, 60.0)
        interval_seconds = 0.0
        target_interval = _normalize_interval_token(interval) if interval is not None else None
        if target_interval:
            resolved = _interval_seconds(target_interval)
            if resolved is not None and resolved > 0:
                interval_seconds = float(resolved)
        else:
            # Prefer the smallest target candle interval for general strategy
            # health metrics, but per-interval checks should pass the interval.
            candidate_seconds = [
                float(value)
                for value in (
                    _interval_seconds(token)
                    for token in (self.intervals or [self.interval])
                )
                if value is not None and value > 0
            ]
            if candidate_seconds:
                interval_seconds = min(candidate_seconds)
            else:
                try:
                    interval_seconds = float(self._interval_delta.total_seconds())
                except Exception:
                    interval_seconds = 0.0
        if interval_seconds > 0:
            # Require at least two full bars (or 120s) before declaring inactivity.
            threshold = max(threshold, interval_seconds * 2.0, 120.0)
        return threshold

    # ------------------------------------------------------------------
    def _mark_bar_stream_activity(
        self,
        *,
        seen_at: datetime | None = None,
        interval: str | None = None,
    ) -> None:
        """Mark the stream as alive when we receive relevant bar payloads."""
        if seen_at is None:
            seen_at = datetime.now(timezone.utc)
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=timezone.utc)
        else:
            seen_at = seen_at.astimezone(timezone.utc)
        self._last_bar_received_at = seen_at
        interval_token = _normalize_interval_token(interval) if interval is not None else None
        if interval_token:
            self._last_bar_received_at_by_interval[interval_token] = seen_at
        if self._bar_stream_missing or self._bar_stream_refresh_inflight:
            self._bar_stream_missing = False
            self._bar_stream_refresh_inflight = False
            self._inactivity_recovery_next_attempt = 0.0
            self._inactivity_recovery_backoff = 30.0
            self._telemetry_update_phase_metrics(
                self._PHASE_SUBSCRIPTION,
                stream_status="active",
                missing_stream=None,
                last_bar_at=self._telemetry_format_value(seen_at),
            )

    def _latest_bar_activity_for_interval(
        self,
        interval: str | None,
    ) -> datetime | None:
        interval_token = _normalize_interval_token(interval) if interval is not None else None
        if interval_token:
            return self._last_bar_received_at_by_interval.get(interval_token)
        return self._last_bar_received_at

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
    async def recover_streams(self, streams: set[str], reason: str | None = None) -> None:
        if not getattr(self, "active", False):
            return
        target_streams = {stream for stream in streams if stream in {"bar", "ticker"}}
        if not target_streams:
            return
        now = datetime.now(timezone.utc)
        details = {
            "streams": sorted(target_streams),
            "reason": reason or "stream_recovery_requested",
        }
        self._telemetry_log(
            "Strategy stream recovery requested",
            level="WARN",
            tone="warning",
            phase=self._PHASE_SUBSCRIPTION,
            deduplicate=False,
            details=details,
            timestamp=now,
        )
        if self._use_unified_data:
            try:
                await self._teardown_unified_subscription()
            except Exception:
                self.logger.exception("Failed to teardown unified subscription during recovery")
            try:
                await self._ensure_unified_subscription()
            except Exception:
                self.logger.exception("Failed to re-establish unified subscription during recovery")
        else:
            self._restart_bar_listener()

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
                except Exception as exc:
                    self.logger.exception("Health check failed with exception: %s", exc)
                    ready = False
                    health_reason = "market_data_health_check_failed"
            transient_health_failure = (
                not ready and _is_transient_market_data_health_reason(health_reason)
            )
            if not ready and not transient_health_failure:
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
            if transient_health_failure:
                self.logger.warning(
                    "Market data readiness probe returned transient failure for %s; attempting subscription registration anyway (%s)",
                    self.name,
                    health_reason,
                )

            registered = True
            if callable(registrar) and self.symbol:
                try:
                    intervals = list(dict.fromkeys(self.intervals or [self.interval]))
                    source_intervals = self._resolve_subscription_intervals()
                    for interval in source_intervals:
                        channel = self._resolve_unified_event_channel_for_interval(interval)
                        ok = await registrar(
                            self.name,
                            symbol=self.symbol,
                            timeframe=interval,
                            channel=channel,
                            metadata={
                                "strategy": self.name,
                                "symbol": self.symbol,
                                "interval": interval,
                                "intervals": intervals,
                                "source_intervals": source_intervals,
                            },
                        )
                        if not ok:
                            registered = False
                            break
                except Exception:
                    registered = False
            if not registered and callable(registration_checker):
                try:
                    registration_candidates: list[str] = []
                    telemetry_id = self._telemetry_strategy_id()
                    registration_candidates.append(str(telemetry_id))
                    if self.name not in registration_candidates:
                        registration_candidates.append(self.name)
                    for candidate in registration_candidates:
                        registered = await registration_checker(candidate)
                        if registered:
                            break
                    if not registered:
                        self.logger.info(
                            "Strategy %s registration check pending via coordinator",
                            self.name,
                            extra={"event": "strategy.subscription.registration_pending"},
                        )
                except Exception as e:
                    self.logger.info(
                        "Strategy %s registration check raised transient exception: %s",
                        self.name,
                        e,
                    )
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
        await self._start_unified_subscription()

    async def _start_unified_subscription(self) -> bool:
        already_connected = (
            self._data_layer_subscription is not None
            and self._subscription_connected_at is not None
        )
        try:
            if not already_connected:
                self._telemetry_set_phase_status(
                    self._PHASE_SUBSCRIPTION,
                    status="connecting",
                    status_code="initialising",
                )
            if self._health_check_enabled:
                self._schedule_coroutine(self._schedule_periodic_health_check())
            await self._ensure_unified_subscription()
            self._cancel_retry_thread()
            if already_connected:
                connected_at = self._subscription_connected_at or datetime.now(timezone.utc)
                self._telemetry_set_phase_status(
                    self._PHASE_SUBSCRIPTION,
                    status="connected",
                    status_code="subscribed",
                    status_reason="Unified candle subscription already active",
                    status_cause="Unified candle subscription already active",
                    status_cause_code="subscription_connected",
                    status_details={
                        "connected_at": self._telemetry_format_value(connected_at),
                    },
                    timestamp=connected_at,
                )
                self._telemetry_update_phase_metrics(
                    self._PHASE_SUBSCRIPTION,
                    connected_since=connected_at,
                    last_heartbeat=connected_at,
                )
                self._start_subscription_heartbeat()
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
            # Allow refresh even if strategy_not_registered, to recover from seeding timeouts
            if callable(refresher):
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
            
            # If strategy is not registered, attempt to re-register
            if refresh_reason == "strategy_not_registered":
                registrar = getattr(facade, "ensure_bar_subscription", None)
                if callable(registrar) and self.symbol:
                    try:
                        intervals = list(dict.fromkeys(self.intervals or [self.interval]))
                        source_intervals = self._resolve_subscription_intervals()
                        for interval in source_intervals:
                            await registrar(
                                self.name,
                                symbol=self.symbol,
                                timeframe=interval,
                                channel=self._resolve_unified_event_channel_for_interval(interval),
                                metadata={
                                    "strategy": self.name,
                                    "symbol": self.symbol,
                                    "interval": interval,
                                    "intervals": intervals,
                                    "source_intervals": source_intervals,
                                },
                            )
                        self._telemetry_log(
                            "Strategy re-registered during inactivity recovery",
                            level="INFO",
                            tone="positive",
                            phase=self._PHASE_SUBSCRIPTION,
                            deduplicate=False,
                            details=details,
                            timestamp=now,
                        )
                    except Exception:
                        self.logger.exception(
                            "Failed to re-register strategy during inactivity recovery"
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
                    await self._teardown_unified_subscription()
                except Exception:
                    self.logger.exception(
                        "Failed to teardown unified subscription during inactivity recovery"
                    )
                try:
                    await self._ensure_unified_subscription()
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
        refresher = getattr(facade, "refresh_subscription", None) if facade else None
        now = datetime.now(timezone.utc)
        source_intervals = tuple(self._resolve_subscription_intervals())
        stale_intervals: list[tuple[str, float | None, float]] = []
        metric_stale_intervals: list[dict[str, Any]] = []
        connected_runtime_seconds: float | None = None
        if self._subscription_connected_at is not None:
            connected_runtime_seconds = max(
                0.0, (now - self._subscription_connected_at).total_seconds()
            )

        for interval in source_intervals:
            threshold_seconds = self._bar_inactivity_threshold_seconds(interval)
            last_bar_at = self._latest_bar_activity_for_interval(interval)
            effective_last_bar_at = last_bar_at
            if (
                effective_last_bar_at is not None
                and self._subscription_connected_at is not None
                and self._subscription_connected_at > effective_last_bar_at
            ):
                effective_last_bar_at = self._subscription_connected_at
            inactive_seconds: float | None = None
            if effective_last_bar_at is not None:
                inactive_seconds = max(0.0, (now - effective_last_bar_at).total_seconds())
            elif (
                connected_runtime_seconds is not None
                and connected_runtime_seconds >= threshold_seconds
            ):
                inactive_seconds = connected_runtime_seconds

            if inactive_seconds is not None:
                metric_stale_intervals.append(
                    {
                        "interval": interval,
                        "inactive_seconds": round(inactive_seconds, 1),
                        "threshold_seconds": round(threshold_seconds, 1),
                        "last_bar_at": self._telemetry_format_value(last_bar_at),
                        "effective_last_bar_at": self._telemetry_format_value(
                            effective_last_bar_at
                        ),
                    }
                )
            if inactive_seconds is not None and inactive_seconds >= threshold_seconds:
                stale_intervals.append((interval, inactive_seconds, threshold_seconds))

        if not stale_intervals:
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
                    stale_intervals=[],
                )
                self._telemetry_log(
                    "Unified bar stream restored",
                    level="INFO",
                    tone="positive",
                    phase=self._PHASE_SUBSCRIPTION,
                    deduplicate=False,
                    timestamp=now,
                )
            elif metric_stale_intervals:
                self._telemetry_update_phase_metrics(
                    self._PHASE_SUBSCRIPTION,
                    stream_status="active",
                    stale_intervals=metric_stale_intervals,
                )
            return

        max_inactive_seconds = max(
            inactive_seconds or 0.0 for _, inactive_seconds, _ in stale_intervals
        )
        max_threshold_seconds = max(
            threshold_seconds for _, _, threshold_seconds in stale_intervals
        )
        recovery_triggered = False
        if stale_intervals:
            recovery_triggered = await self._recover_from_bar_inactivity(
                inactive_seconds=max_inactive_seconds,
                threshold_seconds=max_threshold_seconds,
                refresher=refresher if callable(refresher) else None,
            )
            if self._recovering_after_inactivity:
                return
        if recovery_triggered:
            return
        if not self._bar_stream_missing:
            self._telemetry_set_phase_status(
                self._PHASE_SUBSCRIPTION,
                status="waiting",
                status_code="awaiting_bar_stream",
                status_details={
                    "stream": "bar",
                    "stale_intervals": [
                        {
                            "interval": interval,
                            "inactive_seconds": round(inactive_seconds or 0.0, 1),
                            "threshold_seconds": round(threshold_seconds, 1),
                            "last_bar_at": self._telemetry_format_value(
                                self._latest_bar_activity_for_interval(interval)
                            ),
                        }
                        for interval, inactive_seconds, threshold_seconds in stale_intervals
                    ],
                },
            )
            self._telemetry_update_phase_metrics(
                self._PHASE_SUBSCRIPTION,
                stream_status="missing",
                missing_stream="bar",
                stale_intervals=metric_stale_intervals,
            )
            self._telemetry_log(
                "Unified bar stream inactive; awaiting coordinator refresh",
                level="WARN",
                tone="warning",
                phase=self._PHASE_SUBSCRIPTION,
                deduplicate=False,
                details={
                    "stale_intervals": [
                        {
                            "interval": interval,
                            "inactive_seconds": round(inactive_seconds or 0.0, 1),
                            "threshold_seconds": round(threshold_seconds, 1),
                        }
                        for interval, inactive_seconds, threshold_seconds in stale_intervals
                    ]
                },
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
                        details={
                            "refresh_reason": refresh_reason,
                            "stale_intervals": [
                                {
                                    "interval": interval,
                                    "inactive_seconds": round(inactive_seconds or 0.0, 1),
                                    "threshold_seconds": round(threshold_seconds, 1),
                                }
                                for interval, inactive_seconds, threshold_seconds in stale_intervals
                            ],
                        },
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
            registrar = getattr(facade, "ensure_bar_subscription", None) if facade else None
            if callable(registrar) and self.symbol:
                try:
                    intervals = list(dict.fromkeys(self.intervals or [self.interval]))
                    source_intervals = self._resolve_subscription_intervals()
                    for interval in source_intervals:
                        await registrar(
                            self.name,
                            symbol=self.symbol,
                            timeframe=interval,
                            channel=self._resolve_unified_event_channel_for_interval(interval),
                            metadata={
                                "strategy": self.name,
                                "symbol": self.symbol,
                                "interval": interval,
                                "intervals": intervals,
                                "source_intervals": source_intervals,
                            },
                        )
                except Exception:
                    self.logger.exception(
                        "Failed to re-register strategy during bar stream health check"
                    )

    def _telemetry_record_data_event(
        self,
        timestamp: datetime | None,
        volume: float | int | None,
        *,
        interval: str | None = None,
    ) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        event_ts = self._maybe_parse_timestamp(timestamp)
        interval_token = _normalize_interval_token(interval) or self.interval
        subscription_id = interval_token
        try:
            normalised = telemetry.record_data_event(
                self._telemetry_strategy_id(),
                timestamp=event_ts,
                subscription_id=subscription_id,
                symbol=self.symbol or None,
                interval=interval_token,
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
        self._last_bar_received_at_by_interval[interval_token] = seen_at
        self._tick_count += 1
        self._last_tick_timestamp = seen_at
        self._current_candle_volume = float(volume or 0.0)
        last_bar_value = self._telemetry_format_value(event_ts)
        self._telemetry_update_phase_metrics(
            self._PHASE_SUBSCRIPTION,
            last_bar_at=last_bar_value,
            stream_status="active" if not self._recovering_after_inactivity else "recovering_after_inactivity",
        )
        if self._subscription_connected_at is None:
            self._on_subscription_connected(
                reason="Bar stream active",
                cause="Received live bar data",
                cause_code="bar_stream_active",
                details={"last_bar_at": last_bar_value},
                timestamp=seen_at,
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
        interval = _normalize_interval_token(candle.get("interval")) or self.interval
        self._telemetry_record_data_event(
            timestamp, candle.get("volume"), interval=interval
        )
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
