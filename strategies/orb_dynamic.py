"""Dynamic Opening Range Breakout strategy with stage telemetry caching."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from statistics import mean
from typing import Any, Dict, Iterable, Mapping, Sequence

try:  # pragma: no cover - Python 3.8 fallback
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - fallback for systems without zoneinfo
    from backports.zoneinfo import ZoneInfo  # type: ignore[assignment]

from src.data_layer import DataSubscriptionRequest
from src.common.market_data.aggregation import floor_timestamp as _floor_timestamp
from .candle import CandleSubscriptionStrategy
from .liquidity_tool import LiquidityFilterConfig, LiquidityFilterMetrics, LiquidityStrategyTool
from .templates import StrategyTemplate
from src.strategy.exit import ExitMode

SESSION_PRESETS: Mapping[str, tuple[str, str]] = {
    "Auto-Detect": ("0930-1600", "America/New_York"),
    "New-York": ("0930-1600", "America/New_York"),
    "London": ("0800-1630", "Europe/London"),
    "Tokyo": ("0900-1500", "Asia/Tokyo"),
    "Sydney": ("1000-1600", "Australia/Sydney"),
    "Frankfurt": ("0900-1730", "Europe/Berlin"),
    "Custom": ("0930-1600", "America/New_York"),
}

STAGE_CONFIG: tuple[tuple[str, int, str], ...] = (
    ("ORB5", 5, "enable_orb5_signals"),
    ("ORB15", 15, "enable_orb15_signals"),
    ("ORB30", 30, "enable_orb30_signals"),
    ("ORB60", 60, "enable_orb60_signals"),
)

INPUT_PARAMETER_METADATA = [
    ("enable_orb5_signals", "bool", "Enable ORB 5-Min", {}),
    ("enable_orb15_signals", "bool", "Enable ORB 15-Min", {}),
    ("enable_orb30_signals", "bool", "Enable ORB 30-Min", {}),
    ("enable_orb60_signals", "bool", "Enable ORB 60-Min", {}),
    (
        "session_mode",
        "string",
        "Session Preset",
        {
            "options": [
                "Auto-Detect",
                "New-York",
                "London",
                "Tokyo",
                "Sydney",
                "Frankfurt",
                "Custom",
            ]
        },
    ),
    ("custom_session", "session", "Custom Session Hours", {}),
    ("enable_extended_hours", "bool", "Include Extended Hours", {}),
    ("extended_pre_market", "session", "Pre-Market Session", {}),
    ("extended_after_hours", "session", "After-Hours Session", {}),
    ("enable_breakout", "bool", "Enable Breakout Detection", {}),
    (
        "enable_auto_stage_orders",
        "bool",
        "Automatically dispatch ORB breakout orders when ready",
        {},
    ),
    ("enable_retest", "bool", "Enable Retest Tracking", {}),
    (
        "breakout_buffer",
        "float",
        "Breakout Buffer (%)",
        {"min": 0.0, "max": 5.0, "step": 0.1},
    ),
    (
        "retest_buffer",
        "float",
        "Retest Buffer (%)",
        {"min": 0.0, "max": 5.0, "step": 0.1},
    ),
    (
        "atr_length",
        "int",
        "ATR Lookback",
        {"min": 5, "max": 50},
    ),
    (
        "atr_multiplier",
        "float",
        "ATR Multiplier",
        {"min": 0.5, "max": 3.0, "step": 0.1},
    ),
    ("volume_ma_length", "int", "Volume MA Length", {"min": 1, "max": 200}),
    ("enable_volume_filter", "bool", "Enable Volume Filter", {}),
    ("volume_multiplier", "float", "Volume Multiplier", {"min": 0.5, "step": 0.1}),
    ("strong_volume_multiplier", "float", "Strong Volume Multiplier", {"min": 1.0, "step": 0.1}),
    ("enable_trend_filter", "bool", "Enable Trend Filter", {}),
    ("fixed_risk", "float", "Fixed Risk ($)", {"min": 10.0, "max": 10000.0}),
    (
        "risk_pct",
        "float",
        "Risk Percentage (%)",
        {"min": 0.1, "max": 5.0, "step": 0.1},
    ),
    ("enable_liquidity_filter", "bool", "Enable Liquidity Filter", {}),
    ("liquidity_interval", "string", "Liquidity Interval", {}),
    ("liquidity_lookback_bars", "int", "Liquidity Lookback Bars", {"min": 40, "max": 400}),
    ("liquidity_atr_period", "int", "Liquidity ATR Period", {"min": 5, "max": 60}),
    ("liquidity_swing_window", "int", "Liquidity Swing Window", {"min": 1, "max": 8}),
    (
        "liquidity_eq_tolerance_ticks",
        "float",
        "EQ Tolerance (ticks)",
        {"min": 0.5, "max": 12.0, "step": 0.5},
    ),
    (
        "liquidity_min_penetration_ticks",
        "float",
        "Min Sweep Penetration (ticks)",
        {"min": 0.5, "max": 12.0, "step": 0.5},
    ),
    ("liquidity_max_reclaim_bars", "int", "Max Reclaim Bars", {"min": 1, "max": 8}),
    (
        "liquidity_displacement_atr_multiplier",
        "float",
        "Displacement ATR Multiplier",
        {"min": 0.2, "max": 3.0, "step": 0.1},
    ),
    (
        "liquidity_structure_lookback",
        "int",
        "Displacement Structure Lookback",
        {"min": 2, "max": 30},
    ),
    (
        "liquidity_tick_size",
        "float",
        "Liquidity Tick Size",
        {"min": 0.0001, "max": 10.0, "step": 0.0001},
    ),
    (
        "liquidity_invalidate_buffer_ticks",
        "float",
        "Invalidate Buffer (ticks)",
        {"min": 0.0, "max": 8.0, "step": 0.5},
    ),
    (
        "liquidity_min_confidence",
        "float",
        "Liquidity Min Confidence",
        {"min": 0.0, "max": 1.0, "step": 0.05},
    ),
]


def _coerce_float(value: Any, default: float = float("nan")) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if math.isfinite(numeric):
        return numeric
    return default


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            ts = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        try:
            ts = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _parse_session_window(value: str) -> tuple[dtime, dtime]:
    try:
        start_text, end_text = value.split("-", 1)
        start_hour = max(0, min(23, int(start_text[:2])))
        start_minute = max(0, min(59, int(start_text[2:4])))
        end_hour = max(0, min(23, int(end_text[:2])))
        end_minute = max(0, min(59, int(end_text[2:4])))
        return dtime(start_hour, start_minute), dtime(end_hour, end_minute)
    except Exception:
        return dtime(9, 30), dtime(16, 0)


def _moving_average_volume(candles: Iterable[Mapping[str, Any]], length: int) -> float | None:
    volumes: list[float] = []
    for candle in candles:
        volume = _coerce_float(candle.get("volume"), float("nan"))
        if math.isfinite(volume):
            volumes.append(volume)
    if not volumes:
        return None
    window = volumes[-max(1, length) :]
    if not window:
        return None
    return mean(window)


def _calculate_atr(candles: Iterable[Mapping[str, Any]], length: int) -> float | None:
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    for candle in candles:
        high = _coerce_float(candle.get("high"), float("nan"))
        low = _coerce_float(candle.get("low"), float("nan"))
        close = _coerce_float(candle.get("close"), float("nan"))
        if not (math.isfinite(high) and math.isfinite(low) and math.isfinite(close)):
            continue
        highs.append(high)
        lows.append(low)
        closes.append(close)
    periods = min(len(highs), len(lows), len(closes))
    length = max(1, min(length, periods))
    if periods < 2 or length < 1:
        return None
    true_ranges: list[float] = []
    for index in range(1, periods):
        current_high = highs[index]
        current_low = lows[index]
        previous_close = closes[index - 1]
        tr = max(
            current_high - current_low,
            abs(current_high - previous_close),
            abs(current_low - previous_close),
        )
        true_ranges.append(tr)
    if not true_ranges:
        return None
    window = true_ranges[-length:]
    return sum(window) / len(window)


def _calculate_ema(candles: Iterable[Mapping[str, Any]], length: int) -> float | None:
    closes: list[float] = []
    for candle in candles:
        close = _coerce_float(candle.get("close"), float("nan"))
        if math.isfinite(close):
            closes.append(close)
    if len(closes) < length:
        return None
    # Calculate initial SMA
    ema = sum(closes[:length]) / length
    multiplier = 2.0 / (length + 1)
    for price in closes[length:]:
        ema = (price - ema) * multiplier + ema
    return ema


def _interval_to_minutes(interval: str) -> int:
    text = (interval or "1m").strip().lower()
    if text.endswith("ms"):
        return 1
    if text.endswith("s"):
        seconds = _coerce_int(text[:-1], 1)
        return max(1, seconds // 60) or 1
    if text.endswith("m"):
        return max(1, _coerce_int(text[:-1], 1))
    if text.endswith("h"):
        hours = _coerce_int(text[:-1], 1)
        return max(1, hours * 60)
    if text.endswith("d"):
        days = _coerce_int(text[:-1], 1)
        return max(1, days * 1440)
    return max(1, _coerce_int(text, 1))


def _format_telemetry_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


@dataclass
class ORBStageState:
    """State container for a single ORB stage."""

    name: str
    minutes: int
    candles_required: int
    enabled: bool = True
    range_high: float | None = None
    range_low: float | None = None
    completed_at: datetime | None = None
    last_candle_time: datetime | None = None
    total_volume: float = 0.0
    candles_processed: int = 0
    breakout_up: int = 0
    breakout_down: int = 0
    retests: int = 0
    failures: int = 0
    active_breakout: str | None = None
    range_pct: float | None = None
    atr_ratio: float | None = None
    volume_ratio: float | None = None
    last_error: str | None = None
    last_metrics: Dict[str, Any] = field(default_factory=dict)
    last_log_snapshot: Dict[str, Any] = field(default_factory=dict, repr=False)
    last_position_open: bool | None = field(default=None, repr=False)

    def reset(self, enabled: bool, candles_required: int) -> None:
        self.enabled = enabled
        self.candles_required = max(1, candles_required)
        self.range_high = None
        self.range_low = None
        self.completed_at = None
        self.last_candle_time = None
        self.total_volume = 0.0
        self.candles_processed = 0
        self.breakout_up = 0
        self.breakout_down = 0
        self.retests = 0
        self.failures = 0
        self.active_breakout = None
        self.range_pct = None
        self.atr_ratio = None
        self.volume_ratio = None
        self.last_error = None
        self.last_metrics.clear()
        self.last_log_snapshot.clear()
        self.last_position_open = None

    @property
    def complete(self) -> bool:
        return self.completed_at is not None

    @property
    def range_value(self) -> float | None:
        if self.range_high is None or self.range_low is None:
            return None
        return self.range_high - self.range_low

    def consume(
        self,
        *,
        high: float,
        low: float,
        close: float,
        volume: float,
        close_time: datetime,
    ) -> bool:
        if not self.enabled or self.completed_at is not None:
            return False
        if self.last_candle_time is not None and close_time <= self.last_candle_time:
            # Idempotency check: Don't process the same candle again
            return False
        changed = False
        previous_high = self.range_high
        previous_low = self.range_low
        if math.isfinite(high):
            self.range_high = high if previous_high is None else max(previous_high, high)
        if math.isfinite(low):
            self.range_low = low if previous_low is None else min(previous_low, low)
        if self.range_high != previous_high or self.range_low != previous_low:
            changed = True
        self.total_volume += max(0.0, volume)
        self.candles_processed += 1
        self.last_candle_time = close_time
        if self.candles_processed >= self.candles_required:
            self.completed_at = close_time
            range_value = self.range_value
            if range_value is not None and self.range_low and self.range_low > 0:
                self.range_pct = (range_value / self.range_low) * 100.0
            else:
                self.range_pct = None
            self.last_error = None
            changed = True
        return changed

    def update_indicators(
        self, *, atr: float | None, avg_volume: float | None
    ) -> bool:
        updated = False
        range_value = self.range_value
        atr_ratio = None
        if range_value is not None and atr and atr > 0:
            atr_ratio = range_value / atr
        if atr_ratio != self.atr_ratio:
            self.atr_ratio = atr_ratio
            updated = True
        stage_avg_volume = (
            (self.total_volume / self.candles_processed)
            if self.candles_processed
            else None
        )
        volume_ratio = None
        if stage_avg_volume is not None and avg_volume and avg_volume > 0:
            volume_ratio = stage_avg_volume / avg_volume
        if volume_ratio != self.volume_ratio:
            self.volume_ratio = volume_ratio
            updated = True
        return updated

    def evaluate_breakout(
        self,
        close_price: float,
        breakout_buffer: float,
        retest_buffer: float,
        *,
        enable_retest: bool,
        volume_ok: bool = True,
        trend_ok_up: bool = True,
        trend_ok_down: bool = True,
    ) -> bool:
        if not self.complete or self.range_high is None or self.range_low is None:
            return False
        changed = False
        upper_trigger = self.range_high * (1.0 + breakout_buffer / 100.0)
        lower_trigger = self.range_low * (1.0 - breakout_buffer / 100.0)
        if close_price >= upper_trigger:
            if self.active_breakout != "up":
                if volume_ok and trend_ok_up:
                    self.breakout_up += 1
                    self.active_breakout = "up"
                    self.last_error = None
                    changed = True
        elif close_price <= lower_trigger:
            if self.active_breakout != "down":
                if volume_ok and trend_ok_down:
                    self.breakout_down += 1
                    self.active_breakout = "down"
                    self.last_error = None
                    changed = True
        else:
            if not enable_retest:
                if self.active_breakout is not None and (
                    close_price <= self.range_low or close_price >= self.range_high
                ):
                    self.failures += 1
                    self.active_breakout = None
                    self.last_error = "breakout_failed"
                    changed = True
                return changed
            if self.active_breakout == "up":
                retest_threshold = self.range_high * (1.0 - retest_buffer / 100.0)
                if close_price <= retest_threshold:
                    self.retests += 1
                    self.active_breakout = "retest_up"
                    self.last_error = None
                    changed = True
                elif close_price <= self.range_low:
                    self.failures += 1
                    self.active_breakout = None
                    self.last_error = "breakout_failed"
                    changed = True
            elif self.active_breakout == "down":
                retest_threshold = self.range_low * (1.0 + retest_buffer / 100.0)
                if close_price >= retest_threshold:
                    self.retests += 1
                    self.active_breakout = "retest_down"
                    self.last_error = None
                    changed = True
                elif close_price >= self.range_high:
                    self.failures += 1
                    self.active_breakout = None
                    self.last_error = "breakout_failed"
                    changed = True
            elif self.active_breakout in {"retest_up", "retest_down"}:
                if close_price >= upper_trigger or close_price <= lower_trigger:
                    self.failures += 1
                    self.active_breakout = None
                    self.last_error = "breakout_failed"
                    changed = True
        return changed

    def pipeline_status(self) -> str:
        if not self.enabled:
            return "disabled"
        if not self.complete:
            return "awaiting_range"
        if self.active_breakout is None:
            return "awaiting_breakout"
        if self.active_breakout in {"up", "down"}:
            return f"breakout_{self.active_breakout}"
        if self.active_breakout in {"retest_up", "retest_down"}:
            return self.active_breakout
        return "awaiting_breakout"

    def metrics_snapshot(self) -> Dict[str, Any]:
        return {
            f"{self.name}.range": self.range_value,
            f"{self.name}.range_pct": self.range_pct,
            f"{self.name}.atr_ratio": self.atr_ratio,
            f"{self.name}.volume_ratio": self.volume_ratio,
            f"{self.name}.breakouts_up": self.breakout_up,
            f"{self.name}.breakouts_down": self.breakout_down,
            f"{self.name}.retests": self.retests,
            f"{self.name}.failures": self.failures,
        }

    def telemetry_payload(self) -> Dict[str, Any]:
        snapshot = self.metrics_snapshot()
        snapshot.update(
            {
                "stage": self.name,
                "complete": self.complete,
                "completed_at": _format_telemetry_timestamp(self.completed_at),
                "breakout_state": self.active_breakout,
                "last_error": self.last_error,
                "pipeline_status": self.pipeline_status(),
            }
        )
        return snapshot

    def log_payload(self) -> Dict[str, Any]:
        payload = self.telemetry_payload()
        payload.update(
            {
                "minutes": self.minutes,
                "candles_required": self.candles_required,
                "enabled": self.enabled,
                "candles_processed": self.candles_processed,
                "range_high": self.range_high,
                "range_low": self.range_low,
                "range_pct": self.range_pct,
                "atr_ratio": self.atr_ratio,
                "volume_ratio": self.volume_ratio,
                "breakouts_up": self.breakout_up,
                "breakouts_down": self.breakout_down,
                "retests": self.retests,
                "failures": self.failures,
            }
        )
        payload["completed_at"] = _format_telemetry_timestamp(self.completed_at)
        return payload


@dataclass
class DynamicORBStrategy(CandleSubscriptionStrategy, StrategyTemplate):
    """Opening Range Breakout strategy with configurable multi-stage levels."""

    name: str = "Dynamic ORB Breakout"
    strategy_type: str = "dynamic_orb_breakout"
    description: str = (
        "跟踪5/15/30/60分钟开盘区间，实时缓存阶段数据并输出突破/回测统计"
    )
    symbol: str = "SPY"
    interval: str = "5m"
    history_limit: int = 400
    default_quantity: int = 1
    cooldown_seconds: float = 60.0
    signal_frequency_seconds: float = 180.0
    max_loss_streak: int = 3
    enable_orb5_signals: bool = True
    enable_orb15_signals: bool = True
    enable_orb30_signals: bool = True
    enable_orb60_signals: bool = True
    session_mode: str = "Auto-Detect"
    custom_session: str = "0930-1600"
    enable_extended_hours: bool = False
    extended_pre_market: str = "0400-0930"
    extended_after_hours: str = "1600-2000"
    enable_breakout: bool = True
    enable_auto_stage_orders: bool = True
    enable_retest: bool = True
    breakout_buffer: float = 0.2
    retest_buffer: float = 0.0
    atr_length: int = 14
    atr_multiplier: float = 1.5
    volume_ma_length: int = 20
    enable_volume_filter: bool = False
    volume_multiplier: float = 1.0
    strong_volume_multiplier: float = 1.5
    enable_trend_filter: bool = False
    fixed_risk: float = 200.0
    risk_pct: float = 1.0
    enable_liquidity_filter: bool = False
    liquidity_interval: str = "1m"
    liquidity_lookback_bars: int = 120
    liquidity_atr_period: int = 14
    liquidity_swing_window: int = 2
    liquidity_eq_tolerance_ticks: float = 3.0
    liquidity_min_penetration_ticks: float = 3.0
    liquidity_max_reclaim_bars: int = 2
    liquidity_displacement_atr_multiplier: float = 1.0
    liquidity_structure_lookback: int = 8
    liquidity_tick_size: float = 0.25
    liquidity_invalidate_buffer_ticks: float = 1.0
    liquidity_min_confidence: float = 0.45
    dispatch_history_candles: bool = True

    _session_tz: ZoneInfo = field(init=False, repr=False)
    _session_open_clock: dtime = field(init=False, repr=False)
    _session_close_clock: dtime = field(init=False, repr=False)
    _current_session: date | None = field(default=None, init=False, repr=False)
    _stage_cache: Dict[str, ORBStageState] = field(
        default_factory=dict, init=False, repr=False
    )
    _base_interval_minutes: int = field(default=5, init=False, repr=False)
    _stage_signal_events: Dict[str, datetime] = field(
        default_factory=dict, init=False, repr=False
    )
    _last_ordered_stage: str | None = field(default=None, init=False, repr=False)
    _last_ordered_at: datetime | None = field(default=None, init=False, repr=False)
    _liquidity_tool: LiquidityStrategyTool = field(init=False, repr=False)

    def __post_init__(self) -> None:  # noqa: D401 - defer to parent docstring
        super().__post_init__()
        # Configure exit settings
        exit_config = getattr(self, "exit_config", None)
        if exit_config is not None:
            if exit_config.mode == ExitMode.NONE:
                exit_config.mode = ExitMode.ATR
            exit_config.atr_length = max(1, int(self.atr_length))
            exit_config.atr_multiplier = float(self.atr_multiplier)
        
        # Configure strategy settings
        self.intervals = [self.interval]
        self._base_interval_minutes = _interval_to_minutes(self.interval)
        self._configure_session()
        self._initialise_stage_cache()
        self._configure_liquidity_filter()
        self._register_parameters()

    def _bootstrap_history_target(self) -> int:
        indicator_need = max(
            int(self.atr_length) * 4,
            int(self.volume_ma_length) * 4,
            120,
        )
        return max(80, min(int(self.history_limit), indicator_need))

    async def on_start(self) -> None:
        """Initialize strategy and perform active history backfill."""
        await super().on_start()
        await self._await_market_data_ready_and_subscribe()
        if self._history_replay_in_progress:
            return

        # Active history retrieval (Backfill)
        with self._candles_lock:
            candles = self.get_candles(self.interval)
            current_count = len(candles)

        required_history = self._bootstrap_history_target()
        if current_count < required_history:
            self.logger.info(
                "Active backfill requested",
                extra={
                    "current": current_count,
                    "required": required_history,
                    "symbol": self.symbol,
                },
            )
            
            # Calculate time range using the actual subscribed interval.
            delta = self._interval_delta
            now = datetime.now(timezone.utc)
            missing = required_history - current_count
            start_time = _floor_timestamp(now - (self._interval_delta * missing), delta)
            
            request = DataSubscriptionRequest(
                channel=self._resolve_bar_channel(),
                symbol=self.symbol,
                interval=self.interval,
                options={
                    "interval": self.interval,
                    "start": start_time,
                    "end": now,
                },
            )
            
            try:
                records = await asyncio.wait_for(
                    self._load_history_records(
                        request=request,
                        start=start_time,
                        end=now,
                        interval=delta,
                    ),
                    timeout=60.0,
                )
                
                ingested_count = 0
                if records:
                    self._reset_unified_bucket()
                    self._history_replay_in_progress = True
                    try:
                        for item in records:
                            if not item:
                                continue
                            closed = self._ingest_bar_payload(item)
                            if closed:
                                # Handle list return from _ingest_bar_payload
                                closed_events = closed if isinstance(closed, list) else [closed]
                                for event in closed_events:
                                    with self._candles_lock:
                                        target = self.get_candles(self.interval)
                                        # Avoid duplicates based on end time
                                        if not target or target[-1].get("end") != event.get("end"):
                                            target.append(event)
                                            ingested_count += 1
                                    await self.on_candle(event)
                    finally:
                        self._history_replay_in_progress = False
                    
                    self.logger.info(
                        "Backfill completed",
                        extra={
                            "ingested": ingested_count,
                            "symbol": self.symbol
                        }
                    )
            except Exception as e:
                self.logger.exception(
                    "Backfill failed",
                    extra={"error": str(e)}
                )

        # Update status to monitoring
        now = datetime.now(timezone.utc)
        self._telemetry_set_phase_status(
            self._PHASE_SIGNALS,
            status="running",
            status_code="monitoring",
            status_reason="Monitoring market data",
            timestamp=now,
        )
        # Also update execution phase to avoid "awaiting_data"
        self._telemetry_set_phase_status(
            getattr(self, "_PHASE_EXECUTION", "execution"),
            status="running",
            status_code="monitoring",
            status_reason="Monitoring signals",
            timestamp=now,
        )

    # ------------------------------------------------------------------
    def _sync_required_market_streams(self) -> None:  # noqa: D401 - base override
        if getattr(self, "required_market_data_streams", None) != ("bar",):
            self.required_market_data_streams = ("bar",)

    # ------------------------------------------------------------------
    def _configure_session(self) -> None:
        preset = SESSION_PRESETS.get(self.session_mode, SESSION_PRESETS["Auto-Detect"])
        window_text, tz_name = preset
        if self.session_mode == "Custom":
            window_text = (self.custom_session or preset[0]).strip() or preset[0]
        start_clock, end_clock = _parse_session_window(window_text)
        try:
            self._session_tz = ZoneInfo(tz_name)
        except Exception:  # pragma: no cover - fallback to UTC
            self._session_tz = ZoneInfo("UTC")
        self._session_open_clock = start_clock
        self._session_close_clock = end_clock

    # ------------------------------------------------------------------
    def _initialise_stage_cache(self) -> None:
        self._stage_cache.clear()
        for name, minutes, flag in STAGE_CONFIG:
            enabled = bool(getattr(self, flag, True))
            candles_required = max(1, math.ceil(minutes / self._base_interval_minutes))
            self._stage_cache[name] = ORBStageState(
                name=name,
                minutes=minutes,
                candles_required=candles_required,
                enabled=enabled,
            )
        self._current_session = None

    # ------------------------------------------------------------------
    def _reset_stage_state(self, session_date: date) -> None:
        for name, minutes, flag in STAGE_CONFIG:
            enabled = bool(getattr(self, flag, True))
            candles_required = max(1, math.ceil(minutes / self._base_interval_minutes))
            stage = self._stage_cache.setdefault(
                name,
                ORBStageState(
                    name=name,
                    minutes=minutes,
                    candles_required=candles_required,
                    enabled=enabled,
                ),
            )
            stage.reset(enabled, candles_required)
        self._stage_signal_events.clear()
        self._last_ordered_stage = None
        self._last_ordered_at = None
        # self._telemetry_log(
        #     "Reset ORB stage cache",
        #     phase=self._PHASE_AGGREGATION,
        #     level="DEBUG",
        #     deduplicate=True,
        #     details={
        #         "session_date": session_date.isoformat(),
        #         "stages": [stage.name for stage in self._stage_cache.values()],
        #     },
        # )

    # ------------------------------------------------------------------
    def _register_parameters(self) -> None:
        definitions: Dict[str, Dict[str, Any]] = {}
        for name, type_name, label, extra in INPUT_PARAMETER_METADATA:
            metadata = {"type": type_name, "label": label}
            metadata.update(extra)
            current_value = getattr(self, name, None)
            if current_value is not None:
                metadata.setdefault("default", current_value)
            definitions[name] = metadata
        self.set_parameter_definitions(definitions)

    # ------------------------------------------------------------------
    def apply_parameter_updates(self, updates: Mapping[str, Any]) -> Dict[str, Any]:
        applied = super().apply_parameter_updates(updates)
        if not applied:
            return applied
        stage_flags = {
            "enable_orb5_signals",
            "enable_orb15_signals",
            "enable_orb30_signals",
            "enable_orb60_signals",
        }
        if stage_flags.intersection(applied):
            for name, minutes, flag in STAGE_CONFIG:
                stage = self._stage_cache.get(name)
                if stage is None:
                    continue
                enabled = bool(getattr(self, flag, True))
                candles_required = max(
                    1, math.ceil(minutes / self._base_interval_minutes)
                )
                stage.reset(enabled, candles_required)
        if any(
            key in applied
            for key in {
                "session_mode",
                "custom_session",
                "enable_extended_hours",
                "extended_pre_market",
                "extended_after_hours",
            }
        ):
            self._configure_session()
            self._current_session = None
        if any(key.startswith("liquidity_") for key in applied) or "enable_liquidity_filter" in applied:
            self._configure_liquidity_filter()
        self._register_parameters()
        return applied

    # ------------------------------------------------------------------
    def _ensure_session(self, close_time: datetime) -> None:
        local_dt = close_time.astimezone(self._session_tz)
        session_date = local_dt.date()
        if self._current_session == session_date:
            return
        self._current_session = session_date
        self._reset_stage_state(session_date)

    # ------------------------------------------------------------------
    def _record_stage_signal_event(
        self, stage: ORBStageState, close_time: datetime
    ) -> None:
        self._stage_signal_events[stage.name] = close_time

    # ------------------------------------------------------------------
    def _stage_order_sequence_valid(self) -> bool:
        # Only check sequence for stages that have actually signaled
        signaled_stages = [
            name
            for name, _, _ in STAGE_CONFIG
            if name in self._stage_signal_events
        ]
        
        if not signaled_stages:
            return True
            
        sequence = [
            (name, self._stage_signal_events[name]) for name in signaled_stages
        ]
        
        # Sort by timestamp
        ordered_by_time = sorted(sequence, key=lambda pair: pair[1])
        actual_order = [name for name, _ in ordered_by_time]
        
        # Expected order is the relative order of these stages in STAGE_CONFIG
        config_order_map = {name: i for i, (name, _, _) in enumerate(STAGE_CONFIG)}
        expected_order = sorted(signaled_stages, key=lambda name: config_order_map.get(name, 999))
        
        return actual_order == expected_order

    # ------------------------------------------------------------------
    def _stage_ready_for_order(
        self, stage: ORBStageState, close_time: datetime
    ) -> bool:
        if not self.enable_auto_stage_orders:
            self.logger.debug(f"Stage {stage.name} ready check failed: auto_stage_orders disabled")
            return False

        # Do NOT block based on other stages being incomplete. 
        # Independent stages should be able to trade as soon as they are ready.

        if not self._stage_order_sequence_valid():
            self.logger.debug(f"Stage {stage.name} blocked by invalid sequence")
            return False

        if self._last_ordered_at is not None:
            elapsed = (close_time - self._last_ordered_at).total_seconds()
            if elapsed < self.cooldown_seconds:
                self.logger.debug(f"Stage {stage.name} blocked by cooldown: {elapsed} < {self.cooldown_seconds}")
                return False

        if self._last_ordered_stage:
            ordered_stage_names = [name for name, _, _ in STAGE_CONFIG]
            try:
                last_index = ordered_stage_names.index(self._last_ordered_stage)
                current_index = ordered_stage_names.index(stage.name)
                if current_index <= last_index:
                    self.logger.debug(f"Stage {stage.name} blocked by stage order: {current_index} <= {last_index}")
                    return False
            except ValueError:
                pass
        
        self.logger.debug(f"Stage {stage.name} IS READY for order")
        return True

    def _check_volume_condition(self, volume: float, avg_volume: float | None) -> bool:
        if not self.enable_volume_filter:
            return True
        if avg_volume is None or avg_volume <= 0:
            return True
        return volume > (avg_volume * self.volume_multiplier)

    def _check_trend_condition(self, close_price: float, candles: Sequence[Mapping[str, Any]], side: str) -> bool:
        if not self.enable_trend_filter:
            return True
        # Use EMA 20 for trend check
        ema = _calculate_ema(candles, 20)
        if ema is None:
            return True
        if side == "up":
            return close_price > ema
        else:
            return close_price < ema

    def _configure_liquidity_filter(self) -> None:
        self.enable_liquidity_filter = bool(self.enable_liquidity_filter)
        self.liquidity_interval = (self.liquidity_interval or "1m").strip() or "1m"
        self.liquidity_lookback_bars = max(40, int(self.liquidity_lookback_bars))
        self.liquidity_atr_period = max(2, int(self.liquidity_atr_period))
        self.liquidity_swing_window = max(1, int(self.liquidity_swing_window))
        self.liquidity_eq_tolerance_ticks = max(0.5, float(self.liquidity_eq_tolerance_ticks))
        self.liquidity_min_penetration_ticks = max(0.5, float(self.liquidity_min_penetration_ticks))
        self.liquidity_max_reclaim_bars = max(1, int(self.liquidity_max_reclaim_bars))
        self.liquidity_displacement_atr_multiplier = max(
            0.1, float(self.liquidity_displacement_atr_multiplier)
        )
        self.liquidity_structure_lookback = max(2, int(self.liquidity_structure_lookback))
        self.liquidity_tick_size = max(1e-6, float(self.liquidity_tick_size))
        self.liquidity_invalidate_buffer_ticks = max(
            0.0, float(self.liquidity_invalidate_buffer_ticks)
        )
        self.liquidity_min_confidence = max(0.0, min(1.0, float(self.liquidity_min_confidence)))
        config = LiquidityFilterConfig(
            interval=self.liquidity_interval,
            lookback_bars=self.liquidity_lookback_bars,
            atr_period=self.liquidity_atr_period,
            swing_window=self.liquidity_swing_window,
            eq_tolerance_ticks=self.liquidity_eq_tolerance_ticks,
            min_penetration_ticks=self.liquidity_min_penetration_ticks,
            max_reclaim_bars=self.liquidity_max_reclaim_bars,
            displacement_atr_multiplier=self.liquidity_displacement_atr_multiplier,
            structure_lookback=self.liquidity_structure_lookback,
            tick_size=self.liquidity_tick_size,
            invalidate_buffer_ticks=self.liquidity_invalidate_buffer_ticks,
        )
        if hasattr(self, "_liquidity_tool"):
            self._liquidity_tool.update_config(config)
        else:
            self._liquidity_tool = LiquidityStrategyTool(config)

    def _evaluate_liquidity_gate(
        self,
        *,
        side: str,
        reference_price: float,
        timestamp: datetime,
        reason: str,
        stage_name: str,
    ) -> tuple[bool, LiquidityFilterMetrics]:
        interval_used = self.liquidity_interval
        candles = list(self.get_candles(interval_used))
        if not candles and interval_used != self.interval:
            interval_used = self.interval
            candles = list(self.get_candles(interval_used))
        metrics = self._liquidity_tool.evaluate(candles)
        expected_bias = "LONG" if side.upper() == "BUY" else "SHORT"
        is_buy = side.upper() == "BUY"
        zone = metrics.entry_zone or {"low": 0.0, "high": 0.0}
        zone_low = float(zone.get("low", 0.0) or 0.0)
        zone_high = float(zone.get("high", 0.0) or 0.0)
        zone_valid = zone_high > zone_low
        entry_zone_ok = zone_valid and zone_low <= reference_price <= zone_high
        details = dict(metrics.details or {})
        long_trap = details.get("long_false_breakout_trap", {})
        short_trap = details.get("short_false_breakout_trap", {})
        trap_payload = long_trap if is_buy else short_trap
        trap_detected = bool(trap_payload.get("active"))
        bias_ok = metrics.trade_bias == expected_bias
        confidence_ok = metrics.confidence >= self.liquidity_min_confidence
        invalidate = float(metrics.invalidate_level or 0.0)
        invalidate_ok = (
            reference_price > invalidate if side.upper() == "BUY" else reference_price < invalidate
        )
        enabled = bool(self.enable_liquidity_filter)
        passed = (not enabled) or (not trap_detected)
        self._telemetry_log(
            "Liquidity filter evaluated before Dynamic ORB dispatch",
            level="INFO",
            tone="neutral" if passed else "warning",
            phase=self._PHASE_SIGNALS,
            details={
                "symbol": getattr(self, "symbol", "") or "",
                "stage": stage_name,
                "strategy_reason": reason,
                "side": side.upper(),
                "enabled": enabled,
                "passed": passed,
                "filter_mode": "anti_fake_breakout_only",
                "liquidity_interval": interval_used,
                "trade_bias": metrics.trade_bias,
                "entry_zone": {"low": zone_low, "high": zone_high},
                "invalidate_level": invalidate,
                "confidence": float(metrics.confidence),
                "checks": {
                    "trap_blocked": trap_detected,
                    "bias_match_advisory": bias_ok,
                    "confidence_ok_advisory": confidence_ok,
                    "entry_zone_ok_advisory": entry_zone_ok,
                    "invalidate_ok_advisory": invalidate_ok,
                },
                "active_trap": trap_payload if trap_detected else None,
                "reference_price": float(reference_price),
                "min_confidence": float(self.liquidity_min_confidence),
                "liquidity_details": details,
            },
            deduplicate=False,
            timestamp=timestamp,
        )
        return passed, metrics

    # ------------------------------------------------------------------
    async def on_candle(self, candle: Mapping[str, Any]) -> None:  # noqa: D401 - event hook
        close_time_debug = candle.get('close_time') or candle.get('timestamp') or candle.get('end')
        # self.logger.debug(f"DEBUG: on_candle called with {close_time_debug}")
        if not bool(candle.get("is_closed", True)):
            return
        
        close_time = _parse_timestamp(
            candle.get("close_time")
            or candle.get("timestamp")
            or candle.get("end")
            or candle.get("time")
        )

        if close_time:
             self._ensure_session(close_time)
             
        # Debug logging for session and stages
        if hasattr(self, "_current_session"):
            session_str = f"Session: {self._current_session}"
        else:
            session_str = "Session: None"
        
        stages_str = ""
        if hasattr(self, "_stage_cache"):
            stages_str = ", ".join([f"{k}:En={v.enabled}/Cmp={v.complete}/{v.range_high}/{v.range_low}/BO={v.active_breakout}" for k, v in self._stage_cache.items()])
            
        close_price_debug = candle.get("close")
        self.logger.debug(f"ORB Debug: {close_time_debug} | {session_str} | Stages: {stages_str} | Close: {close_price_debug}")

        if close_time is None:
            return

        # Add regular status heartbeat to clear "awaiting_data"
        self._telemetry_set_phase_status(
            self._PHASE_SIGNALS,
            status="running",
            status_code="processing_candles",
            status_reason="Processing incoming candles for ORB breakout detection",
            status_details={
                "symbol": self.symbol,
                "stage_count": len(self._stage_cache),
                "active_stages": sum(1 for stage in self._stage_cache.values() if stage.enabled),
            },
            timestamp=close_time if isinstance(close_time, datetime) else None,
        )

        high = _coerce_float(candle.get("high"), float("nan"))
        low = _coerce_float(candle.get("low"), float("nan"))
        close_price = _coerce_float(candle.get("close"), float("nan"))
        volume = _coerce_float(candle.get("volume"), 0.0)
        if not (
            math.isfinite(high)
            and math.isfinite(low)
            and math.isfinite(close_price)
        ):
            return
        self._ensure_session(close_time)
        candles = self.get_candles()
        atr_value = _calculate_atr(candles, int(self.atr_length))
        avg_volume = _moving_average_volume(candles, int(self.volume_ma_length))
        position_open = abs(self._current_position()) > 0.0
        volume_ok = self._check_volume_condition(volume, avg_volume)
        trend_ok_up = self._check_trend_condition(close_price, candles, "up")
        trend_ok_down = self._check_trend_condition(close_price, candles, "down")

        for name, _, _ in STAGE_CONFIG:
            stage = self._stage_cache.get(name)
            if stage is None:
                continue
            stage_changed = stage.consume(
                high=high,
                low=low,
                close=close_price,
                volume=volume,
                close_time=close_time,
            )
            if stage.complete:
                stage_changed |= stage.update_indicators(
                    atr=atr_value, avg_volume=avg_volume
                )
                if self.enable_breakout:
                    stage_changed |= stage.evaluate_breakout(
                        close_price,
                        self.breakout_buffer,
                        self.retest_buffer,
                        enable_retest=self.enable_retest,
                        volume_ok=volume_ok,
                        trend_ok_up=trend_ok_up,
                        trend_ok_down=trend_ok_down,
                    )
            if stage_changed:
                self._publish_stage_update(
                    stage, close_time, position_open, close_price, candle
                )

    # ------------------------------------------------------------------
    def _publish_stage_update(
        self,
        stage: ORBStageState,
        close_time: datetime,
        position_open: bool,
        close_price: float,
        candle: Mapping[str, Any],
    ) -> None:
        status_code = None
        payload = stage.log_payload()
        snapshot_for_log = dict(payload)
        previous_snapshot = stage.last_log_snapshot
        changed_keys = {
            key
            for key, value in snapshot_for_log.items()
            if previous_snapshot.get(key) != value
        }
        changed_keys.update(key for key in previous_snapshot.keys() if key not in snapshot_for_log)
        indicator_only_keys = {
            "atr_ratio",
            "volume_ratio",
            f"{stage.name}.atr_ratio",
            f"{stage.name}.volume_ratio",
        }
        position_changed = (
            stage.last_position_open is not None
            and stage.last_position_open != position_open
        )
        should_log = not previous_snapshot
        if not should_log:
            if changed_keys - indicator_only_keys:
                should_log = True
            elif position_changed:
                should_log = True
        if should_log:
            details = dict(snapshot_for_log)
            details["timestamp"] = close_time.isoformat()
            status_code, cause_code = self._stage_event_codes(
                stage, previous_snapshot, snapshot_for_log
            )
            
            # Only log significant events (status_code present) to reduce noise
            if not status_code:
                stage.last_log_snapshot = snapshot_for_log
                stage.last_position_open = position_open
                return

            if status_code:
                details["status_code"] = status_code
            if cause_code:
                details["cause_code"] = cause_code
            details.setdefault("status", stage.pipeline_status())
            details["stage_event"] = True
            details["stage"] = stage.name
            if status_code:
                self._record_stage_signal_event(stage, close_time)

            log_message = f"{stage.name} update"
            tone = "neutral"
            if status_code in ("breakout_up", "breakout_down"):
                direction = "UP" if status_code == "breakout_up" else "DOWN"
                trigger = stage.range_high if direction == "UP" else stage.range_low
                trigger_val = f"{trigger:.2f}" if trigger is not None else "N/A"
                log_message = f"Entry Signal: {stage.name} Breakout {direction} | Price: {close_price:.2f} | Trigger: {trigger_val} | Result: PASS"
                tone = "positive"
            elif status_code == "stage_completed":
                high_val = f"{stage.range_high:.2f}" if stage.range_high is not None else "N/A"
                low_val = f"{stage.range_low:.2f}" if stage.range_low is not None else "N/A"
                log_message = f"{stage.name} Stage Complete | Range: {low_val}-{high_val}"

            self._telemetry_log(
                log_message,
                phase=self._PHASE_SIGNALS,
                deduplicate=False,
                details=details,
                tone=tone,
            )
            stage.last_log_snapshot = snapshot_for_log
            stage.last_position_open = position_open
            
            # Debugging condition for order dispatch
            dispatch_event_exists = self._dispatch_event is not None
            replay_flag = getattr(self, "_history_replay_in_progress", False)
            self.logger.debug(f"ORB Dispatch Check: dispatch_exists={dispatch_event_exists}, pos_open={position_open}, status={status_code}, replay={replay_flag}")

            # Debug logging for order dispatch conditions
            replay_flag = getattr(self, "_history_replay_in_progress", False)
            if status_code in {"breakout_up", "breakout_down"}:
                self.logger.info(
                    f"ORB Breakout Detected: status={status_code}, "
                    f"pos_open={position_open}, "
                    f"replay={replay_flag}, "
                    f"dispatch_exists={self._dispatch_event is not None}"
                )

            if (
                not position_open
                and status_code in {"breakout_up", "breakout_down"}
                and not replay_flag
            ):
                if self._stage_ready_for_order(stage, close_time):
                    side = "BUY" if status_code == "breakout_up" else "SELL"
                    liquidity_ok, liquidity_metrics = self._evaluate_liquidity_gate(
                        side=side,
                        reference_price=float(close_price),
                        timestamp=close_time,
                        reason=f"dynamic_orb_{status_code}",
                        stage_name=stage.name,
                    )
                    if not liquidity_ok:
                        return
                    quantity = max(1, int(self.default_quantity))
                    order_payload: Dict[str, Any] = {
                        "side": side,
                        "quantity": float(quantity),
                        "order_type": "MARKET",
                        "symbol": self.symbol,
                        "reason": f"orb_breakout_{stage.name.lower()}",
                        "metadata": {
                            "stage": stage.name,
                            "range_high": float(stage.range_high or 0.0),
                            "range_low": float(stage.range_low or 0.0),
                            "atr_ratio": float(stage.atr_ratio or 0.0) if stage.atr_ratio is not None else None,
                            "volume_ratio": float(stage.volume_ratio or 0.0) if stage.volume_ratio is not None else None,
                            "breakout_state": stage.active_breakout,
                            "status_code": status_code,
                            "cause_code": cause_code,
                            "timestamp": close_time.isoformat(),
                            "quantity": float(quantity),
                            "liquidity_trade_bias": liquidity_metrics.trade_bias,
                            "liquidity_confidence": float(liquidity_metrics.confidence),
                            "liquidity_entry_zone": dict(liquidity_metrics.entry_zone),
                            "liquidity_invalidate_level": float(liquidity_metrics.invalidate_level),
                        },
                    }
                    exit_mode = getattr(getattr(self, "exit_config", None), "mode", ExitMode.NONE)
                    if exit_mode is not ExitMode.NONE:
                        order_payload["metadata"]["exit_mode"] = exit_mode.value
                        exit_targets = self.evaluate_exit_signal(
                            position=float(quantity)
                            * (1.0 if side == "BUY" else -1.0),
                            entry_price=float(close_price),
                            account_equity=getattr(self, "account_equity", None),
                            bar=candle,
                            is_dom=False,
                        )
                        if exit_targets is not None:
                            if exit_targets.stop_loss is not None:
                                order_payload["metadata"]["evaluated_stop_loss"] = float(
                                    exit_targets.stop_loss
                                )
                            if exit_targets.take_profit is not None:
                                order_payload["metadata"]["evaluated_take_profit"] = float(
                                    exit_targets.take_profit
                                )
                    
                    ok, reason = self.can_open_new_trade(side, float(quantity))
                    if not ok:
                        self._telemetry_log(
                            "Risk manager blocked ORB breakout",
                            level="WARN",
                            tone="warning",
                            phase=self._PHASE_DISPATCH,
                            details={"stage": stage.name, "reason": reason},
                        )
                        return

                    queued = self.queue_order(order_payload)
                    if queued:
                        self._last_ordered_stage = stage.name
                        self._last_ordered_at = close_time
                        self._telemetry_log(
                            "Queued ORB breakout order",
                            level="INFO",
                            tone="positive",
                            phase=self._PHASE_DISPATCH,
                            details={
                                "stage": stage.name,
                                "side": side,
                                "quantity": float(quantity),
                            },
                            deduplicate=False,
                        )
                    else:
                        self._telemetry_log(
                            "Failed to queue ORB breakout order",
                            level="WARN",
                            tone="warning",
                            phase=self._PHASE_DISPATCH,
                            details={
                                "stage": stage.name,
                                "side": side,
                                "quantity": float(quantity),
                            },
                            deduplicate=False,
                        )
        snapshot = stage.telemetry_payload()
        if snapshot != stage.last_metrics:
            stage_cache_snapshot: list[dict[str, Any]] = []
            for name, _, _ in STAGE_CONFIG:
                cached_stage = self._stage_cache.get(name)
                if cached_stage is None:
                    continue
                stage_cache_snapshot.append(cached_stage.log_payload())
            self._telemetry_update_phase_metrics(
                self._PHASE_AGGREGATION,
                snapshot,
                stage_cache=stage_cache_snapshot,
            )
            enriched = dict(snapshot)
            enriched["stage_cache"] = stage_cache_snapshot
            stage.last_metrics = enriched

    def _stage_event_codes(
        self,
        stage: ORBStageState,
        previous: Mapping[str, Any],
        current: Mapping[str, Any],
    ) -> tuple[str | None, str | None]:
        status_code: str | None = None
        cause_code: str | None = None
        stage_key = stage.name.lower()
        if current.get("complete") and not previous.get("complete"):
            status_code = "stage_completed"
            cause_code = f"orb/{stage_key}_complete"
        elif previous.get("breakouts_up") != current.get("breakouts_up"):
            status_code = "breakout_up"
            cause_code = f"orb/{stage_key}_breakout_up"
        elif previous.get("breakouts_down") != current.get("breakouts_down"):
            status_code = "breakout_down"
            cause_code = f"orb/{stage_key}_breakout_down"
        elif previous.get("retests") != current.get("retests"):
            status_code = "breakout_retest"
            cause_code = f"orb/{stage_key}_retest"
        elif previous.get("failures") != current.get("failures"):
            status_code = "breakout_failure"
            cause_code = f"orb/{stage_key}_failure"
        return status_code, cause_code
