from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Mapping, Optional

from src.common.market_data import normalize_interval_token
from src.common.pricing_defaults import resolve_index_future_point_value
from src.data_layer import DataSubscriptionRequest
from src.common.market_data.aggregation import floor_timestamp as _floor_timestamp
from src.strategy.exit import ExitConfig, ExitEvaluationResult, ExitMode

from .buy_the_dip import DEFAULT_INSTRUMENT_DETAILS
from .candle import CandleSubscriptionStrategy
from .indicator_utils import EmaTracker, RsiTracker, coerce_float, parse_timestamp
from .liquidity_tool import LiquidityFilterConfig, LiquidityFilterMetrics, LiquidityStrategyTool
from .templates import StrategyTemplate


@dataclass
class FiveMinuteMomentumStrategy(CandleSubscriptionStrategy, StrategyTemplate):
    """Trend-following strategy combining H1 bias with 5m triggers."""

    name: str = "5m Momentum Trend Following"
    strategy_type: str = "five_minute_momentum"
    _PHASE_EXECUTION = "execution"
    use_base_exit = False
    description: str = (
        "基于1分钟数据聚合多周期，利用EMA顺序与RSI确认趋势后，在5分钟图上寻找动能K线顺势入场"
    )
    symbol: str = "BTCUSDT"
    interval: str = "1m"
    history_limit: int = 6000
    default_quantity: int = 1
    cooldown_seconds: float = 300.0
    rsi_period: int = 14
    rsi_long_threshold: float = 50.0
    rsi_short_threshold: float = 50.0
    ema_touch_lookback: int = 12
    ema_touch_tolerance: float = 0.0015
    momentum_body_ratio: float = 0.1
    momentum_push_threshold: float = 0.0
    stop_buffer_pct: float = 0.0008
    risk_reward_ratio: float = 2.0
    bias_tolerance: float = 0.0005
    min_hourly_bars: int = 200
    min_five_bars: int = 200
    allow_pyramiding: bool = False
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

    _history: Dict[str, Deque[Dict[str, Any]]] = field(default_factory=dict, init=False, repr=False)
    _ema_5m: Dict[int, EmaTracker] = field(default_factory=dict, init=False, repr=False)
    _ema_1h: Dict[int, EmaTracker] = field(default_factory=dict, init=False, repr=False)
    _hourly_bias_value: Optional[str] = field(default=None, init=False, repr=False)
    _hourly_ema_values: Dict[int, Optional[float]] = field(default_factory=dict, init=False, repr=False)
    _five_ema_values: Dict[int, Optional[float]] = field(default_factory=dict, init=False, repr=False)
    _rsi_5m: RsiTracker = field(init=False, repr=False)
    _candles_since_touch_long: Optional[int] = field(default=None, init=False, repr=False)
    _candles_since_touch_short: Optional[int] = field(default=None, init=False, repr=False)
    _last_signal_monotonic: float = field(default=0.0, init=False, repr=False)
    _last_signal_bar: Optional[datetime] = field(default=None, init=False, repr=False)
    _last_exit_time: Optional[datetime] = field(default=None, init=False, repr=False)
    _history_replay_in_progress: bool = field(default=False, init=False, repr=False)
    _hourly_backfill_requested: bool = field(default=False, init=False, repr=False)
    _hourly_backfill_failed: bool = field(default=False, init=False, repr=False)
    _five_backfill_requested: bool = field(default=False, init=False, repr=False)
    _five_backfill_failed: bool = field(default=False, init=False, repr=False)
    _hourly_backfill_next_attempt: float = field(default=0.0, init=False, repr=False)
    _five_backfill_next_attempt: float = field(default=0.0, init=False, repr=False)
    _hourly_backfill_attempts: int = field(default=0, init=False, repr=False)
    _five_backfill_attempts: int = field(default=0, init=False, repr=False)
    _last_backfill_error: Dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _active_stop_loss: Optional[float] = field(default=None, init=False, repr=False)
    _active_take_profit: Optional[float] = field(default=None, init=False, repr=False)
    _active_exit_entry_price: Optional[float] = field(default=None, init=False, repr=False)
    _active_exit_position_sign: int = field(default=0, init=False, repr=False)
    _exit_dispatched: bool = field(default=False, init=False, repr=False)
    _liquidity_tool: LiquidityStrategyTool = field(init=False, repr=False)
    summary_points: List[str] = field(default_factory=list, init=False)
    file_path: str = field(default="src/strategies/five_minute_momentum.py", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        self._force_runtime_intervals(interval="5m", intervals=["5m", "1h"])
        
        exit_config = getattr(self, "exit_config", None)
        if exit_config is None:
            self.exit_config = ExitConfig(mode=ExitMode.FIXED_RR)
        elif exit_config.mode == ExitMode.NONE:
            exit_config.mode = ExitMode.FIXED_RR

        self.default_quantity = max(1, int(self.default_quantity))
        self.cooldown_seconds = max(0.0, float(self.cooldown_seconds))
        self.rsi_period = max(2, int(self.rsi_period))
        self.rsi_long_threshold = float(self.rsi_long_threshold)
        self.rsi_short_threshold = float(self.rsi_short_threshold)
        self.ema_touch_lookback = max(1, int(self.ema_touch_lookback))
        self.ema_touch_tolerance = max(0.0, float(self.ema_touch_tolerance))
        self.momentum_body_ratio = max(0.0, min(1.0, float(self.momentum_body_ratio)))
        self.momentum_push_threshold = max(0.0, float(self.momentum_push_threshold))
        self.stop_buffer_pct = max(0.0, float(self.stop_buffer_pct))
        self.risk_reward_ratio = max(0.0, float(self.risk_reward_ratio))
        self.bias_tolerance = max(0.0, float(self.bias_tolerance))
        self.min_hourly_bars = max(1, int(self.min_hourly_bars))
        self.min_five_bars = max(1, int(self.min_five_bars))
        self.allow_pyramiding = bool(self.allow_pyramiding)
        self._configure_liquidity_filter()
        self.summary_points = [
            "直接订阅5分钟与1小时K线，按真实周期运行",
            "以1小时EMA偏向筛选多空方向，并在5分钟EMA顺序与RSI共振时寻找动能K线",
            "自动计算EMA/结构止损与1:2盈亏比目标并结合冷却时间控制频率",
        ]
        self.file_path = "src/strategies/five_minute_momentum.py"
        self._setup_state()
        self._register_parameters()

    def _setup_state(self) -> None:
        self._aggregators = {}
        self._history = {"5m": deque(maxlen=600), "1h": deque(maxlen=600)}
        self._ema_5m = {period: EmaTracker(period) for period in (21, 50, 200)}
        self._ema_1h = {period: EmaTracker(period) for period in (21, 50, 200)}
        self._hourly_ema_values = {period: None for period in (21, 50, 200)}
        self._five_ema_values = {period: None for period in (21, 50, 200)}
        self._rsi_5m = RsiTracker(self.rsi_period)
        self._hourly_bias_value = None
        self._candles_since_touch_long = None
        self._candles_since_touch_short = None
        self._last_signal_monotonic = 0.0
        self._last_signal_bar = None
        self._last_exit_time = None
        self._active_stop_loss = None
        self._active_take_profit = None
        self._active_exit_entry_price = None
        self._active_exit_position_sign = 0
        self._exit_dispatched = False

    def _clear_active_exit_levels(self) -> None:
        self._active_stop_loss = None
        self._active_take_profit = None
        self._active_exit_entry_price = None
        self._active_exit_position_sign = 0

    def _resolve_bar_source_interval(self, interval: str) -> str:
        return normalize_interval_token(interval) or self.interval

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
                "default": "5m",
                "readonly": True,
                "label": "Primary Interval",
            },
            "intervals": {
                "type": "list",
                "default": ["5m", "1h"],
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
            "cooldown_seconds": {
                "type": "float",
                "default": self.cooldown_seconds,
                "min": 0.0,
                "max": 3600.0,
                "label": "Signal Cooldown (s)",
                "step": 5.0,
            },
            "rsi_period": {
                "type": "int",
                "default": self.rsi_period,
                "min": 5,
                "max": 50,
                "label": "RSI Period",
            },
            "rsi_long_threshold": {
                "type": "float",
                "default": self.rsi_long_threshold,
                "min": 45.0,
                "max": 70.0,
                "step": 0.5,
                "label": "RSI Long Threshold",
            },
            "rsi_short_threshold": {
                "type": "float",
                "default": self.rsi_short_threshold,
                "min": 30.0,
                "max": 55.0,
                "step": 0.5,
                "label": "RSI Short Threshold",
            },
            "ema_touch_lookback": {
                "type": "int",
                "default": self.ema_touch_lookback,
                "min": 1,
                "max": 6,
                "label": "EMA Touch Lookback (bars)",
            },
            "ema_touch_tolerance": {
                "type": "float",
                "default": self.ema_touch_tolerance,
                "min": 0.0,
                "max": 0.01,
                "step": 0.0005,
                "label": "EMA Touch Tolerance",
            },
            "momentum_body_ratio": {
                "type": "float",
                "default": self.momentum_body_ratio,
                "min": 0.2,
                "max": 0.9,
                "step": 0.05,
                "label": "Momentum Body Ratio",
            },
            "momentum_push_threshold": {
                "type": "float",
                "default": self.momentum_push_threshold,
                "min": 0.0,
                "max": 0.01,
                "step": 0.0005,
                "label": "Close vs EMA Push Threshold",
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
            "bias_tolerance": {
                "type": "float",
                "default": self.bias_tolerance,
                "min": 0.0,
                "max": 0.01,
                "step": 0.0005,
                "label": "Hourly Bias Tolerance",
            },
            "allow_pyramiding": {
                "type": "bool",
                "default": self.allow_pyramiding,
                "label": "Allow Pyramiding",
            },
            "enable_liquidity_filter": {
                "type": "bool",
                "default": self.enable_liquidity_filter,
                "label": "Enable Liquidity Filter",
            },
            "liquidity_interval": {
                "type": "str",
                "default": self.liquidity_interval,
                "label": "Liquidity Interval",
            },
            "liquidity_lookback_bars": {
                "type": "int",
                "default": self.liquidity_lookback_bars,
                "min": 40,
                "max": 400,
                "label": "Liquidity Lookback Bars",
            },
            "liquidity_atr_period": {
                "type": "int",
                "default": self.liquidity_atr_period,
                "min": 5,
                "max": 60,
                "label": "Liquidity ATR Period",
            },
            "liquidity_swing_window": {
                "type": "int",
                "default": self.liquidity_swing_window,
                "min": 1,
                "max": 8,
                "label": "Liquidity Swing Window",
            },
            "liquidity_eq_tolerance_ticks": {
                "type": "float",
                "default": self.liquidity_eq_tolerance_ticks,
                "min": 0.5,
                "max": 12.0,
                "step": 0.5,
                "label": "EQ Tolerance (ticks)",
            },
            "liquidity_min_penetration_ticks": {
                "type": "float",
                "default": self.liquidity_min_penetration_ticks,
                "min": 0.5,
                "max": 12.0,
                "step": 0.5,
                "label": "Min Sweep Penetration (ticks)",
            },
            "liquidity_max_reclaim_bars": {
                "type": "int",
                "default": self.liquidity_max_reclaim_bars,
                "min": 1,
                "max": 8,
                "label": "Max Reclaim Bars",
            },
            "liquidity_displacement_atr_multiplier": {
                "type": "float",
                "default": self.liquidity_displacement_atr_multiplier,
                "min": 0.2,
                "max": 3.0,
                "step": 0.1,
                "label": "Displacement ATR Multiplier",
            },
            "liquidity_structure_lookback": {
                "type": "int",
                "default": self.liquidity_structure_lookback,
                "min": 2,
                "max": 30,
                "label": "Displacement Structure Lookback",
            },
            "liquidity_tick_size": {
                "type": "float",
                "default": self.liquidity_tick_size,
                "min": 0.0001,
                "max": 10.0,
                "step": 0.0001,
                "label": "Liquidity Tick Size",
            },
            "liquidity_invalidate_buffer_ticks": {
                "type": "float",
                "default": self.liquidity_invalidate_buffer_ticks,
                "min": 0.0,
                "max": 8.0,
                "step": 0.5,
                "label": "Invalidate Buffer (ticks)",
            },
            "liquidity_min_confidence": {
                "type": "float",
                "default": self.liquidity_min_confidence,
                "min": 0.0,
                "max": 1.0,
                "step": 0.05,
                "label": "Liquidity Min Confidence",
            },
        }
        self.set_parameter_definitions(definitions)

    def apply_parameter_updates(self, updates: Mapping[str, Any]) -> Dict[str, Any]:
        applied = super().apply_parameter_updates(updates)
        if "interval" in updates or "intervals" in updates:
            self.interval = "5m"
            self.intervals = ["5m", "1h"]
            self._refresh_interval_state()
            applied["interval"] = self.interval
            applied["intervals"] = list(self.intervals)
        if "interval" in applied or "intervals" in applied or "symbol" in applied:
            self._setup_state()
        if any(key.startswith("liquidity_") for key in applied) or "enable_liquidity_filter" in applied:
            self._configure_liquidity_filter()
        return applied

    def _configure_liquidity_filter(self) -> None:
        self.enable_liquidity_filter = bool(self.enable_liquidity_filter)
        self.liquidity_interval = normalize_interval_token(self.liquidity_interval) or "1m"
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

    def _required_hourly_bars(self) -> int:
        return max(int(self.min_hourly_bars), 100)

    async def on_start(self) -> None:
        await super().on_start()
        await self._await_market_data_ready_and_subscribe()
        if self._history_replay_in_progress:
            return
        backfill = getattr(self, "_backfill_history_with_fallback", None)
        now_monotonic = self._monotonic_now()
        required_hourly = self._required_hourly_bars()
        if len(self._history.get("1h", [])) < required_hourly:
            if now_monotonic >= self._hourly_backfill_next_attempt and not self._hourly_backfill_requested:
                self._hourly_backfill_requested = True
                try:
                    if callable(backfill):
                        backfilled = backfill(
                            timeframe="1h",
                            min_bars=required_hourly,
                            minutes_per_bar=60,
                        )
                        if hasattr(backfilled, "__await__"):
                            backfilled = await backfilled
                    else:
                        backfilled = await self._backfill_history_from_data_layer(
                            timeframe="1h",
                            min_bars=required_hourly,
                            minutes_per_bar=60,
                        )
                finally:
                    self._hourly_backfill_requested = False
                if backfilled:
                    self._hourly_backfill_failed = False
                    have_after = len(self._history.get("1h", []))
                    if have_after >= required_hourly:
                        self._hourly_backfill_attempts = 0
                        self._hourly_backfill_next_attempt = 0.0
                    else:
                        self._hourly_backfill_attempts += 1
                        base_delay = max(1.0, float(getattr(self, "history_retry_delay", 2.0)))
                        backoff = max(1.0, float(getattr(self, "history_retry_backoff", 2.0)))
                        delay = max(30.0, base_delay * (backoff ** max(0, self._hourly_backfill_attempts - 1)))
                        self._hourly_backfill_next_attempt = now_monotonic + delay
                else:
                    self._hourly_backfill_failed = True
                    self._hourly_backfill_attempts += 1
                    base_delay = max(1.0, float(getattr(self, "history_retry_delay", 2.0)))
                    backoff = max(1.0, float(getattr(self, "history_retry_backoff", 2.0)))
                    delay = max(30.0, base_delay * (backoff ** max(0, self._hourly_backfill_attempts - 1)))
                    self._hourly_backfill_next_attempt = now_monotonic + delay
        if len(self._history.get("5m", [])) < self.min_five_bars:
            if now_monotonic >= self._five_backfill_next_attempt and not self._five_backfill_requested:
                self._five_backfill_requested = True
                try:
                    if callable(backfill):
                        backfilled = backfill(
                            timeframe="5m",
                            min_bars=self.min_five_bars,
                            minutes_per_bar=5,
                        )
                        if hasattr(backfilled, "__await__"):
                            backfilled = await backfilled
                    else:
                        backfilled = await self._backfill_history_from_data_layer(
                            timeframe="5m",
                            min_bars=self.min_five_bars,
                            minutes_per_bar=5,
                        )
                finally:
                    self._five_backfill_requested = False
                if backfilled:
                    self._five_backfill_failed = False
                    have_after = len(self._history.get("5m", []))
                    if have_after >= self.min_five_bars:
                        self._five_backfill_attempts = 0
                        self._five_backfill_next_attempt = 0.0
                    else:
                        self._five_backfill_attempts += 1
                        base_delay = max(1.0, float(getattr(self, "history_retry_delay", 2.0)))
                        backoff = max(1.0, float(getattr(self, "history_retry_backoff", 2.0)))
                        delay = max(30.0, base_delay * (backoff ** max(0, self._five_backfill_attempts - 1)))
                        self._five_backfill_next_attempt = now_monotonic + delay
                else:
                    self._five_backfill_failed = True
                    self._five_backfill_attempts += 1
                    base_delay = max(1.0, float(getattr(self, "history_retry_delay", 2.0)))
                    backoff = max(1.0, float(getattr(self, "history_retry_backoff", 2.0)))
                    delay = max(30.0, base_delay * (backoff ** max(0, self._five_backfill_attempts - 1)))
                    self._five_backfill_next_attempt = now_monotonic + delay
        if self._hourly_backfill_failed and len(self._history.get("1h", [])) < required_hourly:
            error_reason = self._last_backfill_error.get("1h")
            status_reason = "1小时历史K线回填失败"
            if error_reason:
                status_reason = f"{status_reason}: {error_reason}"
            self._telemetry_set_phase_status(
                self._PHASE_SIGNALS,
                status="error",
                status_code="data_unavailable",
                status_reason=status_reason,
                status_details={
                    "timeframe": "1h",
                    "have_bars": len(self._history.get("1h", [])),
                    "need_bars": required_hourly,
                    "symbol": self.symbol,
                    "error": error_reason,
                    "next_retry_seconds": max(
                        0.0, self._hourly_backfill_next_attempt - now_monotonic
                    ),
                },
            )
            self._telemetry_log_phase_detail(
                phase=self._PHASE_SIGNALS,
                message=status_reason,
                level="WARN",
                tone="warning",
                details={
                    "timeframe": "1h",
                    "have_bars": len(self._history.get("1h", [])),
                    "need_bars": required_hourly,
                    "symbol": self.symbol,
                    "error": error_reason,
                    "next_retry_seconds": max(
                        0.0, self._hourly_backfill_next_attempt - now_monotonic
                    ),
                },
            )
        if self._five_backfill_failed and len(self._history.get("5m", [])) < self.min_five_bars:
            error_reason = self._last_backfill_error.get("5m")
            status_reason = "5分钟历史K线回填失败"
            if error_reason:
                status_reason = f"{status_reason}: {error_reason}"
            self._telemetry_set_phase_status(
                self._PHASE_SIGNALS,
                status="error",
                status_code="data_unavailable",
                status_reason=status_reason,
                status_details={
                    "timeframe": "5m",
                    "have_bars": len(self._history.get("5m", [])),
                    "need_bars": self.min_five_bars,
                    "symbol": self.symbol,
                    "error": error_reason,
                    "next_retry_seconds": max(
                        0.0, self._five_backfill_next_attempt - now_monotonic
                    ),
                },
            )
            self._telemetry_log_phase_detail(
                phase=self._PHASE_SIGNALS,
                message=status_reason,
                level="WARN",
                tone="warning",
                details={
                    "timeframe": "5m",
                    "have_bars": len(self._history.get("5m", [])),
                    "need_bars": self.min_five_bars,
                    "symbol": self.symbol,
                    "error": error_reason,
                    "next_retry_seconds": max(
                        0.0, self._five_backfill_next_attempt - now_monotonic
                    ),
                },
            )

        # Update aggregation phase status if backfill provided data or if we are waiting for live data
        # This prevents "awaiting_data" from being displayed when we are actually just waiting for the next 1m bar
        if not self._hourly_backfill_requested and not self._five_backfill_requested:
             timestamp = datetime.now(timezone.utc)
             self._telemetry_set_phase_status(
                getattr(self, "_PHASE_AGGREGATION", "aggregation"),
                status="running",
                status_code="idle",
                status_reason="Waiting for live candle data",
                timestamp=timestamp,
            )
             self._telemetry_set_phase_status(
                self._PHASE_EXECUTION,
                status="running",
                status_code="idle",
                status_reason="Monitoring signals",
                timestamp=timestamp,
            )

    async def on_candle(self, candle: Mapping[str, Any]) -> None:
        normalized = self._coerce_source_candle(candle)
        if normalized is None:
            return
        interval = normalize_interval_token(candle.get("interval")) or self.interval
        if interval not in {"5m", "1h"}:
            return
        
        # Send execution heartbeat
        self._telemetry_set_phase_status(
            self._PHASE_EXECUTION,
            status="running",
            status_code="monitoring",
            status_reason="Monitoring signals",
            timestamp=datetime.now(timezone.utc),
        )

        backfill = getattr(self, "_backfill_history_with_fallback", None)
        required_hourly = self._required_hourly_bars()
        if (
            interval == "5m"
            and not self._history_replay_in_progress
            and len(self._history.get("1h", [])) < required_hourly
        ):
            now_monotonic = self._monotonic_now()
            if now_monotonic >= self._hourly_backfill_next_attempt and not self._hourly_backfill_requested:
                self._hourly_backfill_requested = True
                try:
                    if callable(backfill):
                        backfilled = await backfill(
                            timeframe="1h",
                            min_bars=required_hourly,
                            minutes_per_bar=60,
                        )
                    else:
                        backfilled = await self._backfill_history_from_data_layer(
                            timeframe="1h",
                            min_bars=required_hourly,
                            minutes_per_bar=60,
                        )
                finally:
                    self._hourly_backfill_requested = False
                if backfilled:
                    self._hourly_backfill_failed = False
                    have_after = len(self._history.get("1h", []))
                    if have_after >= required_hourly:
                        self._hourly_backfill_attempts = 0
                        self._hourly_backfill_next_attempt = 0.0
                    else:
                        self._hourly_backfill_attempts += 1
                        base_delay = max(1.0, float(getattr(self, "history_retry_delay", 2.0)))
                        backoff = max(1.0, float(getattr(self, "history_retry_backoff", 2.0)))
                        delay = max(30.0, base_delay * (backoff ** max(0, self._hourly_backfill_attempts - 1)))
                        self._hourly_backfill_next_attempt = now_monotonic + delay
                else:
                    self._hourly_backfill_failed = True
                    self._hourly_backfill_attempts += 1
                    base_delay = max(1.0, float(getattr(self, "history_retry_delay", 2.0)))
                    backoff = max(1.0, float(getattr(self, "history_retry_backoff", 2.0)))
                    delay = max(30.0, base_delay * (backoff ** max(0, self._hourly_backfill_attempts - 1)))
                    self._hourly_backfill_next_attempt = now_monotonic + delay
        if interval == "5m":
            self._history["5m"].append(normalized)
            await self._process_five_minute(normalized)
            return
        self._history["1h"].append(normalized)
        await self._process_hourly(normalized)

    def _maybe_execute_base_exit(self, candle: Mapping[str, Any]) -> None:
        interval = normalize_interval_token(candle.get("interval")) or normalize_interval_token(
            candle.get("timeframe")
        )
        # Evaluate exits only on closed 5m candles so ATR/SL/TP stay aligned
        # with the strategy signal timeframe.
        if interval != "5m":
            return
        was_dispatched = getattr(self, "_base_exit_dispatched", False)
        super()._maybe_execute_base_exit(candle)
        now_dispatched = getattr(self, "_base_exit_dispatched", False)
        if not was_dispatched and now_dispatched:
            ts = candle.get("end")
            if isinstance(ts, datetime):
                self._last_exit_time = ts
            else:
                self._last_exit_time = datetime.now(timezone.utc)

    def _coerce_source_candle(self, candle: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            start = parse_timestamp(candle.get("start"))
            end = parse_timestamp(candle.get("end"))
        except Exception:
            self.logger.debug("Failed to parse candle timestamps", extra={"symbol": self.symbol})
            return None
        open_price = coerce_float(candle.get("open"), default=float("nan"))
        high_price = coerce_float(candle.get("high"), default=float("nan"))
        low_price = coerce_float(candle.get("low"), default=float("nan"))
        close_price = coerce_float(candle.get("close"), default=float("nan"))
        volume = coerce_float(candle.get("volume"), default=0.0)
        if not all(math.isfinite(value) for value in (open_price, high_price, low_price, close_price)):
            return None
        return {
            "start": start,
            "end": end,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
        }

    def _update_aggregations(self, candle: Mapping[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
        if not hasattr(self, "_debug_agg_count"):
             self._debug_agg_count = 0
             self.logger.debug("DEBUG_STRATEGY: Aggregators keys: %s", list(self._aggregators.keys()))
        self._debug_agg_count += 1
        if self._debug_agg_count % 10 == 0:
             self.logger.debug("DEBUG_STRATEGY: Aggregating candle %s. Time=%s", self._debug_agg_count, candle.get('start'))

        timestamp = candle.get("start")
        if not isinstance(timestamp, datetime):
            timestamp = candle.get("end")
        record = {
            "timestamp": timestamp,
            "open": candle["open"],
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
            "volume": candle["volume"],
        }
        closed: Dict[str, List[Dict[str, Any]]] = {}
        self._telemetry_set_phase_status(
            getattr(self, "_PHASE_AGGREGATION", "aggregation"),
            status="running",
            status_code="active",
            status_reason="Processing subscribed candles",
            timestamp=timestamp if isinstance(timestamp, datetime) else None,
        )
        for token, aggregator in self._aggregators.items():
            buckets = aggregator.push(record, close_hint=True)
            if not buckets:
                continue
            formatted: List[Dict[str, Any]] = []
            for bucket in buckets:
                formatted_bucket = self._format_bucket(bucket, token)
                self._history[token].append(formatted_bucket)
                formatted.append(formatted_bucket)
            closed[token] = formatted
        return closed

    async def _backfill_history_from_data_layer(
        self, *, timeframe: str, min_bars: int, minutes_per_bar: int
    ) -> bool:
        try:
            have = len(self._history.get(timeframe, []))
            missing = max(min_bars - have, 1)
            token = (timeframe or "").strip().lower()
            if token.endswith("m") and token[:-1].isdigit():
                delta = timedelta(minutes=int(token[:-1]))
            elif token.endswith("h") and token[:-1].isdigit():
                delta = timedelta(hours=int(token[:-1]))
            else:
                delta = timedelta(minutes=max(1, minutes_per_bar))
            now = datetime.now(timezone.utc)
            target_bars = max(int(min_bars), int(missing))
            start = _floor_timestamp(now - (delta * target_bars), delta)
            
            # Resolve the correct channel for the specific timeframe
            # We must use _resolve_channel to ensure we target market.bar_1h for 1h data
            # instead of the default 1m channel
            base_channel = getattr(self, "data_layer_channel", "market.bar") or "market.bar"
            channel = self._resolve_channel(base_channel, timeframe)
            
            request = DataSubscriptionRequest(
                channel=channel,
                symbol=self.symbol,
                interval=timeframe,
                options={"interval": timeframe, "start": start, "end": now},
            )
            config = self._history_replay_config()
            try:
                records = await asyncio.wait_for(
                    self._load_history_records(
                        request=request,
                        start=start,
                        end=now,
                        interval=delta,
                        config=config,
                    ),
                    timeout=60.0
                )
            except asyncio.TimeoutError:
                records = []
                self.logger.warning("Backfill timed out for %s", timeframe)

            if not records:
                extended_missing = max(
                    int(min_bars) * 5,
                    int(min_bars) + 10,
                    int(missing) * 5,
                )
                extended_start = _floor_timestamp(now - (delta * extended_missing), delta)
                if extended_start < start:
                    try:
                        records = await asyncio.wait_for(
                            self._load_history_records(
                                request=request,
                                start=extended_start,
                                end=now,
                                interval=delta,
                                config=config,
                            ),
                            timeout=60.0
                        )
                    except asyncio.TimeoutError:
                         pass

            if not records:
                self._last_backfill_error[timeframe] = "empty_history"
                return False
            ingested_before = len(self._history.get(timeframe, []))
            previous_replay_state = self._history_replay_in_progress
            self._history_replay_in_progress = True
            ingest_limit = max(int(self.history_limit), int(min_bars))
            try:
                for item in list(records or [])[-ingest_limit:]:
                    if timeframe in {"5m", "1h"}:
                        normalized = self._coerce_source_candle(
                            {**item, "interval": timeframe}
                        )
                        if normalized is None:
                            continue
                        self._history[timeframe].append(normalized)
                        if timeframe == "1h":
                            await self._process_hourly(normalized)
                        else:
                            await self._process_five_minute(normalized)
                        continue
                    try:
                        start_ts = parse_timestamp(item.get("timestamp") or item.get("start"))
                    except Exception:
                        start_ts = None
                    if not isinstance(start_ts, datetime):
                        continue
                    start_ts = _floor_timestamp(start_ts, delta)
                    bucket = {
                        "start": start_ts,
                        "end": start_ts + delta,
                        "open": coerce_float(item.get("open")),
                        "high": coerce_float(item.get("high")),
                        "low": coerce_float(item.get("low")),
                        "close": coerce_float(item.get("close")),
                        "volume": coerce_float(item.get("volume"), default=0.0),
                        "interval": timeframe,
                    }
                    self._history[timeframe].append(bucket)
                    if timeframe == "1h":
                        await self._process_hourly(bucket)
                    elif timeframe == "5m":
                        await self._process_five_minute(bucket)
            finally:
                self._history_replay_in_progress = previous_replay_state
            return len(self._history.get(timeframe, [])) > ingested_before
        except Exception as exc:
            self._last_backfill_error[timeframe] = str(exc)
            self.logger.warning(
                "Backfill attempt failed for %s: %s", timeframe, str(exc), extra={"symbol": self.symbol}, exc_info=True
            )
            return False

    def _interval_to_minutes(self, token: str) -> int:
        cleaned = (token or "").strip().lower()
        if cleaned.endswith("m") and cleaned[:-1].isdigit():
            return max(1, int(cleaned[:-1]))
        if cleaned.endswith("h") and cleaned[:-1].isdigit():
            return max(1, int(cleaned[:-1])) * 60
        if cleaned.endswith("d") and cleaned[:-1].isdigit():
            return max(1, int(cleaned[:-1])) * 1440
        return 1

    async def _backfill_history_with_fallback(
        self, *, timeframe: str, min_bars: int, minutes_per_bar: int
    ) -> bool:
        before = len(self._history.get(timeframe, []))
        backfilled = await self._backfill_history_from_data_layer(
            timeframe=timeframe, min_bars=min_bars, minutes_per_bar=minutes_per_bar
        )
        if backfilled:
            self._last_backfill_error.pop(timeframe, None)
            return True
        if timeframe == self.interval:
            return False
        if before >= min_bars:
            return False
        source_token = normalize_interval_token(self.interval) or "1m"
        source_minutes = self._interval_to_minutes(source_token)
        target_minutes = max(1, minutes_per_bar)
        multiplier = max(1, int(math.ceil(target_minutes / max(1, source_minutes))))
        missing = max(min_bars - before, 1)
        needed_source_bars = missing * multiplier
        await self._backfill_history_from_data_layer(
            timeframe=self.interval,
            min_bars=needed_source_bars,
            minutes_per_bar=source_minutes,
        )
        after = len(self._history.get(timeframe, []))
        if after > before:
            self._last_backfill_error.pop(timeframe, None)
        return after > before

    def _format_bucket(self, bucket: Mapping[str, Any], interval: str) -> Dict[str, Any]:
        start = bucket.get("start")
        if not isinstance(start, datetime):
            start = parse_timestamp(start)
        end = bucket.get("end")
        if not isinstance(end, datetime):
            end = parse_timestamp(end)
        return {
            "start": start,
            "end": end,
            "open": coerce_float(bucket.get("open")),
            "high": coerce_float(bucket.get("high")),
            "low": coerce_float(bucket.get("low")),
            "close": coerce_float(bucket.get("close")),
            "volume": coerce_float(bucket.get("volume")),
            "interval": interval,
        }

    def _format_rule(
        self,
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

    async def _process_hourly(self, candle: Mapping[str, Any]) -> None:
        close_price = candle["close"]
        required_hourly = self._required_hourly_bars()
        current_hourly_len = len(self._history.get('1h', []))
        self.logger.debug("DEBUG_STRATEGY: Processing hourly bar. Count=%s, Required=%s, Close=%s", current_hourly_len, required_hourly, close_price)
        
        for period, tracker in self._ema_1h.items():
            self._hourly_ema_values[period] = tracker.update(close_price)
        required_hourly = self._required_hourly_bars()
        if len(self._history.get("1h", [])) < required_hourly:
            if self._history_replay_in_progress:
                if len(self._history.get("1h", [])) % 10 == 0:
                     self.logger.debug("DEBUG_STRATEGY: Not enough hourly bars yet. Have %s, Need %s", len(self._history.get('1h', [])), required_hourly)
                self._hourly_bias_value = None
                return
            self.logger.debug("DEBUG_STRATEGY: Insufficient hourly bars. Have %s, Need %s. Waiting for backfill.", len(self._history.get("1h", [])), required_hourly)
            self._telemetry_log_signal_waiting(
                step="小时级别EMA",
                reason="等待足够的1小时K线以确定趋势偏向",
                metric=float(len(self._history.get("1h", []))),
                threshold=float(required_hourly),
                comparison="bars",
                details={
                    "timeframe": "1h",
                    "have_bars": len(self._history.get("1h", [])),
                    "need_bars": required_hourly,
                    "symbol": self.symbol,
                },
                timestamp=datetime.now(timezone.utc),
            )
            now_monotonic = self._monotonic_now()
            if now_monotonic < self._hourly_backfill_next_attempt:
                self._hourly_bias_value = None
                return
            if not self._hourly_backfill_requested:
                self._hourly_backfill_requested = True
                try:
                    backfilled = await self._backfill_history_with_fallback(
                        timeframe="1h", min_bars=required_hourly, minutes_per_bar=60
                    )
                    self.logger.debug("DEBUG_STRATEGY: Backfill result: %s", backfilled)
                finally:
                    self._hourly_backfill_requested = False
                if backfilled:
                    self._hourly_backfill_failed = False
                    have_after = len(self._history.get("1h", []))
                    if have_after >= required_hourly:
                        self._hourly_backfill_attempts = 0
                        self._hourly_backfill_next_attempt = 0.0
                    else:
                        self._hourly_backfill_attempts += 1
                        base_delay = max(1.0, float(getattr(self, "history_retry_delay", 2.0)))
                        backoff = max(1.0, float(getattr(self, "history_retry_backoff", 2.0)))
                        delay = max(30.0, base_delay * (backoff ** max(0, self._hourly_backfill_attempts - 1)))
                        self._hourly_backfill_next_attempt = now_monotonic + delay
                else:
                    self._hourly_backfill_failed = True
                    self._hourly_backfill_attempts += 1
                    base_delay = max(1.0, float(getattr(self, "history_retry_delay", 2.0)))
                    backoff = max(1.0, float(getattr(self, "history_retry_backoff", 2.0)))
                    delay = max(30.0, base_delay * (backoff ** max(0, self._hourly_backfill_attempts - 1)))
                    self._hourly_backfill_next_attempt = now_monotonic + delay
            self._hourly_bias_value = None
            return
        self._hourly_bias_value = self._derive_hourly_bias(close_price)

    def evaluate_exit_signal(
        self,
        position: float,
        entry_price: float | None,
        account_equity: float | None = None,
        bar: Mapping[str, Any] | None = None,
        is_dom: bool | None = None,
    ) -> ExitEvaluationResult | None:
        exit_config = getattr(self, "exit_config", None)
        mode = getattr(exit_config, "mode", ExitMode.NONE)
        if mode != ExitMode.FIXED_RR:
            bar_for_exit = bar
            if mode in {ExitMode.ATR, ExitMode.TRAILING_ATR} and isinstance(bar, Mapping):
                bar_interval = normalize_interval_token(bar.get("interval")) or normalize_interval_token(
                    bar.get("timeframe")
                )
                if bar_interval != "5m":
                    bar_for_exit = None
            return super().evaluate_exit_signal(
                position=position,
                entry_price=entry_price,
                account_equity=account_equity,
                bar=bar_for_exit,
                is_dom=is_dom,
            )
        if abs(float(position)) <= 1e-9 or entry_price is None:
            self._clear_active_exit_levels()
            return super().evaluate_exit_signal(
                position=position,
                entry_price=entry_price,
                account_equity=account_equity,
                bar=bar,
                is_dom=is_dom,
            )
        if self._active_stop_loss is None and self._active_take_profit is None:
            return super().evaluate_exit_signal(
                position=position,
                entry_price=entry_price,
                account_equity=account_equity,
                bar=bar,
                is_dom=is_dom,
            )
        current_sign = 1 if float(position) > 0 else -1
        stored_entry = self._active_exit_entry_price
        if (
            stored_entry is None
            or self._active_exit_position_sign != current_sign
            or not math.isclose(float(entry_price), stored_entry, abs_tol=1e-9)
        ):
            self._clear_active_exit_levels()
            return super().evaluate_exit_signal(
                position=position,
                entry_price=entry_price,
                account_equity=account_equity,
                bar=bar,
                is_dom=is_dom,
            )
        return ExitEvaluationResult(
            stop_loss=self._active_stop_loss,
            take_profit=self._active_take_profit,
            changed=False,
            mode=ExitMode.FIXED_RR,
            details={"reason": "stored_levels"},
        )

    def _derive_hourly_bias(self, close_price: float) -> Optional[str]:
        ema21 = self._hourly_ema_values.get(21)
        ema50 = self._hourly_ema_values.get(50)
        ema200 = self._hourly_ema_values.get(200)
        if ema21 is None or ema50 is None:
            self.logger.debug("DEBUG_STRATEGY: Bias check failed - missing EMAs. 21=%s, 50=%s", ema21, ema50)
            return None
        tolerance = abs(ema50) * self.bias_tolerance
        
        bias = None
        if ema21 - ema50 > tolerance and close_price > ema50:
            if ema200 is not None and ema50 < ema200:
                bias = None
            else:
                bias = "long"
        elif ema50 - ema21 > tolerance and close_price < ema50:
            if ema200 is not None and ema50 > ema200:
                bias = None
            else:
                bias = "short"
        
        if bias:
             self.logger.debug("DEBUG_STRATEGY: Bias calculated: %s. EMA21=%.2f, EMA50=%.2f, EMA200=%s, Close=%.2f", bias, ema21, ema50, ema200, close_price)
        else:
             self.logger.debug("DEBUG_STRATEGY: Bias is None. EMA21=%.2f, EMA50=%.2f, EMA200=%s, Close=%.2f, Tol=%.4f", ema21, ema50, ema200, close_price, tolerance)
        return bias

    async def _process_five_minute(self, candle: Mapping[str, Any]) -> None:
        close_price = candle["close"]
        for period, tracker in self._ema_5m.items():
            self._five_ema_values[period] = tracker.update(close_price)
        rsi_value = self._rsi_5m.update(close_price)
        ema21 = self._five_ema_values.get(21)
        ema50 = self._five_ema_values.get(50)
        ema200 = self._five_ema_values.get(200)
        ts = None
        try:
            ts = candle.get("end")
        except Exception:
            ts = None
        self._maybe_execute_base_exit(candle)
        
        # Log successful processing to verify data flow
        if ema21 is not None and ema50 is not None and ema200 is not None:
             self.logger.debug("DEBUG_STRATEGY: 5m processed. Close=%s, EMAs: 21=%s, 50=%s, 200=%s", 
                               close_price, ema21, ema50, ema200)

        if ema21 is None or ema50 is None or ema200 is None:
            self.logger.debug("DEBUG_STRATEGY: Missing 5m EMAs. 21=%s, 50=%s, 200=%s", ema21, ema50, ema200)
            return
        self._update_touch_counters(candle, ema21, ema50, ema200)
        if len(self._history.get("5m", [])) < self.min_five_bars:
            if self._history_replay_in_progress:
                return
            self.logger.debug("DEBUG_STRATEGY: Insufficient 5m bars. Have %s, Need %s", len(self._history.get("5m", [])), self.min_five_bars)
            self._telemetry_log_signal_waiting(
                step="五分钟历史",
                reason="等待足够的5分钟K线以计算RSI/EMA",
                metric=float(len(self._history.get("5m", []))),
                threshold=float(self.min_five_bars),
                comparison="bars",
                details={
                    "timeframe": "5m",
                    "have_bars": len(self._history.get("5m", [])),
                    "need_bars": self.min_five_bars,
                    "symbol": self.symbol,
                },
                timestamp=ts,
            )
            now_monotonic = self._monotonic_now()
            if now_monotonic < self._five_backfill_next_attempt:
                return
            if not self._five_backfill_requested:
                self._five_backfill_requested = True
                try:
                    backfilled = await self._backfill_history_with_fallback(
                        timeframe="5m", min_bars=self.min_five_bars, minutes_per_bar=5
                    )
                finally:
                    self._five_backfill_requested = False
                if backfilled:
                    self._five_backfill_failed = False
                    have_after = len(self._history.get("5m", []))
                    if have_after >= self.min_five_bars:
                        self._five_backfill_attempts = 0
                        self._five_backfill_next_attempt = 0.0
                    else:
                        self._five_backfill_attempts += 1
                        base_delay = max(1.0, float(getattr(self, "history_retry_delay", 2.0)))
                        backoff = max(1.0, float(getattr(self, "history_retry_backoff", 2.0)))
                        delay = max(30.0, base_delay * (backoff ** max(0, self._five_backfill_attempts - 1)))
                        self._five_backfill_next_attempt = now_monotonic + delay
                else:
                    self._five_backfill_failed = True
                    self._five_backfill_attempts += 1
                    base_delay = max(1.0, float(getattr(self, "history_retry_delay", 2.0)))
                    backoff = max(1.0, float(getattr(self, "history_retry_backoff", 2.0)))
                    delay = max(30.0, base_delay * (backoff ** max(0, self._five_backfill_attempts - 1)))
                    self._five_backfill_next_attempt = now_monotonic + delay
            return
        if self._hourly_bias_value is None:
            ema21 = self._hourly_ema_values.get(21)
            ema50 = self._hourly_ema_values.get(50)
            ema200 = self._hourly_ema_values.get(200)
            tolerance = abs(ema50) * self.bias_tolerance if ema50 is not None else None
            bar_end_value = ts.isoformat() if isinstance(ts, datetime) else None
            have_hourly_bars = len(self._history.get("1h", []))
            long_ok = (
                ema21 is not None
                and ema50 is not None
                and close_price > ema50
                and (ema21 - ema50) > (tolerance or 0.0)
                and (ema200 is None or ema50 >= ema200)
            )
            short_ok = (
                ema21 is not None
                and ema50 is not None
                and close_price < ema50
                and (ema50 - ema21) > (tolerance or 0.0)
                and (ema200 is None or ema50 <= ema200)
            )
            long_threshold = (
                (ema50 + tolerance) if ema50 is not None and tolerance is not None else None
            )
            short_threshold = (
                (ema50 - tolerance) if ema50 is not None and tolerance is not None else None
            )
            evaluations = [
                {
                    "condition": "ema21>=ema50+tolerance",
                    "current": {"ema21": ema21, "ema50": ema50, "tolerance": tolerance},
                    "threshold": long_threshold,
                    "passed": bool(ema21 is not None and ema50 is not None and (ema21 - ema50) > (tolerance or 0.0)),
                    "details": {"bias": "long", "bar_end": bar_end_value},
                },
                {
                    "condition": "close>ema50",
                    "current": close_price,
                    "threshold": ema50,
                    "passed": bool(ema50 is not None and close_price > ema50),
                    "details": {"bias": "long", "bar_end": bar_end_value},
                },
                {
                    "condition": "ema50>=ema200",
                    "current": {"ema50": ema50, "ema200": ema200},
                    "passed": bool(ema200 is None or (ema50 is not None and ema50 >= ema200)),
                    "details": {"bias": "long", "bar_end": bar_end_value},
                },
                {
                    "condition": "ema50>=ema21+tolerance",
                    "current": {"ema21": ema21, "ema50": ema50, "tolerance": tolerance},
                    "threshold": short_threshold,
                    "passed": bool(ema21 is not None and ema50 is not None and (ema50 - ema21) > (tolerance or 0.0)),
                    "details": {"bias": "short", "bar_end": bar_end_value},
                },
                {
                    "condition": "close<ema50",
                    "current": close_price,
                    "threshold": ema50,
                    "passed": bool(ema50 is not None and close_price < ema50),
                    "details": {"bias": "short", "bar_end": bar_end_value},
                },
                {
                    "condition": "ema50<=ema200",
                    "current": {"ema50": ema50, "ema200": ema200},
                    "passed": bool(ema200 is None or (ema50 is not None and ema50 <= ema200)),
                    "details": {"bias": "short", "bar_end": bar_end_value},
                },
            ]
            def _ema_status(value: float | None, period: int) -> float | str:
                if value is None:
                    return f"n/a (have={have_hourly_bars}, need={period})"
                return value

            long_threshold_value = (
                (ema50 + tolerance)
                if ema50 is not None and tolerance is not None
                else _ema_status(None, 50)
            )
            short_threshold_value = (
                (ema21 + tolerance)
                if ema21 is not None and tolerance is not None
                else _ema_status(None, 21)
            )
            self._telemetry_set_phase_status(
                self._PHASE_SIGNALS,
                status="running",
                status_code="conditions_not_met",
                status_reason="1小时趋势尚未确认",
                status_details={
                    "symbol": self.symbol,
                    "bar_end": bar_end_value,
                    "long_ok": long_ok,
                    "short_ok": short_ok,
                },
                timestamp=ts,
            )
            # DEBUG_STRATEGY: Log why hourly bias is None
            if have_hourly_bars >= 100:  # Only log if we have enough bars to expect a bias
                 self.logger.debug("DEBUG_STRATEGY: Hourly bias None. LongOK=%s, ShortOK=%s. Close=%s, EMA21=%s, EMA50=%s, EMA200=%s, Tol=%s", long_ok, short_ok, close_price, ema21, ema50, ema200, tolerance)

            self._telemetry_log(
                "1小时趋势未确认 (震荡或反转中)",
                level="INFO",
                tone="neutral",
                phase=self._PHASE_SIGNALS,
                details={
                    "symbol": self.symbol,
                    "interval": "1h",
                    "bar_end": bar_end_value,
                    "reason": "需满足多头组 OR 空头组的所有条件",
                    "check_long_group": {
                        "1_ema_trend": self._format_rule(
                            _ema_status(ema21, 21),
                            long_threshold_value,
                            ">=",
                            bool(evaluations[0]["passed"]),
                        ),
                        "2_price_location": self._format_rule(
                            close_price,
                            _ema_status(ema50, 50),
                            ">=",
                            bool(evaluations[1]["passed"]),
                        ),
                        "3_ema200_filter": self._format_rule(
                            _ema_status(ema50, 50),
                            _ema_status(ema200, 200),
                            ">=",
                            bool(evaluations[2]["passed"]),
                        ),
                    },
                    "check_short_group": {
                        "1_ema_trend": self._format_rule(
                            _ema_status(ema50, 50),
                            short_threshold_value,
                            ">=",
                            bool(evaluations[3]["passed"]),
                        ),
                        "2_price_location": self._format_rule(
                            close_price,
                            _ema_status(ema50, 50),
                            "<=",
                            bool(evaluations[4]["passed"]),
                        ),
                        "3_ema200_filter": self._format_rule(
                            _ema_status(ema50, 50),
                            _ema_status(ema200, 200),
                            "<=",
                            bool(evaluations[5]["passed"]),
                        ),
                    },
                    "hourly_bars": have_hourly_bars,
                },
                deduplicate=False,
                timestamp=ts,
            )
            return
        if self._hourly_bias_value == "long":
            self._evaluate_long(candle, ema21, ema50, ema200, rsi_value, allow_order=True)
            self._evaluate_short(candle, ema21, ema50, ema200, rsi_value, allow_order=False)
        elif self._hourly_bias_value == "short":
            self._evaluate_short(candle, ema21, ema50, ema200, rsi_value, allow_order=True)
            self._evaluate_long(candle, ema21, ema50, ema200, rsi_value, allow_order=False)

    def _get_current_position(self) -> float:
        # Prefer any non-flat view of exposure. Backtests can briefly lag between
        # account snapshots, risk-state updates, and the strategy's own `_position`.
        provider = getattr(self, "position_provider", None)
        if provider is None:
            provider = self._dependencies.get("position_provider")

        provider_position: float | None = None
        if callable(provider):
            try:
                resolved = self._resolve_maybe_awaitable_float(
                    provider(self.symbol),
                    default=0.0,
                    label=f"{self.name}.position_provider",
                )
                provider_position = float(resolved or 0.0)
            except Exception:
                provider_position = None

        risk = getattr(self, "risk_manager", None)
        if risk is None:
            risk = self._dependencies.get("risk_manager")
        risk_position: float | None = None
        if risk is not None:
            identifier = getattr(self, "identifier", None) or self.name
            try:
                state = risk.current_state(identifier)
            except Exception:
                state = None

            if isinstance(state, Mapping):
                try:
                    risk_position = float(state.get("net_position", 0.0) or 0.0)
                except Exception:
                    risk_position = None

        strategy_position, _ = self._resolve_position_state()
        internal_position = getattr(self, "_position", 0.0)
        try:
            internal_position_value = float(internal_position or 0.0)
        except (TypeError, ValueError):
            internal_position_value = 0.0
        candidates = [
            provider_position,
            risk_position,
            float(strategy_position or 0.0),
            internal_position_value,
        ]
        for candidate in candidates:
            if candidate is not None and abs(candidate) > 1e-9:
                return float(candidate)
        return float(provider_position or risk_position or strategy_position or internal_position_value or 0.0)

    def _update_touch_counters(
        self,
        candle: Mapping[str, Any],
        ema21: float,
        ema50: float,
        ema200: float,
    ) -> None:
        tolerance = self.ema_touch_tolerance
        low = candle["low"]
        high = candle["high"]
        touched_long = False
        touched_short = False
        for value in (ema21, ema50, ema200):
            if value is None:
                continue
            if low <= value * (1 + tolerance):
                touched_long = True
            if high >= value * (1 - tolerance):
                touched_short = True
        if touched_long:
            self._candles_since_touch_long = 0
        elif self._candles_since_touch_long is not None:
            self._candles_since_touch_long += 1
            if self._candles_since_touch_long > self.ema_touch_lookback:
                self._candles_since_touch_long = None
        if touched_short:
            self._candles_since_touch_short = 0
        elif self._candles_since_touch_short is not None:
            self._candles_since_touch_short += 1
            if self._candles_since_touch_short > self.ema_touch_lookback:
                self._candles_since_touch_short = None

    def _evaluate_long(
        self,
        candle: Mapping[str, Any],
        ema21: float,
        ema50: float,
        ema200: float,
        rsi_value: Optional[float],
        *,
        allow_order: bool = True,
    ) -> None:
        # Check for existing position to prevent pyramiding/overwriting
        if allow_order:
            ok, reason = self.can_open_new_trade("BUY")
            if not ok:
                allow_order = False

        body = candle["close"] - candle["open"]
        range_size = candle["high"] - candle["low"]
        body_ratio = (body / range_size) if range_size > 0 else 0.0
        push = (candle["close"] - (ema21 or 0.0)) / (ema21 or 1.0)
        entry_price = candle["close"]
        cond_touch = self._candles_since_touch_long is not None
        cond_rsi = rsi_value is not None and rsi_value >= self.rsi_long_threshold
        # Relaxed EMA condition: only require EMA21 > EMA50 (uptrend)
        # We trust Hourly Bias to handle the major trend direction.
        cond_ema = bool(ema21 > ema50)
        cond_bias_confirm = self._hourly_bias_value == "long"
        cond_body_pos = body > 0
        cond_range = range_size > 0
        cond_body_ratio = body_ratio >= self.momentum_body_ratio
        cond_push = push >= self.momentum_push_threshold
        current_time = candle.get("end")
        if not isinstance(current_time, datetime):
            # Fallback if candle doesn't have valid datetime
            cond_cooldown = True
        else:
            cond_cooldown = self._check_cooldown(current_time)
            
        # Original logic calculates Long Stop (below entry)
        original_stop = self._compute_long_stop(candle, ema21, ema50, ema200)
        cond_stop_ok = original_stop is not None and (entry_price - (original_stop or 0.0)) > 0
        all_pass = (
            cond_touch
            and cond_rsi
            and (cond_ema or cond_bias_confirm)
            and cond_body_pos
            and cond_range
            and cond_body_ratio
            and cond_push
            and cond_cooldown
            and cond_stop_ok
        )
        ema_order_gap = None
        if ema21 is not None and ema50 is not None:
            ema_order_gap = ema21 - ema50
        self._telemetry_set_phase_status(self._PHASE_SIGNALS, status="evaluated", status_code="conditions_checked")
        
        if cond_ema or self._hourly_bias_value == "long":
             self.logger.debug("DEBUG_STRATEGY: LONG candidate. Pass=%s. Touch=%s, RSI=%s (%s), BodyRatio=%.2f, BodyPos=%s, Push=%.4f, StopOK=%s, Cooldown=%s, EMA21=%.2f, EMA50=%.2f", all_pass, cond_touch, cond_rsi, rsi_value, body_ratio, cond_body_pos, push, cond_stop_ok, cond_cooldown, ema21, ema50)

        self._telemetry_log(
            "5m momentum long conditions evaluated",
            level="INFO",
            tone="neutral",
            phase=self._PHASE_SIGNALS,
            details={
                "symbol": getattr(self, "symbol", "") or "",
                "interval": "5m",
                "bar_end": candle.get("end").isoformat() if isinstance(candle.get("end"), datetime) else None,
                "touch_ema": self._format_rule(
                    self._candles_since_touch_long,
                    self.ema_touch_lookback,
                    "<=",
                    cond_touch,
                ),
                "rsi": self._format_rule(
                    rsi_value,
                    self.rsi_long_threshold,
                    ">=",
                    cond_rsi,
                ),
                "ema_order": self._format_rule(
                    ema_order_gap,
                    0.0,
                    ">",
                    cond_ema,
                ),
                "body_ratio": self._format_rule(
                    body_ratio,
                    self.momentum_body_ratio,
                    ">=",
                    cond_body_ratio,
                ),
                "push": self._format_rule(
                    push,
                    self.momentum_push_threshold,
                    ">=",
                    cond_push,
                ),
                "order_allowed": "PASS" if allow_order else "FAIL",
            },
            deduplicate=False,
            timestamp=candle.get("end") if isinstance(candle.get("end"), datetime) else None,
        )
        if not all_pass or not allow_order:
            return
        liquidity_ok, liquidity_metrics = self._evaluate_liquidity_gate(
            side="BUY",
            reference_price=entry_price,
            timestamp=candle.get("end"),
            reason="five_minute_momentum_long",
        )
        if not liquidity_ok:
            return
            
        # Standard Logic: Execute BUY when Long conditions are met
        stop_price = original_stop
        risk = entry_price - (stop_price or 0.0)
        target_price = (
            entry_price + risk * self.risk_reward_ratio if self.risk_reward_ratio > 0 else None
        )

        metrics = {
            "ema21": ema21,
            "ema50": ema50,
            "ema200": ema200,
            "rsi": rsi_value,
            "body_ratio": body_ratio,
            "push": push,
            "liquidity_confidence": liquidity_metrics.confidence,
            "liquidity_invalidate_level": liquidity_metrics.invalidate_level,
            "swapped": False,
        }
        self._submit_order(
            side="BUY",
            entry=entry_price,
            stop=stop_price,
            target=target_price,
            candle=candle,
            reason="five_minute_momentum_long",
            metrics=metrics,
            extra_tags={"hourly_bias": self._hourly_bias_value or "neutral", "swapped": "false"},
        )

    def _evaluate_short(
        self,
        candle: Mapping[str, Any],
        ema21: float,
        ema50: float,
        ema200: float,
        rsi_value: Optional[float],
        *,
        allow_order: bool = True,
    ) -> None:
        # Check for existing position to prevent pyramiding/overwriting
        if allow_order:
            # Strict pyramiding check: if already short and pyramiding not allowed, block
            if not self.allow_pyramiding and self._get_current_position() < 0:
                 allow_order = False
            else:
                ok, reason = self.can_open_new_trade("SELL")
                if not ok:
                    allow_order = False

        body = candle["close"] - candle["open"]
        range_size = candle["high"] - candle["low"]
        body_ratio = (abs(body) / range_size) if range_size > 0 else 0.0
        push = ((ema21 or 0.0) - candle["close"]) / (abs(ema21) or 1.0)
        entry_price = candle["close"]
        cond_touch = self._candles_since_touch_short is not None
        cond_rsi = rsi_value is not None and rsi_value <= self.rsi_short_threshold
        # Relaxed EMA condition
        cond_ema = bool(ema21 < ema50)
        cond_bias_confirm = self._hourly_bias_value == "short"
        cond_body_neg = body < 0
        cond_range = range_size > 0
        cond_body_ratio = body_ratio >= self.momentum_body_ratio
        cond_push = push >= self.momentum_push_threshold
        current_time = candle.get("end")
        if not isinstance(current_time, datetime):
            # Fallback if candle doesn't have valid datetime
            cond_cooldown = True
        else:
            cond_cooldown = self._check_cooldown(current_time)

        # Original logic calculates Short Stop (above entry)
        original_stop = self._compute_short_stop(candle, ema21, ema50, ema200)
        cond_stop_ok = original_stop is not None and ((original_stop or 0.0) - entry_price) > 0
        all_pass = (
            cond_touch
            and cond_rsi
            and (cond_ema or cond_bias_confirm)
            and cond_body_neg
            and cond_range
            and cond_body_ratio
            and cond_push
            and cond_cooldown
            and cond_stop_ok
        )
        ema_order_gap = None
        if ema21 is not None and ema50 is not None:
            ema_order_gap = ema50 - ema21
        self._telemetry_set_phase_status(self._PHASE_SIGNALS, status="evaluated", status_code="conditions_checked")
        
        if cond_ema or self._hourly_bias_value == "short":
             self.logger.debug("DEBUG_STRATEGY: SHORT candidate. Pass=%s. Touch=%s, RSI=%s (%s), EMA_Align=%s, BodyRatio=%.2f, BodyNeg=%s, Push=%.4f, StopOK=%s, Cooldown=%s, EMA21=%.2f, EMA50=%.2f", all_pass, cond_touch, cond_rsi, rsi_value, cond_ema, body_ratio, cond_body_neg, push, cond_stop_ok, cond_cooldown, ema21, ema50)

        self._telemetry_log(
            "5m momentum short conditions evaluated",
            level="INFO",
            tone="neutral",
            phase=self._PHASE_SIGNALS,
            details={
                "symbol": getattr(self, "symbol", "") or "",
                "interval": "5m",
                "bar_end": candle.get("end").isoformat() if isinstance(candle.get("end"), datetime) else None,
                "touch_ema": self._format_rule(
                    self._candles_since_touch_short,
                    self.ema_touch_lookback,
                    "<=",
                    cond_touch,
                ),
                "rsi": self._format_rule(
                    rsi_value,
                    self.rsi_short_threshold,
                    "<=",
                    cond_rsi,
                ),
                "ema_order": self._format_rule(
                    ema_order_gap,
                    0.0,
                    ">",
                    cond_ema,
                ),
                "body_ratio": self._format_rule(
                    body_ratio,
                    self.momentum_body_ratio,
                    ">=",
                    cond_body_ratio,
                ),
                "push": self._format_rule(
                    push,
                    self.momentum_push_threshold,
                    ">=",
                    cond_push,
                ),
                "order_allowed": "PASS" if allow_order else "FAIL",
            },
            deduplicate=False,
            timestamp=candle.get("end") if isinstance(candle.get("end"), datetime) else None,
        )
        if not all_pass or not allow_order:
            return
        liquidity_ok, liquidity_metrics = self._evaluate_liquidity_gate(
            side="SELL",
            reference_price=entry_price,
            timestamp=candle.get("end"),
            reason="five_minute_momentum_short",
        )
        if not liquidity_ok:
            return
            
        # Standard Logic: Execute SELL when Short conditions are met
        stop_price = original_stop
        risk = (stop_price or 0.0) - entry_price
        target_price = (
            entry_price - risk * self.risk_reward_ratio if self.risk_reward_ratio > 0 else None
        )

        metrics = {
            "ema21": ema21,
            "ema50": ema50,
            "ema200": ema200,
            "rsi": rsi_value,
            "body_ratio": body_ratio,
            "push": push,
            "liquidity_confidence": liquidity_metrics.confidence,
            "liquidity_invalidate_level": liquidity_metrics.invalidate_level,
            "swapped": False,
        }
        self._submit_order(
            side="SELL",
            entry=entry_price,
            stop=stop_price,
            target=target_price,
            candle=candle,
            reason="five_minute_momentum_short",
            metrics=metrics,
            extra_tags={"hourly_bias": self._hourly_bias_value or "neutral", "swapped": "false"},
        )

    def _evaluate_liquidity_gate(
        self,
        *,
        side: str,
        reference_price: float,
        timestamp: Any,
        reason: str,
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
        in_entry_zone = zone_valid and zone_low <= reference_price <= zone_high
        details = dict(metrics.details or {})
        long_trap = details.get("long_false_breakout_trap", {})
        short_trap = details.get("short_false_breakout_trap", {})
        trap_payload = long_trap if is_buy else short_trap
        trap_detected = bool(trap_payload.get("active"))
        bias_ok = metrics.trade_bias == expected_bias
        confidence_ok = metrics.confidence >= self.liquidity_min_confidence
        enabled = bool(self.enable_liquidity_filter)
        passed = (not enabled) or (not trap_detected)
        self._telemetry_log(
            "Liquidity filter evaluated before signal dispatch",
            level="INFO",
            tone="neutral" if passed else "warning",
            phase=self._PHASE_SIGNALS,
            details={
                "symbol": getattr(self, "symbol", "") or "",
                "strategy_reason": reason,
                "side": side.upper(),
                "enabled": enabled,
                "passed": passed,
                "filter_mode": "anti_fake_breakout_only",
                "liquidity_interval": interval_used,
                "trade_bias": metrics.trade_bias,
                "entry_zone": {"low": zone_low, "high": zone_high},
                "invalidate_level": float(metrics.invalidate_level or 0.0),
                "confidence": float(metrics.confidence),
                "checks": {
                    "trap_blocked": trap_detected,
                    "bias_match_advisory": bias_ok,
                    "confidence_ok_advisory": confidence_ok,
                    "entry_zone_ok_advisory": in_entry_zone,
                },
                "active_trap": trap_payload if trap_detected else None,
                "reference_price": float(reference_price),
                "min_confidence": float(self.liquidity_min_confidence),
                "liquidity_details": details,
            },
            deduplicate=False,
            timestamp=timestamp if isinstance(timestamp, datetime) else None,
        )
        return passed, metrics

    def _check_cooldown(self, current_time: datetime) -> bool:
        if self.cooldown_seconds <= 0:
            return True
        
        # Check time since last exit (prevent churning)
        if self._last_exit_time is not None:
            elapsed_exit = (current_time - self._last_exit_time).total_seconds()
            if elapsed_exit < self.cooldown_seconds:
                return False

        if self._last_signal_bar is None:
            return True
            
        elapsed = (current_time - self._last_signal_bar).total_seconds()
        if elapsed < self.cooldown_seconds:
            return False
        return True

    def _compute_long_stop(
        self, candle: Mapping[str, Any], ema21: float, ema50: float, ema200: float
    ) -> Optional[float]:
        buffer = candle["close"] * self.stop_buffer_pct
        anchors = [candle["low"]]
        for value in (ema21, ema50, ema200):
            if value is not None:
                anchors.append(value)
        stop = min(anchors) - buffer
        return stop if stop > 0 else None

    def _compute_short_stop(
        self, candle: Mapping[str, Any], ema21: float, ema50: float, ema200: float
    ) -> Optional[float]:
        buffer = candle["close"] * self.stop_buffer_pct
        anchors = [candle["high"]]
        for value in (ema21, ema50, ema200):
            if value is not None:
                anchors.append(value)
        stop = max(anchors) + buffer
        return stop if stop > 0 else None

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
        risk_manager = getattr(self, "risk_manager", None)
        if risk_manager is not None:
            checker = getattr(risk_manager, "check_circuit_breaker_before_new_order", None)
            if callable(checker):
                try:
                    ok, reason_text = checker()
                except Exception:
                    self.logger.exception("Risk manager check failed")
                    return
                if not ok:
                    message = "Risk breaker blocked order"
                    if reason_text:
                        message = f"{message}: {reason_text}"
                    self.logger.warning(
                        message,
                        extra={
                            "event": "strategy.signal.risk_blocked",
                            "strategy": self.name,
                            "symbol": self.symbol,
                            "side": side,
                        },
                    )
                    return
        stop, target = self._apply_fixed_rr_exit(side, entry, stop, target)
        quantity = self._determine_quantity(entry, stop)
        if quantity <= 0:
            return
        exchange, sec_type = self._resolve_instrument(self.symbol)
        metadata: Dict[str, Any] = {
            "entry_price": float(entry),
            "entry_price_hint": float(entry),
            "interval_end": candle["end"].isoformat(),
        }
        if stop is not None and stop > 0:
            metadata["stop_loss"] = float(stop)
        if target is not None and target > 0:
            metadata["take_profit"] = float(target)
        self._record_stop_levels_telemetry(stop, target)
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
            self.logger.warning(
                "Failed to queue five-minute momentum order",
                extra={
                    "event": "strategy.order.queue_failed",
                    "strategy": self.name,
                    "symbol": self.symbol,
                    "side": side,
                    "quantity": quantity,
                },
            )
            return
        self._active_stop_loss = float(stop) if stop is not None and stop > 0 else None
        self._active_take_profit = float(target) if target is not None and target > 0 else None
        self._active_exit_entry_price = float(entry)
        self._active_exit_position_sign = 1 if side.upper() == "BUY" else -1
        self._exit_dispatched = False
        self._last_signal_monotonic = time.monotonic()
        self._last_signal_bar = candle["end"]
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
        self.logger.info("Five-minute momentum order queued", extra=log_extra)

    def _apply_fixed_rr_exit(
        self,
        side: str,
        entry: float,
        stop: Optional[float],
        target: Optional[float],
    ) -> tuple[Optional[float], Optional[float]]:
        config = getattr(self, "exit_config", None)
        if config is None or config.mode != ExitMode.FIXED_RR:
            return stop, target
        risk_amount = getattr(config, "risk_amount", None)
        if risk_amount is None:
            return stop, target
        try:
            per_unit = abs(float(risk_amount))
        except (TypeError, ValueError):
            return stop, target
        if per_unit <= 0:
            return stop, target
        direction = 1.0 if side.upper() == "BUY" else -1.0
        try:
            rr_ratio = max(float(getattr(config, "rr_ratio", 0.0)), 0.0)
        except (TypeError, ValueError):
            rr_ratio = 0.0
        fixed_stop = entry - direction * per_unit
        fixed_target = entry + direction * per_unit * rr_ratio if rr_ratio > 0 else None
        return fixed_stop, fixed_target

    def _record_stop_levels_telemetry(
        self, stop: Optional[float], target: Optional[float]
    ) -> None:
        telemetry = getattr(self, "runtime_telemetry", None)
        if telemetry is None:
            return
        try:
            telemetry.record_stop_levels(
                self._telemetry_strategy_id(),
                stop_loss_enabled=stop is not None,
                stop_loss_price=stop,
                take_profit_enabled=target is not None,
                take_profit_price=target,
            )
        except Exception:
            self.logger.exception("Failed to record stop levels telemetry")

    def _determine_quantity(self, price: float, stop_loss_price: float | None = None) -> int:
        forced_quantity = getattr(self, "_force_trade_quantity", None)
        if forced_quantity is not None:
            try:
                return max(1, int(forced_quantity))
            except (TypeError, ValueError):
                pass
        # Try risk-based sizing first if configured
        risk_amount = None
        max_quantity = 10
        
        config = getattr(self, "exit_config", None)
        if config:
             if hasattr(config, "risk_amount") and config.risk_amount is not None:
                try:
                    risk_amount = float(config.risk_amount)
                except (ValueError, TypeError):
                    pass
             if hasattr(config, "max_quantity") and config.max_quantity is not None:
                 try:
                     max_quantity = int(config.max_quantity)
                 except (ValueError, TypeError):
                     pass

        if risk_amount is not None and risk_amount > 0 and stop_loss_price is not None:
                point_value = resolve_index_future_point_value(self.symbol) or 1.0
                dist = abs(price - stop_loss_price)
                if dist > 1e-9:
                    # Calculate quantity based on risk: Risk = Dist * PV * Qty  =>  Qty = Risk / (Dist * PV)
                    raw_qty = risk_amount / (dist * point_value)
                    
                    self.logger.info(
                        "Risk sizing: Risk=%s, Dist=%s, PV=%s, RawQty=%s, StopLoss=%s, Price=%s",
                        risk_amount, dist, point_value, raw_qty, stop_loss_price, price
                    )

                    # Check for extreme over-sizing due to tight stops
                    if raw_qty > max_quantity:
                         self.logger.warning(
                             "Calculated quantity %s exceeds max %s (Risk=%s, Dist=%s, PV=%s). Capping.", 
                             raw_qty, max_quantity, risk_amount, dist, point_value
                         )
                    
                    qty = min(max_quantity, max(1, int(raw_qty)))
                
                # Log if forced to 1 despite risk violation
                if raw_qty < 1.0:
                     actual_risk = dist * point_value * 1
                     self.logger.warning(
                         "Forcing quantity to 1 despite risk violation. ConfigRisk=%s, ActualRisk=%s (Dist=%s, PV=%s)",
                         risk_amount, actual_risk, dist, point_value
                     )
                return qty

        qty = max(1, int(self.default_quantity))
        # self.logger.debug(f"Determined quantity: {qty} (default={self.default_quantity})")
        return min(max_quantity, qty)

    def _resolve_instrument(self, symbol: str) -> tuple[str, str]:
        base = (symbol or "").upper()
        if base in DEFAULT_INSTRUMENT_DETAILS:
            details = DEFAULT_INSTRUMENT_DETAILS[base]
            return details.get("exchange", "CME"), details.get("sec_type", "FUT")
        for key, details in DEFAULT_INSTRUMENT_DETAILS.items():
            if base.startswith(key):
                return details.get("exchange", "CME"), details.get("sec_type", "FUT")
        return "SMART", "STK"
