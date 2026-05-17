from __future__ import annotations

import asyncio
import math
import time
import collections
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Mapping, Optional

from src.common.market_data import normalize_interval_token
from src.strategy.base import StrategyError
from src.data_layer import DataSubscriptionRequest
from src.common.market_data.aggregation import floor_timestamp as _floor_timestamp

from .buy_the_dip import DEFAULT_INSTRUMENT_DETAILS
from .candle import CandleSubscriptionStrategy, _interval_to_delta
from .indicator_utils import EmaTracker, coerce_float, parse_timestamp
from .templates import StrategyTemplate


@dataclass
class BRRPatternState:
    """State machine container for the Breakout-Retest-Rejection pattern."""

    direction: str
    stage: str = "idle"
    level: Optional[float] = None
    breakout_bar: Optional[Mapping[str, Any]] = None
    retest_bar: Optional[Mapping[str, Any]] = None
    bars_since_breakout: int = 0
    bars_since_retest: int = 0
    fib50: Optional[float] = None
    fib618: Optional[float] = None

    def reset(self) -> None:
        self.stage = "idle"
        self.level = None
        self.breakout_bar = None
        self.retest_bar = None
        self.bars_since_breakout = 0
        self.bars_since_retest = 0
        self.fib50 = None
        self.fib618 = None


@dataclass
class BRRStrategy(CandleSubscriptionStrategy, StrategyTemplate):
    """Breakout-Retest-Rejection scalping strategy supporting 1m/5m entries."""

    name: str = "BRR Breakout Strategy"
    strategy_type: str = "brr_strategy"
    description: str = (
        "基于Breakout-Retest-Rejection的剥头皮策略，结合多周期趋势确认与斐波那契汇合"
    )
    symbol: str = "BTCUSDT"
    interval: str = "1m"
    history_limit: int = 6000
    default_quantity: int = 1
    entry_timeframe: str = "1m"
    cooldown_seconds: float = 0.0
    level_lookback: int = 10
    breakout_body_ratio: float = 0.2
    breakout_buffer_pct: float = 0.0
    trend_tolerance: float = 0.0001
    retest_window: int = 30
    rejection_window: int = 20
    retest_tolerance_pct: float = 0.005
    weak_pullback_body_ratio: float = 0.7
    rejection_body_ratio: float = 0.05
    rejection_wick_ratio: float = 0.0
    use_fibonacci_confirmation: bool = False
    fib_tolerance_ratio: float = 0.2
    stop_buffer_pct: float = 0.001
    risk_reward_ratio: float = 1.5
    max_stop_loss_pct: float = 0.015
    debug: bool = True
    signal_frequency_seconds: float = 0.0  # Disable frequency check for scalping

    _history: Dict[str, Deque[Dict[str, Any]]] = field(default_factory=dict, init=False, repr=False)
    _trend_trackers: Dict[str, Dict[int, EmaTracker]] = field(default_factory=dict, init=False, repr=False)
    _trend_bias: Dict[str, Optional[str]] = field(default_factory=dict, init=False, repr=False)
    _pattern_state: Dict[str, BRRPatternState] = field(default_factory=dict, init=False, repr=False)
    _last_signal_monotonic: float = field(default=0.0, init=False, repr=False)
    _last_signal_bar: Optional[datetime] = field(default=None, init=False, repr=False)
    _last_signal_key: tuple[datetime, str] | None = field(default=None, init=False, repr=False)
    _entry_backfill_requested: Dict[str, bool] = field(default_factory=lambda: {"1m": False, "5m": False}, init=False, repr=False)
    _entry_backfill_next_attempt: Dict[str, float] = field(
        default_factory=lambda: {"1m": 0.0, "5m": 0.0},
        init=False,
        repr=False,
    )
    _entry_backfill_inflight: Dict[str, bool] = field(
        default_factory=lambda: {"1m": False, "5m": False},
        init=False,
        repr=False,
    )
    _entry_backfill_attempts: Dict[str, int] = field(
        default_factory=lambda: {"1m": 0, "5m": 0},
        init=False,
        repr=False,
    )
    _stats: Dict[str, int] = field(default_factory=lambda: collections.defaultdict(int), init=False, repr=False)
    waiting_log_min_interval: float = 10.0
    suppress_waiting_during_replay: bool = True
    _waiting_log_last_ts: Dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _waiting_log_last_bar: Dict[str, Optional[datetime]] = field(default_factory=dict, init=False, repr=False)
    summary_points: List[str] = field(default_factory=list, init=False)
    file_path: str = field(default="src/strategies/brr_strategy.py", init=False)

    def __post_init__(self) -> None:
        self.entry_timeframe = (self.entry_timeframe or "1m").lower()
        if self.entry_timeframe not in {"1m", "5m"}:
            self.entry_timeframe = "1m"
        self._entry_interval, self._trend_interval = self._resolve_mode_intervals()
        super().__post_init__()
        self._force_runtime_intervals(
            interval=self._entry_interval,
            intervals=[self._entry_interval, self._trend_interval],
        )

        self.default_quantity = max(1, int(self.default_quantity))
        self.entry_timeframe = (self.entry_timeframe or "1m").lower()
        if self.entry_timeframe not in {"1m", "5m"}:
            self.entry_timeframe = "1m"
        self.cooldown_seconds = max(0.0, float(self.cooldown_seconds))
        self.level_lookback = max(5, int(self.level_lookback))
        self.breakout_body_ratio = max(0.0, min(0.95, float(self.breakout_body_ratio)))
        self.breakout_buffer_pct = max(0.0, float(self.breakout_buffer_pct))
        self.trend_tolerance = max(0.0, float(self.trend_tolerance))
        self.retest_window = max(1, int(self.retest_window))
        self.rejection_window = max(1, int(self.rejection_window))
        self.retest_tolerance_pct = max(0.0, float(self.retest_tolerance_pct))
        self.weak_pullback_body_ratio = max(0.0, min(0.9, float(self.weak_pullback_body_ratio)))
        self.rejection_body_ratio = max(0.0, min(0.95, float(self.rejection_body_ratio)))
        self.rejection_wick_ratio = max(0.0, min(0.9, float(self.rejection_wick_ratio)))
        self.use_fibonacci_confirmation = bool(self.use_fibonacci_confirmation)
        self.fib_tolerance_ratio = max(0.0, float(self.fib_tolerance_ratio))
        self.stop_buffer_pct = max(0.0, float(self.stop_buffer_pct))
        self.risk_reward_ratio = max(0.0, float(self.risk_reward_ratio))
        self.summary_points = [
            "直接订阅入场周期与趋势周期K线，并按真实周期跟踪趋势偏向",
            "突破K线需具备强实体与有效突破缓冲，随后等待弱势回测与拒绝信号",
            "结合斐波那契50%/61.8%汇合与1:2风险回报控制入场，并对多空各自维护状态机",
        ]
        self.file_path = "src/strategies/brr_strategy.py"
        self._setup_state()
        self._register_parameters()

    def _bar_inactivity_threshold_seconds(self, interval: str | None = None) -> float:
        return max(super()._bar_inactivity_threshold_seconds(interval), 300.0)

    def apply_parameter_updates(self, updates: Mapping[str, Any]) -> Dict[str, Any]:
        sanitized = dict(updates)
        sanitized.pop("interval", None)
        sanitized.pop("intervals", None)
        applied = super().apply_parameter_updates(sanitized)
        if "entry_timeframe" in applied:
            self.entry_timeframe = (self.entry_timeframe or "1m").lower()
            if self.entry_timeframe not in {"1m", "5m"}:
                self.entry_timeframe = "1m"
            self._entry_interval, self._trend_interval = self._resolve_mode_intervals()
            try:
                self._force_runtime_intervals(
                    interval=self._entry_interval,
                    intervals=[self._entry_interval, self._trend_interval],
                )
                self._setup_state()
            except Exception as exc:
                self.logger.exception("Failed to re-initialize state after parameter update")
                raise StrategyError(f"Invalid parameters: {exc}") from exc

            if self.active and self._use_unified_data:
                try:
                    self._teardown_unified_subscription()
                    self._ensure_unified_subscription()
                except Exception as exc:  # pragma: no cover - defensive logging
                    self.logger.error("Failed to resubscribe after mode change: %s", exc)
        return applied

    def _resolve_mode_intervals(self) -> tuple[str, str]:
        entry_interval = self.entry_timeframe if self.entry_timeframe in {"1m", "5m"} else "1m"
        trend_interval = "15m" if entry_interval == "1m" else "30m"
        return entry_interval, trend_interval

    def _bootstrap_source_history_target(self) -> int:
        entry_interval, trend_interval = self._resolve_mode_intervals()
        trend_trackers = self._trend_trackers.get(trend_interval) or {}
        trend_period = max(trend_trackers.keys(), default=50)
        trend_warmup_bars = trend_period + 20
        trend_multiplier = 30 if trend_interval == "30m" else 15
        trend_source_bars = trend_warmup_bars * trend_multiplier

        entry_warmup_bars = max(
            int(self.level_lookback) + int(self.retest_window) + int(self.rejection_window) + 20,
            120,
        )
        entry_multiplier = 5 if entry_interval == "5m" else 1
        entry_source_bars = entry_warmup_bars * entry_multiplier

        baseline = max(trend_source_bars, entry_source_bars)
        return max(300, min(int(self.history_limit), baseline))

    async def on_start(self) -> None:
        """Initialize strategy and perform active history backfill."""
        await super().on_start()
        await self._await_market_data_ready_and_subscribe()
        if self._history_replay_in_progress:
            return

        entry_interval, trend_interval = self._resolve_mode_intervals()
        required_history = self._bootstrap_source_history_target()
        entry_multiplier = 5 if entry_interval == "5m" else 1
        with self._candles_lock:
            current_entry_bars = len(self._history.get(entry_interval, []))
        current_count = current_entry_bars * entry_multiplier

        if current_count < required_history:
            self.logger.info(
                "Active backfill requested",
                extra={
                    "current": current_count,
                    "required": required_history,
                    "entry_interval": entry_interval,
                    "symbol": self.symbol,
                },
            )
            
            # Calculate time range using the actual entry interval.
            delta = _interval_to_delta(entry_interval)
            now = datetime.now(timezone.utc)
            missing = required_history - current_count
            start_time = _floor_timestamp(now - (delta * missing), delta)
            
            request = DataSubscriptionRequest(
                channel=self._resolve_bar_channel(),
                symbol=self.symbol,
                interval=entry_interval,
                options={
                    "interval": entry_interval,
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
                    for item in records:
                        if not item:
                            continue
                        
                        # Normalize and ingest
                        # BRR uses _coerce_source_candle and _update_aggregations
                        normalized = self._coerce_source_candle(
                            {**item, "interval": entry_interval}
                        )
                        if normalized is None:
                            continue
                        self._history[entry_interval].append(normalized)
                        ingested_count += 1
                
                self.logger.info(
                        "Backfill completed",
                        extra={
                            "ingested": ingested_count,
                            "symbol": self.symbol
                        }
                    )
                
                # Force status update to running
                self._telemetry_set_phase_status(
                    self._PHASE_SIGNALS,
                    status="running",
                    status_code="monitoring",
                    status_reason="Initialized with historical data",
                    timestamp=now,
                )
            except Exception as e:
                self.logger.exception(
                    "Backfill failed",
                    extra={"error": str(e)}
                )
        
        # Ensure status is set even if backfill didn't run or failed
        self._telemetry_set_phase_status(
            self._PHASE_SIGNALS,
            status="running",
            status_code="monitoring",
            status_reason="Monitoring market data",
            timestamp=datetime.now(timezone.utc),
        )

    @staticmethod
    def _format_rule(
        current: float | int | str | None,
        threshold: float | int | str | None,
        op: str,
        passed: bool,
    ) -> str:
        def _fmt(value: float | int | str | None) -> str:
            if value is None:
                return "n/a"
            if isinstance(value, str):
                return value
            if isinstance(value, float):
                if math.isnan(value) or math.isinf(value):
                    return "n/a"
                return f"{value:.3f}"
            return str(value)

        return f"{_fmt(current)} {op} {_fmt(threshold)} -> {'PASS' if passed else 'FAIL'}"

    def _setup_state(self) -> None:
        self._entry_interval, self._trend_interval = self._resolve_mode_intervals()
        entry_interval, trend_interval = self._entry_interval, self._trend_interval
        self._history = {
            entry_interval: deque(maxlen=1200),
            trend_interval: deque(maxlen=400),
        }
        self._trend_trackers = {
            trend_interval: {period: EmaTracker(period) for period in (21, 50)}
        }
        self._trend_bias = {trend_interval: None}
        self._pattern_state = {
            "long": BRRPatternState(direction="long"),
            "short": BRRPatternState(direction="short"),
        }
        self._last_signal_monotonic = 0.0
        self._last_signal_bar = None
        self._entry_backfill_next_attempt = {"1m": 0.0, "5m": 0.0}

    def log_statistics(self) -> None:
        """Log internal statistics for debugging."""
        if not hasattr(self, "_stats"):
            return
        self.logger.info("=== BRR Strategy Internal Statistics ===")
        for key, value in self._stats.items():
            self.logger.info(f"  {key}: {value}")
        self.logger.info("========================================")

    def _register_parameters(self) -> None:
        symbol_default = (self.symbol or "").upper()
        definitions: Dict[str, Dict[str, Any]] = {
            "symbol": {
                "type": "str",
                "default": symbol_default,
                "label": "Symbol",
                "allow_null": True,
            },
            "interval": {
                "type": "str",
                "default": self._entry_interval,
                "readonly": True,
                "label": "Primary Interval",
            },
            "intervals": {
                "type": "list",
                "default": [self._entry_interval, self._trend_interval],
                "readonly": True,
                "label": "Subscribed Intervals",
            },
            "default_quantity": {
                "type": "int",
                "default": self.default_quantity,
                "min": 1,
                "max": 100,
                "label": "Order Quantity",
            },
            "entry_timeframe": {
                "type": "str",
                "default": self.entry_timeframe,
                "choices": ["1m", "5m"],
                "label": "Entry Timeframe",
            },
            "cooldown_seconds": {
                "type": "float",
                "default": self.cooldown_seconds,
                "min": 0.0,
                "max": 3600.0,
                "label": "Signal Cooldown (s)",
                "step": 5.0,
            },
            "level_lookback": {
                "type": "int",
                "default": self.level_lookback,
                "min": 5,
                "max": 120,
                "label": "Breakout Level Lookback",
            },
            "breakout_body_ratio": {
                "type": "float",
                "default": self.breakout_body_ratio,
                "min": 0.2,
                "max": 0.95,
                "step": 0.05,
                "label": "Breakout Body Ratio",
            },
            "breakout_buffer_pct": {
                "type": "float",
                "default": self.breakout_buffer_pct,
                "min": 0.0,
                "max": 0.01,
                "step": 0.0005,
                "label": "Breakout Buffer (%)",
            },
            "trend_tolerance": {
                "type": "float",
                "default": self.trend_tolerance,
                "min": 0.0,
                "max": 0.01,
                "step": 0.0005,
                "label": "Trend EMA Tolerance",
            },
            "retest_window": {
                "type": "int",
                "default": self.retest_window,
                "min": 1,
                "max": 10,
                "label": "Retest Window (bars)",
            },
            "rejection_window": {
                "type": "int",
                "default": self.rejection_window,
                "min": 1,
                "max": 10,
                "label": "Rejection Window (bars)",
            },
            "retest_tolerance_pct": {
                "type": "float",
                "default": self.retest_tolerance_pct,
                "min": 0.0,
                "max": 0.01,
                "step": 0.0005,
                "label": "Retest Tolerance (%)",
            },
            "weak_pullback_body_ratio": {
                "type": "float",
                "default": self.weak_pullback_body_ratio,
                "min": 0.0,
                "max": 0.9,
                "step": 0.05,
                "label": "Retest Body Ratio Max",
            },
            "rejection_body_ratio": {
                "type": "float",
                "default": self.rejection_body_ratio,
                "min": 0.2,
                "max": 0.95,
                "step": 0.05,
                "label": "Rejection Body Ratio Min",
            },
            "rejection_wick_ratio": {
                "type": "float",
                "default": self.rejection_wick_ratio,
                "min": 0.0,
                "max": 0.9,
                "step": 0.05,
                "label": "Rejection Wick Ratio Min",
            },
            "use_fibonacci_confirmation": {
                "type": "bool",
                "default": self.use_fibonacci_confirmation,
                "label": "Require Fibonacci Confirmation",
            },
            "fib_tolerance_ratio": {
                "type": "float",
                "default": self.fib_tolerance_ratio,
                "min": 0.0,
                "max": 1.0,
                "step": 0.05,
                "label": "Fibonacci Tolerance (range fraction)",
            },
            "stop_buffer_pct": {
                "type": "float",
                "default": self.stop_buffer_pct,
                "min": 0.0,
                "max": 0.01,
                "step": 0.0005,
                "label": "Stop Buffer (%)",
            },
            "risk_reward_ratio": {
                "type": "float",
                "default": self.risk_reward_ratio,
                "min": 0.5,
                "max": 5.0,
                "step": 0.1,
                "label": "Risk Reward Ratio",
            },
        }
        self.set_parameter_definitions(definitions)

    async def on_candle(self, candle: Mapping[str, Any]) -> None:
        # Add regular status heartbeat
        self._telemetry_set_phase_status(
            self._PHASE_SIGNALS,
            status="running",
            status_code="processing",
            status_reason="Processing candle data",
            status_details={"symbol": self.symbol},
            timestamp=datetime.now(timezone.utc),
        )

        normalized = self._coerce_source_candle(candle)
        if normalized is None:
            return
        entry_interval, trend_interval = self._resolve_mode_intervals()
        if entry_interval not in self._history:
            self._history[entry_interval] = deque(maxlen=1200)
        if trend_interval not in self._history:
            self._history[trend_interval] = deque(maxlen=400)
        if trend_interval not in self._trend_trackers:
            self._trend_trackers[trend_interval] = {period: EmaTracker(period) for period in (21, 50)}
        if trend_interval not in self._trend_bias:
            self._trend_bias[trend_interval] = None
        interval = normalize_interval_token(normalized.get("interval")) or entry_interval
        if interval == entry_interval:
            self._history[entry_interval].append(normalized)
            await self._handle_entry_candle(entry_interval, normalized)
        elif interval == trend_interval:
            self._history[trend_interval].append(normalized)
            self._update_trend(trend_interval, normalized)

    def _coerce_source_candle(self, candle: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            start = parse_timestamp(candle.get("start"))
            end = parse_timestamp(candle.get("end"))
        except Exception:
            return None
        open_price = coerce_float(candle.get("open"), default=float("nan"))
        high_price = coerce_float(candle.get("high"), default=float("nan"))
        low_price = coerce_float(candle.get("low"), default=float("nan"))
        close_price = coerce_float(candle.get("close"), default=float("nan"))
        volume = coerce_float(candle.get("volume"), default=0.0)
        if not all(math.isfinite(value) for value in (open_price, high_price, low_price, close_price)):
            return None
        interval = normalize_interval_token(candle.get("interval")) or self.entry_timeframe or "1m"
        return {
            "start": start,
            "end": end,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
            "interval": interval,
        }

    def _update_trend(self, timeframe: str, candle: Mapping[str, Any]) -> None:
        self.logger.debug(f"_update_trend called for {timeframe}")
        trackers = self._trend_trackers.get(timeframe)
        if not trackers:
            self.logger.warning(f"No trackers found for {timeframe}")
            return
        close_price = candle["close"]
        ema21 = trackers[21].update(close_price)
        ema50 = trackers[50].update(close_price)
        if ema21 is None or ema50 is None:
            self.logger.info(f"EMA not ready. EMA21: {trackers[21].ready} ({len(trackers[21]._seed)}/{trackers[21].period}), EMA50: {trackers[50].ready} ({len(trackers[50]._seed)}/{trackers[50].period})")
            self._trend_bias[timeframe] = None
            return
        tolerance = abs(ema50) * self.trend_tolerance
        
        self.logger.debug(f"Trend Check {timeframe}: EMA21={ema21:.2f}, EMA50={ema50:.2f}, Tol={tolerance:.2f}, Close={close_price:.2f}")

        if ema21 - ema50 > tolerance: # and close_price > ema50:
            self._trend_bias[timeframe] = "long"
        elif ema50 - ema21 > tolerance: # and close_price < ema50:
            self._trend_bias[timeframe] = "short"
        else:
            self._trend_bias[timeframe] = None

        
        # Debug logging
        if self._trend_bias[timeframe] is not None:
             self.logger.info(f"Trend updated: {timeframe} -> {self._trend_bias[timeframe]} (EMA21={ema21:.2f}, EMA50={ema50:.2f}, Close={close_price:.2f})")


    async def _handle_entry_candle(self, timeframe: str, candle: Mapping[str, Any]) -> None:
        history = self._history.get(timeframe)
        if not history:
            return

        # Add regular heartbeat to ensure status is not "awaiting_data"
        ts = candle.get("end")
        self._telemetry_set_phase_status(
            self._PHASE_SIGNALS,
            status="running",
            status_code="monitoring",
            status_reason="Monitoring for breakouts",
            status_details={
                "symbol": self.symbol,
                "timeframe": timeframe,
                "history_len": len(history),
            },
            timestamp=ts if isinstance(ts, datetime) else None,
        )

        bias_timeframe = "15m" if timeframe == "1m" else "30m"
        bias = self._trend_bias.get(bias_timeframe)
        bias_token = bias if bias in {"long", "short"} else None
        
        # Debug logging
        self.logger.debug(f"Checking entry: {timeframe}, Bias: {bias} (Token: {bias_token})")

        if bias_token is None:
            ts = None
            try:
                ts = candle.get("end")
            except Exception:
                ts = None
            trackers = self._trend_trackers.get(bias_timeframe) or {}
            required = max(trackers.keys(), default=0)
            have_bars = len(self._history.get(bias_timeframe, []))
            
            # Fix: Update status to running even if trend is neutral, to clear "awaiting_data"
            self._telemetry_set_phase_status(
                self._PHASE_SIGNALS,
                status="running",
                status_code="trend_neutral",
                status_reason=f"Trend neutral on {bias_timeframe}",
                status_details={
                    "symbol": getattr(self, "symbol", "") or "",
                    "timeframe": timeframe,
                    "bias_timeframe": bias_timeframe,
                    "have_bars": have_bars,
                    "need_bars": required,
                },
                timestamp=ts if isinstance(ts, datetime) else None,
            )

            if have_bars < required:
                reason_msg = f"等待{bias_timeframe}历史数据 ({have_bars}/{required})"
                comparison_type = "bars >="
                wait_metric: float | str | None = float(have_bars)
                wait_threshold: float | str | None = float(required)
                condition_summary = "bars >= required"
                bias_display = bias or "neutral"
            else:
                reason_msg = f"{bias_timeframe}趋势不明朗 (震荡或反转)"
                comparison_type = "bias ∈"
                wait_metric = bias or "neutral"
                wait_threshold = "long/short"
                condition_summary = "bias ∈ {long, short}"
                bias_display = bias or "neutral"

            self._telemetry_log_signal_waiting(
                step="趋势确认",
                reason=reason_msg,
                metric=wait_metric,
                threshold=wait_threshold,
                comparison=comparison_type,
                details={
                    "symbol": getattr(self, "symbol", "") or "",
                    "timeframe": timeframe,
                    "bias_timeframe": bias_timeframe,
                    "have_bars": have_bars,
                    "need_bars": required,
                    "trend_status": "neutral",
                    "bias": bias_display,
                    "condition": condition_summary,
                },
                timestamp=ts if isinstance(ts, datetime) else None,
            )
            try:
                tag = f"trend_bias_missing.{timeframe}"
                if self._should_log_waiting(tag, candle):
                    self.logger.info(
                        "BRR waiting for higher timeframe trend confirmation",
                        extra={
                            "event": "strategy.signal.waiting",
                            "strategy": self.name,
                            "symbol": getattr(self, "symbol", "") or "",
                            "timeframe": timeframe,
                            "bias_timeframe": bias_timeframe,
                            "have_bars": have_bars,
                            "need_bars": required,
                        },
                    )
            except Exception:
                pass
            
            # Fix: Do not reset pattern state if we are just warming up trend data
            # If we have less than 90% of required bars, we assume we are still backfilling/warming up.
            if have_bars < required * 0.9:
                return

            for state in self._pattern_state.values():
                state.reset()
            return
        for state in self._pattern_state.values():
            desired_bias = state.direction
            if desired_bias == "long" and bias_token == "short":
                details = {
                    "symbol": getattr(self, "symbol", "") or "",
                    "timeframe": timeframe,
                    "bias_timeframe": bias_timeframe,
                    "desired": desired_bias,
                    "current_bias": bias or "neutral",
                }
                try:
                    tag = f"trend_bias_mismatch.{timeframe}.long"
                    if self._should_log_waiting(tag, candle):
                        try:
                            self._telemetry_log_phase_detail(
                                phase=self._PHASE_SIGNALS,
                                message="趋势偏向与入场方向不一致，重置状态",
                                level="WARN",
                                tone="warning",
                                details=details,
                            )
                        except Exception:
                            pass
                        self.logger.debug(
                            "BRR signal gated by trend bias",
                            extra={
                                "event": "strategy.signal.waiting",
                                "strategy": self.name,
                                **details,
                            },
                        )
                except Exception:
                    pass
                state.reset()
                continue
            if desired_bias == "short" and bias_token == "long":
                details = {
                    "symbol": getattr(self, "symbol", "") or "",
                    "timeframe": timeframe,
                    "bias_timeframe": bias_timeframe,
                    "desired": desired_bias,
                    "current_bias": bias or "neutral",
                }
                try:
                    tag = f"trend_bias_mismatch.{timeframe}.short"
                    if self._should_log_waiting(tag, candle):
                        try:
                            self._telemetry_log_phase_detail(
                                phase=self._PHASE_SIGNALS,
                                message="趋势偏向与入场方向不一致，重置状态",
                                level="WARN",
                                tone="warning",
                                details=details,
                            )
                        except Exception:
                            pass
                        self.logger.debug(
                            "BRR signal gated by trend bias",
                            extra={
                                "event": "strategy.signal.waiting",
                                "strategy": self.name,
                                **details,
                            },
                        )
                except Exception:
                    pass
                state.reset()
                continue
            await self._update_pattern(state, timeframe, candle)

    def evaluate_exit_signal(
        self,
        *,
        position: float,
        entry_price: float,
        account_equity: float | None = None,
        bar: Mapping[str, Any] | None = None,
        is_dom: bool = False,
    ) -> Any:
        """Evaluate exit signals based on BRR pattern invalidation."""

        if abs(position) < 1e-9:
            return None

        # If we have a pattern state tracking this trade, we could check for invalidation.
        # For BRR, invalidation usually happens before entry (during retest).
        # Once entered, we rely on Fixed RR (SL/TP) managed by the engine/broker.
        # However, if we wanted to implement early exit:
        
        # Example: If price moves against us significantly but hasn't hit SL, 
        # and we see a reversal signal, we could exit.
        
        # For now, return None to use the default Fixed RR logic (managed by engine via exit_config).
        return None

    def log_statistics(self) -> None:
        """Log internal statistics for debugging."""
        self.logger.info("=== BRR Strategy Statistics ===")
        for key, value in sorted(self._stats.items()):
            self.logger.info(f"  {key}: {value}")
        self.logger.info("===============================")

    async def _update_pattern(
        self,
        state: BRRPatternState,
        timeframe: str,
        candle: Mapping[str, Any],
    ) -> None:
        history = self._history.get(timeframe)

        if history is None:
            return
        required = self._required_history_bars(timeframe)
        if len(history) < required:
            if state.stage != "idle":
                ts = None
                try:
                    ts = candle.get("end")
                except Exception:
                    ts = None
                self._telemetry_log_signal_waiting(
                    step="BRR突破检测",
                    reason="历史K线不足，重置BRR状态",
                    metric=float(len(history)),
                    threshold=float(required),
                    comparison="bars",
                    details={
                        "direction": state.direction,
                        "have_bars": len(history),
                        "need_bars": required,
                        "symbol": self.symbol,
                        "timeframe": timeframe,
                        "bar_end": ts.isoformat() if isinstance(ts, datetime) else None,
                    },
                    timestamp=ts if isinstance(ts, datetime) else None,
                )
                state.reset()
            return
        if state.stage == "idle":
            breakout = await self._detect_breakout(state.direction, history, candle, timeframe)
            if breakout is None:
                return
            state.stage = "await_retest"
            state.level = breakout["level"]
            state.breakout_bar = dict(candle)
            state.retest_bar = None
            state.bars_since_breakout = 0
            state.bars_since_retest = 0
            state.fib50, state.fib618 = self._compute_fibonacci_levels(state)
            return
        if state.stage == "await_retest":
            state.bars_since_breakout += 1
            if state.bars_since_breakout > self.retest_window:
                try:
                    self._telemetry_log_phase_detail(
                        phase=self._PHASE_SIGNALS,
                        message="回测窗口超时，重置BRR状态",
                        level="WARN",
                        tone="warning",
                        details={
                            "symbol": getattr(self, "symbol", "") or "",
                            "timeframe": timeframe,
                            "window": int(self.retest_window),
                            "bars_since_breakout": int(state.bars_since_breakout),
                            "direction": state.direction,
                        },
                    )
                except Exception:
                    pass
                try:
                    tag = f"retest_timeout.{timeframe}.{state.direction}"
                    if self._should_log_waiting(tag, candle):
                        self.logger.debug(
                            "BRR retest window timed out",
                            extra={
                                "event": "strategy.signal.waiting",
                                "strategy": self.name,
                                "symbol": getattr(self, "symbol", "") or "",
                                "timeframe": timeframe,
                                "window": int(self.retest_window),
                                "bars_since_breakout": int(state.bars_since_breakout),
                                "direction": state.direction,
                            },
                        )
                except Exception:
                    pass
                state.reset()
                return
            if self._is_retest(state, candle):
                if self.use_fibonacci_confirmation and not self._passes_fibonacci(state, candle):
                    try:
                        self._telemetry_log_phase_detail(
                            phase=self._PHASE_SIGNALS,
                            message="斐波那契汇合未满足，重置BRR状态",
                            level="WARN",
                            tone="warning",
                            details={
                                "symbol": getattr(self, "symbol", "") or "",
                                "timeframe": timeframe,
                                "direction": state.direction,
                                "fib50": state.fib50 if state.fib50 is not None else None,
                                "fib618": state.fib618 if state.fib618 is not None else None,
                            },
                        )
                    except Exception:
                        pass
                try:
                    tag = f"fib_failed.{timeframe}.{state.direction}"
                    if self._should_log_waiting(tag, candle):
                        self.logger.debug(
                            "BRR fibonacci confirmation failed",
                            extra={
                                "event": "strategy.signal.waiting",
                                "strategy": self.name,
                                "symbol": getattr(self, "symbol", "") or "",
                                "timeframe": timeframe,
                                "direction": state.direction,
                                "fib50": state.fib50 if state.fib50 is not None else None,
                                "fib618": state.fib618 if state.fib618 is not None else None,
                            },
                        )
                except Exception:
                    pass
                state.reset()
                return
            if self._is_rejection(state, candle):
                self._execute_entry(state, timeframe, candle)
                state.reset()
            else:
                state.stage = "await_rejection"
                state.retest_bar = dict(candle)
                state.bars_since_retest = 0
            return
        if state.stage == "await_rejection":
            state.bars_since_retest += 1
            if state.bars_since_retest > self.rejection_window:
                try:
                    self._telemetry_log_phase_detail(
                        phase=self._PHASE_SIGNALS,
                        message="拒绝窗口超时，重置BRR状态",
                        level="WARN",
                        tone="warning",
                        details={
                            "symbol": getattr(self, "symbol", "") or "",
                            "timeframe": timeframe,
                            "window": int(self.rejection_window),
                            "bars_since_retest": int(state.bars_since_retest),
                            "direction": state.direction,
                        },
                    )
                except Exception:
                    pass
                try:
                    tag = f"rejection_timeout.{timeframe}.{state.direction}"
                    if self._should_log_waiting(tag, candle):
                        self.logger.debug(
                            "BRR rejection window timed out",
                            extra={
                                "event": "strategy.signal.waiting",
                                "strategy": self.name,
                                "symbol": getattr(self, "symbol", "") or "",
                                "timeframe": timeframe,
                                "window": int(self.rejection_window),
                                "bars_since_retest": int(state.bars_since_retest),
                                "direction": state.direction,
                            },
                        )
                except Exception:
                    pass
                state.reset()
                return
            if self._is_rejection(state, candle):
                self._execute_entry(state, timeframe, candle)
                state.reset()

    async def _detect_breakout(
        self,
        direction: str,
        history: Deque[Dict[str, Any]],
        candle: Mapping[str, Any],
        timeframe: str,
    ) -> Optional[Dict[str, float]]:
        required = self._required_history_bars(timeframe)
        if len(history) < required:
            ts = None
            try:
                ts = candle.get("end")
            except Exception:
                ts = None
            bar_end_value = ts.isoformat() if isinstance(ts, datetime) else None
            now_monotonic = self._monotonic_now()
            next_attempt = self._entry_backfill_next_attempt.get(timeframe, 0.0)
            if now_monotonic < next_attempt:
                return None
            if self._entry_backfill_inflight.get(timeframe, False):
                return None
            try:
                self._entry_backfill_inflight[timeframe] = True
                have = len(history)
                missing = max(required - have, 1)
                minutes_per_bar = 1 if timeframe == "1m" else 5
                delta = _interval_to_delta(timeframe)
                now = datetime.now(timezone.utc)
                start = _floor_timestamp(now - (delta * missing * minutes_per_bar), delta)
                channel = self._resolve_bar_channel()
                request = DataSubscriptionRequest(
                    channel=channel,
                    symbol=self.symbol,
                    interval=timeframe,
                    options={
                        "interval": timeframe,
                        "start": start,
                        "end": now,
                    },
                )
                config = self._history_replay_config()
                records = await self._load_history_records(
                    request=request,
                    start=start,
                    end=now,
                    interval=delta,
                    config=config,
                )
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
                    if closed:
                        # Handle list return from _ingest_bar_payload
                        closed_events = closed if isinstance(closed, list) else [closed]
                        aggregated.extend(closed_events)
                leftovers = self._flush_unified_bucket(close_partial=True)
                if leftovers:
                    aggregated.extend(leftovers)
                if aggregated:
                    anchor = datetime.min.replace(tzinfo=timezone.utc)
                    aggregated.sort(
                        key=lambda item: self._extract_candle_end(item) or anchor
                    )
                previous_replay_state = self._history_replay_in_progress
                self._history_replay_in_progress = True
                self._last_processed_candle_start = None
                self._last_processed_candle_end = None
                try:
                    for snapshot in aggregated:
                        normalised = self._normalise_candle(snapshot, is_closed=True)
                        if normalised is not None:
                            await self._invoke_candle_handlers(normalised)
                finally:
                    self._history_replay_in_progress = previous_replay_state
                have_after = len(history)
                if records and have_after >= required:
                    self._entry_backfill_attempts[timeframe] = 0
                    self._entry_backfill_next_attempt[timeframe] = 0.0
                else:
                    attempts = self._entry_backfill_attempts.get(timeframe, 0) + 1
                    self._entry_backfill_attempts[timeframe] = attempts
                    base_delay = max(1.0, float(getattr(self, "history_retry_delay", 2.0)))
                    backoff = max(1.0, float(getattr(self, "history_retry_backoff", 2.0)))
                    delay = max(30.0, base_delay * (backoff ** max(0, attempts - 1)))
                    self._entry_backfill_next_attempt[timeframe] = now_monotonic + delay
            except Exception:
                attempts = self._entry_backfill_attempts.get(timeframe, 0) + 1
                self._entry_backfill_attempts[timeframe] = attempts
                base_delay = max(1.0, float(getattr(self, "history_retry_delay", 2.0)))
                backoff = max(1.0, float(getattr(self, "history_retry_backoff", 2.0)))
                delay = max(30.0, base_delay * (backoff ** max(0, attempts - 1)))
                self._entry_backfill_next_attempt[timeframe] = now_monotonic + delay
                self.logger.debug("Entry backfill attempt failed", exc_info=True)
            finally:
                self._entry_backfill_inflight[timeframe] = False
            self._telemetry_log_signal_waiting(
                step="BRR突破检测",
                reason="等待足够的历史K线以检测突破",
                metric=float(len(history)),
                threshold=float(required),
                comparison="bars",
                details={
                    "direction": direction,
                    "have_bars": len(history),
                    "need_bars": required,
                    "symbol": self.symbol,
                    "timeframe": timeframe,
                    "bar_end": bar_end_value,
                },
                timestamp=ts if isinstance(ts, datetime) else None,
            )
            try:
                tag = f"history_wait.{timeframe}.{direction}"
                if self._should_log_waiting(tag, candle):
                    self.logger.debug(
                        "BRR waiting for sufficient candle history",
                        extra={
                            "event": "strategy.signal.waiting",
                            "strategy": self.name,
                            "symbol": self.symbol,
                            "timeframe": timeframe,
                            "direction": direction,
                            "have_bars": len(history),
                            "need_bars": required,
                        },
                    )
            except Exception:
                pass
            return None
        try:
            ts = candle.get("end")
        except Exception:
            ts = None
        self._telemetry_set_phase_status(
            self._PHASE_SIGNALS,
            status="running",
            status_code="history_ready",
            status_reason="历史K线已满足信号检测条件",
            status_details={
                "direction": direction,
                "have_bars": len(history),
                "need_bars": required,
                "symbol": self.symbol,
                "timeframe": timeframe,
            },
            timestamp=ts if isinstance(ts, datetime) else None,
        )
        if self._last_signal_wait_state is not None:
            self._telemetry_set_phase_status(
                self._PHASE_SIGNALS,
                status="running",
                status_code="history_ready",
                status_reason="历史K线已满足信号检测条件",
                status_details={
                    "direction": direction,
                    "have_bars": len(history),
                    "need_bars": required,
                    "symbol": self.symbol,
                    "timeframe": timeframe,
                },
                timestamp=ts,
            )
            self._telemetry_log_processing_step(
                step="历史K线满足",
                metric=float(len(history)),
                threshold=float(required),
                comparison="bars",
                passed=True,
                stage=self._PHASE_SIGNALS,
                details={
                    "direction": direction,
                    "have_bars": len(history),
                    "need_bars": required,
                    "symbol": self.symbol,
                    "timeframe": timeframe,
                    "bar_end": ts.isoformat() if isinstance(ts, datetime) else None,
                },
                timestamp=ts if isinstance(ts, datetime) else None,
            )
            self._telemetry_log_phase_detail(
                phase=self._PHASE_SIGNALS,
                message="历史K线已满足信号检测条件",
                level="INFO",
                tone="positive",
                details={
                    "direction": direction,
                    "have_bars": len(history),
                    "need_bars": required,
                    "symbol": self.symbol,
                    "timeframe": timeframe,
                    "bar_end": ts.isoformat() if isinstance(ts, datetime) else None,
                },
                timestamp=ts if isinstance(ts, datetime) else None,
            )
            self._last_signal_wait_state = None
        previous = list(history)[-required:-1]
        close_price = candle["close"]
        open_price = candle["open"]
        high_price = candle["high"]
        low_price = candle["low"]
        range_size = high_price - low_price
        body = close_price - open_price
        body_ratio = abs(body) / range_size if range_size > 0 else None
        body_ratio_display = f"{body_ratio:.2f}" if body_ratio is not None else "n/a"
        ts = None
        try:
            ts = candle.get("end")
        except Exception:
            ts = None
        if direction == "long":
            level = max(item["high"] for item in previous)
            threshold_level = level * (1 + self.breakout_buffer_pct)
            cond_breakout = close_price > threshold_level
            cond_body_ratio = body_ratio is not None and body_ratio >= self.breakout_body_ratio
            cond_body_sign = body > 0
            cond_body = cond_body_ratio and cond_body_sign
            cond_strength = range_size > 0 and close_price >= high_price - range_size * 0.5
            passed = cond_breakout and cond_body and cond_strength
            
            self._stats["breakout_candidates"] += 1
            if cond_breakout:
                self._stats["breakout_price_condition"] += 1
                if passed:
                    self._stats["breakout_confirmed"] += 1
            
            if cond_breakout and not passed:
                if not cond_body:
                    self._stats["reject_breakout_body"] += 1
                if not cond_strength:
                    self._stats["reject_breakout_strength"] += 1
                self.logger.debug(f"Breakout candidate rejected (Long): Level={level}, Close={close_price}, BodyRatio={body_ratio_display}(req {self.breakout_body_ratio}), Strength={cond_strength}")
            
            try:
                tag = f"breakout_eval.{timeframe}.long"
                if self._should_log_waiting(tag, candle):
                    message = (
                        "突破检测(做多): close>=level*(1+buffer) & body_ratio>=threshold & body>0 & "
                        f"close>=high-0.2*range -> {'PASS' if passed else 'FAIL'}"
                    )
                    self._telemetry_log_phase_detail(
                        phase=self._PHASE_SIGNALS,
                        message=message,
                        level="INFO" if passed else "WARN",
                        tone="positive" if passed else "warning",
                        details={
                            "condition": "close>=level*(1+buffer) & body_ratio>=threshold & body>0 & close>=high-0.2*range",
                            "cond.breakout": self._format_rule(
                                close_price,
                                threshold_level,
                                ">=",
                                cond_breakout,
                            ),
                            "cond.body_ratio": self._format_rule(
                                body_ratio if body_ratio is not None else "n/a",
                                self.breakout_body_ratio,
                                ">=",
                                cond_body_ratio,
                            ),
                            "cond.body_sign": self._format_rule(
                                body,
                                0,
                                ">",
                                cond_body_sign,
                            ),
                            "cond.strength": self._format_rule(
                                close_price,
                                (high_price - range_size * 0.2),
                                ">=",
                                cond_strength,
                            ),
                            "current.close": close_price,
                            "level": level,
                            "threshold": threshold_level,
                            "body_ratio": body_ratio,
                            "body_ratio_threshold": self.breakout_body_ratio,
                            "range": range_size,
                            "result": "PASS" if passed else "FAIL",
                            "timeframe": timeframe,
                            "symbol": self.symbol,
                            "bar_end": ts.isoformat() if isinstance(ts, datetime) else None,
                        },
                        timestamp=ts if isinstance(ts, datetime) else None,
                    )
            except Exception:
                pass
            if not passed:
                return None
            return {"level": level, "body_ratio": body_ratio or 0.0}
        level = min(item["low"] for item in previous)
        threshold_level = level * (1 - self.breakout_buffer_pct)
        cond_breakout = close_price < threshold_level
        cond_body_ratio = body_ratio is not None and body_ratio >= self.breakout_body_ratio
        cond_body_sign = body < 0
        cond_body = cond_body_ratio and cond_body_sign
        cond_strength = range_size > 0 and close_price <= low_price + range_size * 0.5
        passed = cond_breakout and cond_body and cond_strength

        self._stats["breakout_candidates"] += 1
        if cond_breakout:
            self._stats["breakout_price_condition"] += 1
            if passed:
                self._stats["breakout_confirmed"] += 1

        if cond_breakout and not passed:
            if not cond_body:
                self._stats["reject_breakout_body"] += 1
            if not cond_strength:
                self._stats["reject_breakout_strength"] += 1
            self.logger.info(f"Breakout candidate rejected (Short): Level={level}, Close={close_price}, BodyRatio={body_ratio_display}(req {self.breakout_body_ratio}), Strength={cond_strength}")

        try:
            tag = f"breakout_eval.{timeframe}.short"
            if self._should_log_waiting(tag, candle):
                message = (
                    "突破检测(做空): close<=level*(1-buffer) & body_ratio>=threshold & body<0 & "
                    f"close<=low+0.2*range -> {'PASS' if passed else 'FAIL'}"
                )
                self._telemetry_log_phase_detail(
                    phase=self._PHASE_SIGNALS,
                    message=message,
                    level="INFO" if passed else "WARN",
                    tone="positive" if passed else "warning",
                    details={
                        "condition": "close<=level*(1-buffer) & body_ratio>=threshold & body<0 & close<=low+0.2*range",
                        "cond.breakout": self._format_rule(
                            close_price,
                            threshold_level,
                            "<=",
                            cond_breakout,
                        ),
                        "cond.body_ratio": self._format_rule(
                            body_ratio if body_ratio is not None else "n/a",
                            self.breakout_body_ratio,
                            ">=",
                            cond_body_ratio,
                        ),
                        "cond.body_sign": self._format_rule(
                            body,
                            0,
                            "<",
                            cond_body_sign,
                        ),
                        "cond.strength": self._format_rule(
                            close_price,
                            (low_price + range_size * 0.2),
                            "<=",
                            cond_strength,
                        ),
                        "current.close": close_price,
                        "level": level,
                        "threshold": threshold_level,
                        "body_ratio": body_ratio,
                        "body_ratio_threshold": self.breakout_body_ratio,
                        "range": range_size,
                        "result": "PASS" if passed else "FAIL",
                        "timeframe": timeframe,
                        "symbol": self.symbol,
                        "bar_end": ts.isoformat() if isinstance(ts, datetime) else None,
                    },
                    timestamp=ts if isinstance(ts, datetime) else None,
                )
        except Exception:
            pass
        if not passed:
            return None
        return {"level": level, "body_ratio": body_ratio or 0.0}

    def _required_history_bars(self, timeframe: str) -> int:
        required = self.level_lookback + 1
        bias_warmup = self._bias_warmup_bars(timeframe)
        if bias_warmup is not None:
            required = max(required, bias_warmup)
        return required

    def _bias_warmup_bars(self, timeframe: str) -> int | None:
        if timeframe == "1m":
            return 15 * 50
        if timeframe == "5m":
            return 6 * 50
        return None

    def _compute_fibonacci_levels(self, state: BRRPatternState) -> tuple[Optional[float], Optional[float]]:
        breakout = state.breakout_bar
        level = state.level
        if not breakout or level is None:
            return (None, None)
        if state.direction == "long":
            high = breakout["high"]
            impulse = max(high - level, 0.0)
            if impulse <= 0:
                return (level, level)
            return (high - impulse * 0.5, high - impulse * 0.618)
        low = breakout["low"]
        impulse = max(level - low, 0.0)
        if impulse <= 0:
            return (level, level)
        return (low + impulse * 0.5, low + impulse * 0.618)

    def _is_retest(self, state: BRRPatternState, candle: Mapping[str, Any]) -> bool:
        level = state.level
        if level is None:
            return False
        tolerance = max(abs(level) * self.retest_tolerance_pct, 1e-9)
        low = candle["low"]
        high = candle["high"]
        close_price = candle["close"]
        open_price = candle["open"]
        range_size = high - low
        body_ratio = (abs(close_price - open_price) / range_size) if range_size > 0 else 1.0
        cond_range = range_size > 0
        cond_body_ratio = body_ratio <= self.weak_pullback_body_ratio
        
        self._stats["retest_checks"] += 1

        if state.direction == "long":
            cond_touch = low <= level * (1 + self.retest_tolerance_pct)
            cond_close_above = close_price >= level - tolerance
            result = cond_range and cond_body_ratio and cond_touch and cond_close_above
        else:
            cond_touch = high >= level * (1 - self.retest_tolerance_pct)
            cond_close_below = close_price <= level + tolerance
            result = cond_range and cond_body_ratio and cond_touch and cond_close_below
            
        if result:
            self._stats["retest_passed"] += 1
        else:
            if not cond_range:
                self._stats["reject_retest_range"] += 1
            if not cond_body_ratio:
                self._stats["reject_retest_body"] += 1
            if not cond_touch:
                self._stats["reject_retest_touch"] += 1
            if state.direction == "long" and not cond_close_above:
                self._stats["reject_retest_close"] += 1
            if state.direction == "short" and not cond_close_below:
                self._stats["reject_retest_close"] += 1

        if not result:
            evaluations = [
                {"condition": "range>0", "current": range_size, "passed": cond_range},
                {"condition": f"weak_pullback_body_ratio<={self.weak_pullback_body_ratio}", "current": body_ratio, "passed": cond_body_ratio},
            ]
            if state.direction == "long":
                evaluations.extend([
                    {"condition": f"low<=level*(1+{self.retest_tolerance_pct})", "current": {"low": low, "level": level}, "passed": cond_touch},
                    {"condition": f"close>=level-{tolerance:.3f}", "current": {"close": close_price, "level": level, "tolerance": tolerance}, "passed": cond_close_above},
                ])
            else:
                evaluations.extend([
                    {"condition": f"high>=level*(1-{self.retest_tolerance_pct})", "current": {"high": high, "level": level}, "passed": cond_touch},
                    {"condition": f"close<=level+{tolerance:.3f}", "current": {"close": close_price, "level": level, "tolerance": tolerance}, "passed": cond_close_below},
                ])
            self._telemetry_set_phase_status(self._PHASE_SIGNALS, status="evaluated", status_code="conditions_checked")
            self._telemetry_log(
                "BRR retest conditions evaluated",
                level="INFO",
                tone="neutral",
                phase=self._PHASE_SIGNALS,
                details={
                    "symbol": getattr(self, "symbol", "") or "",
                    "direction": state.direction,
                    "level": level,
                    "evaluations": evaluations,
                },
                deduplicate=False,
            )
        return result

    def _passes_fibonacci(self, state: BRRPatternState, candle: Mapping[str, Any]) -> bool:
        fibs = [value for value in (state.fib50, state.fib618) if value is not None]
        if not fibs:
            return True
        if state.breakout_bar is None or state.level is None:
            return True
        reference = candle["low"] if state.direction == "long" else candle["high"]
        if state.direction == "long":
            impulse = max(state.breakout_bar["high"] - state.level, 0.0)
        else:
            impulse = max(state.level - state.breakout_bar["low"], 0.0)
        tolerance = max(impulse * self.fib_tolerance_ratio, abs(state.level) * self.retest_tolerance_pct)
        for value in fibs:
            if abs(reference - value) <= tolerance:
                return True
        return False

    def _is_rejection(self, state: BRRPatternState, candle: Mapping[str, Any]) -> bool:
        level = state.level
        if level is None:
            return False
        
        # Use tolerance similar to retest
        tolerance = max(abs(level) * self.retest_tolerance_pct, 1e-9)
        
        close_price = candle["close"]
        open_price = candle["open"]
        high = candle["high"]
        low = candle["low"]
        range_size = high - low
        body = close_price - open_price
        body_ratio = (abs(body) / range_size) if range_size > 0 else 0.0
        cond_range = range_size > 0
        cond_body_ratio = body_ratio >= self.rejection_body_ratio
        
        self._stats["rejection_checks"] += 1
        
        if state.direction == "long":
            cond_body_pos = body > 0
            # Relaxed close condition
            cond_close_above = close_price >= level - tolerance
            lower_wick = min(open_price, close_price) - low
            cond_wick = (lower_wick / range_size) if range_size > 0 else 0.0
            cond_wick_ok = cond_wick >= self.rejection_wick_ratio
            
            # Allow red body if wick is strong (e.g. > 2x required)
            strong_wick = cond_wick >= (self.rejection_wick_ratio * 2.0)
            cond_sign_or_strong_wick = cond_body_pos or strong_wick
            
            result = cond_range and cond_body_ratio and cond_sign_or_strong_wick and cond_close_above and cond_wick_ok
        else:
            cond_body_neg = body < 0
            # Relaxed close condition
            cond_close_below = close_price <= level + tolerance
            upper_wick = high - max(open_price, close_price)
            cond_wick = (upper_wick / range_size) if range_size > 0 else 0.0
            cond_wick_ok = cond_wick >= self.rejection_wick_ratio
            
            strong_wick = cond_wick >= (self.rejection_wick_ratio * 2.0)
            cond_sign_or_strong_wick = cond_body_neg or strong_wick

            result = cond_range and cond_body_ratio and cond_sign_or_strong_wick and cond_close_below and cond_wick_ok
            
        if result:
            self._stats["rejection_passed"] += 1
        else:
            if not cond_range: self._stats["reject_rejection_range"] += 1
            if not cond_body_ratio: self._stats["reject_rejection_body"] += 1
            if state.direction == "long":
                if not cond_sign_or_strong_wick: self._stats["reject_rejection_sign"] += 1
                if not cond_close_above: self._stats["reject_rejection_close"] += 1
            else:
                if not cond_sign_or_strong_wick: self._stats["reject_rejection_sign"] += 1
                if not cond_close_below: self._stats["reject_rejection_close"] += 1
            if not cond_wick_ok: self._stats["reject_rejection_wick"] += 1
            
            # Detailed rejection logging
            self.logger.info(
                f"Rejection failed ({state.direction}): Level={level:.2f}, Close={close_price:.2f}, "
                f"Range={range_size:.2f}, BodyRatio={body_ratio:.2f}(>={self.rejection_body_ratio}), "
                f"WickRatio={cond_wick if 'cond_wick' in locals() else 0.0:.2f}(>={self.rejection_wick_ratio}), "
                f"Result: R={cond_range}, B={cond_body_ratio}, S={cond_sign_or_strong_wick}, C={cond_close_above if state.direction == 'long' else cond_close_below}, W={cond_wick_ok}"
            )

        if not result:
            self._telemetry_set_phase_status(self._PHASE_SIGNALS, status="evaluated", status_code="conditions_checked")
            self._telemetry_log(
                "BRR rejection conditions evaluated",
                level="INFO",
                tone="neutral",
                phase=self._PHASE_SIGNALS,
                details={
                    "symbol": getattr(self, "symbol", "") or "",
                    "direction": state.direction,
                    "level": level,
                    "cond.range": self._format_rule(range_size, 0, ">", cond_range),
                    "cond.body_ratio": self._format_rule(
                        body_ratio,
                        self.rejection_body_ratio,
                        ">=",
                        cond_body_ratio,
                    ),
                    "cond.body_sign": self._format_rule(
                        body,
                        0,
                        ">" if state.direction == "long" else "<",
                        cond_body_pos if state.direction == "long" else cond_body_neg,
                    ),
                    "cond.close": self._format_rule(
                        close_price,
                        level,
                        ">" if state.direction == "long" else "<",
                        cond_close_above if state.direction == "long" else cond_close_below,
                    ),
                    "cond.wick_ratio": self._format_rule(
                        cond_wick,
                        self.rejection_wick_ratio,
                        ">=",
                        cond_wick_ok,
                    ),
                },
                deduplicate=False,
            )
        return result

    def _execute_entry(
        self,
        state: BRRPatternState,
        timeframe: str,
        candle: Mapping[str, Any],
    ) -> None:
        last_bar = getattr(self, "_last_signal_bar", None)
        try:
            ts = candle.get("end")
        except Exception:
            ts = None
        if isinstance(ts, str):
            try:
                ts = parse_timestamp(ts)
            except Exception:
                ts = None
        if last_bar is not None and ts is not None and last_bar == ts:
            return
        if ts is not None:
            last_key = getattr(self, "_last_signal_key", None)
            current_key = (ts, state.direction)
            if last_key == current_key:
                return
        if not self._check_cooldown(candle):
            self.logger.info(f"Entry skipped: Cooldown active. Seconds: {float(self.cooldown_seconds)}")
            self._telemetry_log_signal_waiting(
                step="BRR冷却",
                reason="等待冷却计时结束以触发新的BRR入场信号",
                details={
                    "timeframe": timeframe,
                    "direction": state.direction,
                    "cooldown_seconds": float(self.cooldown_seconds),
                },
            )
            return
        entry_price = candle["close"]
        if entry_price <= 0:
            self.logger.info("Entry skipped: Price <= 0")
            return
        level = state.level or entry_price
        buffer = entry_price * self.stop_buffer_pct
        if state.direction == "long":
            anchor_low = min(
                level,
                candle["low"],
                (state.retest_bar or candle)["low"],
            )
            stop_price = anchor_low - buffer
            risk = entry_price - stop_price
            if risk <= 0:
                self.logger.info("Entry skipped: Risk <= 0")
                return
            if risk / entry_price > self.max_stop_loss_pct:
                self.logger.info(
                    f"⚠️ Entry skipped (Long): Stop loss too wide ({risk/entry_price:.2%}). "
                    f"Max allowed: {self.max_stop_loss_pct:.2%}. "
                    f"Risk: {risk:.2f}, Price: {entry_price:.2f}"
                )
                if self.debug:
                    self.logger.info(
                        f"⚠️ Entry skipped: Stop loss too wide ({risk/entry_price:.2%}). "
                        f"Max allowed: {self.max_stop_loss_pct:.2%}. "
                        f"Risk: {risk:.2f}, Price: {entry_price:.2f}"
                    )
                    if "_stats" in self.__dict__:
                        self._stats["skipped_max_stop"] = self._stats.get("skipped_max_stop", 0) + 1
                return
            take_profit = entry_price + risk * self.risk_reward_ratio if self.risk_reward_ratio > 0 else None
        else:
            anchor_high = max(
                level,
                candle["high"],
                (state.retest_bar or candle)["high"],
            )
            stop_price = anchor_high + buffer
            risk = stop_price - entry_price
            if risk <= 0:
                self.logger.info("Entry skipped: Risk <= 0")
                return
            if risk / entry_price > self.max_stop_loss_pct:
                self.logger.info(
                    f"⚠️ Entry skipped (Short): Stop loss too wide ({risk/entry_price:.2%}). "
                    f"Max allowed: {self.max_stop_loss_pct:.2%}. "
                    f"Risk: {risk:.2f}, Price: {entry_price:.2f}"
                )
                if self.debug:
                    self.logger.info(
                        f"⚠️ Entry skipped: Stop loss too wide ({risk/entry_price:.2%}). "
                        f"Max allowed: {self.max_stop_loss_pct:.2%}. "
                        f"Risk: {risk:.2f}, Price: {entry_price:.2f}"
                    )
                    if "_stats" in self.__dict__:
                        self._stats["skipped_max_stop"] = self._stats.get("skipped_max_stop", 0) + 1
                return
            take_profit = entry_price - risk * self.risk_reward_ratio if self.risk_reward_ratio > 0 else None
        
        self.logger.info(f"Submitting Order: {state.direction} at {entry_price}")
        metrics = {
            "level": state.level or level,
            "fib50": state.fib50 if state.fib50 is not None else None,
            "fib618": state.fib618 if state.fib618 is not None else None,
            "range": candle["high"] - candle["low"],
        }
        extra = {
            "pattern_timeframe": timeframe,
            "direction": state.direction,
        }
        bias_timeframe = "15m" if timeframe == "1m" else "30m"
        extra["trend_bias"] = self._trend_bias.get(bias_timeframe) or "neutral"
        self._submit_order(
            side="BUY" if state.direction == "long" else "SELL",
            entry=entry_price,
            stop=stop_price,
            target=take_profit,
            candle=candle,
            reason=f"brr_{state.direction}_entry",
            metrics=metrics,
            extra_tags=extra,
        )

    def _check_cooldown(self, candle: Optional[Mapping[str, Any]] = None) -> bool:
        if self.cooldown_seconds <= 0:
            return True
            
        # Use candle time if available (essential for simulation)
        if candle is not None:
            current_ts = candle.get("end")
            if isinstance(current_ts, str):
                current_ts = parse_timestamp(current_ts)
            
            last_ts = getattr(self, "_last_signal_ts", None)
            if current_ts and last_ts:
                if isinstance(last_ts, str):
                    last_ts = parse_timestamp(last_ts)
                # Ensure we compare timezone-aware or both naive
                if getattr(current_ts, "tzinfo", None) and not getattr(last_ts, "tzinfo", None):
                     last_ts = last_ts.replace(tzinfo=current_ts.tzinfo)
                elif not getattr(current_ts, "tzinfo", None) and getattr(last_ts, "tzinfo", None):
                     current_ts = current_ts.replace(tzinfo=last_ts.tzinfo)
                     
                delta = (current_ts - last_ts).total_seconds()
                if delta < self.cooldown_seconds:
                    return False
                return True
            elif current_ts and not last_ts:
                # First trade or no last signal timestamp recorded yet
                return True

        # Fallback to monotonic time for live trading if candle not provided
        now = time.monotonic()
        last_monotonic = getattr(self, "_last_signal_monotonic", 0)
        if now - last_monotonic < self.cooldown_seconds:
            return False
        return True

    def _submit_order(
        self,
        *,
        side: str,
        entry: float,
        stop: Optional[float],
        target: Optional[float],
        candle: Mapping[str, Any],
        reason: str,
        metrics: Mapping[str, Optional[float]],
        extra_tags: Optional[Mapping[str, Any]] = None,
    ) -> None:
        # Check if we can open a new trade
        if not self.can_open_new_trade(side, quantity=1):
            return
        
        quantity = self._determine_quantity(entry)
        if quantity <= 0:
            return
        exchange, sec_type = self._resolve_instrument(self.symbol)
        metadata: Dict[str, Any] = {
            "entry_price": float(entry),
            "interval_end": candle["end"].isoformat(),
        }
        if stop is not None and stop > 0:
            metadata["stop_loss"] = float(stop)
        if target is not None and target > 0:
            metadata["take_profit"] = float(target)
        for key, value in metrics.items():
            if value is None:
                continue
            metadata[key] = float(value)
        if extra_tags:
            for key, value in extra_tags.items():
                metadata[key] = value
        order_payload = {
            "side": side,
            "quantity": float(quantity),
            "order_type": "MARKET",
            "symbol": self.symbol,
            "exchange": exchange,
            "sec_type": sec_type,
            "reason": reason,
            "metadata": metadata,
        }
        if not self.queue_order(order_payload):
            block = self.pop_last_order_block() or {}
            block_code = str(block.get("code") or "").strip().lower()
            log_method = self.logger.info if block_code == "history_replay" else self.logger.warning
            extra = {
                "event": "strategy.order.queue_failed",
                "strategy": self.name,
                "symbol": self.symbol,
                "side": side,
                "quantity": quantity,
            }
            if block:
                extra["block"] = dict(block)
            log_method(
                "Failed to queue BRR order",
                extra=extra,
            )
            return
        ts = candle.get("end")
        self._last_signal_bar = ts
        self._last_signal_ts = ts
        direction = (extra_tags or {}).get("direction", side)
        if ts is not None:
            self._last_signal_key = (ts, direction)
        log_extra = {
            "event": "strategy.order.signal",
            "strategy": self.name,
            "symbol": self.symbol,
            "side": side,
            "quantity": float(quantity),
            "entry_price": float(entry),
        }
        if stop is not None and stop > 0:
            log_extra["stop_loss"] = float(stop)
        if target is not None and target > 0:
            log_extra["take_profit"] = float(target)
        self.logger.info("BRR order queued", extra=log_extra)
        self._last_signal_monotonic = time.monotonic()

    def _should_log_waiting(self, tag: str, candle: Mapping[str, Any]) -> bool:
        if self.suppress_waiting_during_replay and getattr(self, "_history_replay_in_progress", False):
            return False
        now = time.monotonic()
        last_ts = self._waiting_log_last_ts.get(tag, 0.0)
        if now - last_ts < max(0.0, float(self.waiting_log_min_interval)):
            return False
        try:
            end = candle.get("end")
        except Exception:
            end = None
        last_bar = self._waiting_log_last_bar.get(tag)
        if last_bar is not None and end is not None and last_bar == end:
            return False
        self._waiting_log_last_ts[tag] = now
        self._waiting_log_last_bar[tag] = end
        return True

    def _determine_quantity(self, price: float) -> int:
        return max(1, int(self.default_quantity))

    def _resolve_instrument(self, symbol: str) -> tuple[str, str]:
        base = (symbol or "").upper()
        if base in DEFAULT_INSTRUMENT_DETAILS:
            details = DEFAULT_INSTRUMENT_DETAILS[base]
            return details.get("exchange", "CME"), details.get("sec_type", "FUT")
        for key, details in DEFAULT_INSTRUMENT_DETAILS.items():
            if base.startswith(key):
                return details.get("exchange", "CME"), details.get("sec_type", "FUT")
        return "SMART", "STK"

    def evaluate_exit_signal(
        self,
        *,
        position: float,
        entry_price: float,
        account_equity: float | None = None,
        bar: Mapping[str, Any] | None = None,
        is_dom: bool = False,
    ) -> Any:
        """Evaluate exit signals based on BRR pattern invalidation."""
        # If we have a pattern state tracking this trade, we could check for invalidation.
        # For BRR, invalidation usually happens before entry (during retest).
        # Once entered, we rely on Fixed RR (SL/TP) managed by the engine/broker.
        
        # Call super to allow standard exit logic (Fixed RR, ATR, etc.) to run
        return super().evaluate_exit_signal(
            position=position,
            entry_price=entry_price,
            account_equity=account_equity,
            bar=bar,
            is_dom=is_dom,
        )
