"""Opening Range Breakout strategy with Fair Value Gap confirmation."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Deque, Dict, Mapping, NamedTuple, Optional

try:  # pragma: no cover - Python 3.8 fallback
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - fallback for systems without zoneinfo
    from backports.zoneinfo import ZoneInfo  # type: ignore[assignment]

from .candle import CandleSubscriptionStrategy
from .liquidity_tool import LiquidityFilterConfig, LiquidityFilterMetrics, LiquidityStrategyTool
from .templates import StrategyTemplate
from src.strategy.exit import ExitMode
from src.data_layer import DataSubscriptionRequest
from src.common.market_data.aggregation import MinuteBarAggregator, floor_timestamp as _floor_timestamp


DEFAULT_INSTRUMENT_DETAILS: Dict[str, Dict[str, str]] = {
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


class EntryResult(NamedTuple):
    queued: bool
    risk_blocked: bool
    status_code: str | None = None
    reason: str | None = None


@dataclass
class OpeningRangeBreakoutStrategy(CandleSubscriptionStrategy, StrategyTemplate):
    """Opening Range Breakout using 1m candles with FVG confirmation."""

    name: str = "ORB FVG Breakout"
    strategy_type = "orb_fvg_breakout"
    description: str = (
        "基于15分钟开盘区间和1分钟FVG确认的顺势突破策略，自动计算风险与目标R倍数"
    )
    symbol: str = "NQ"
    interval: str = "1m"
    history_limit: int = 400
    default_quantity: int = 1
    session_open_time: str = "09:30"
    session_timezone: str = "America/New_York"
    opening_range_minutes: int = 15
    trade_window_minutes: int = 390
    risk_threshold_points: float = 30.0
    cooldown_seconds: float = 0.0
    signal_frequency_seconds: float = 0.0
    fvg_tolerance_points: float = 50.0
    dispatch_history_candles: bool = True
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

    _recent_candles: Deque[Dict[str, float]] = field(
        default_factory=lambda: deque(maxlen=6), init=False, repr=False
    )
    _session_tz: ZoneInfo = field(init=False, repr=False)
    _session_open_clock: dtime = field(init=False, repr=False)
    _current_session: date | None = field(default=None, init=False, repr=False)
    _session_open_dt: datetime | None = field(default=None, init=False, repr=False)
    _range_end_local: datetime | None = field(default=None, init=False, repr=False)
    _range_high: float | None = field(default=None, init=False, repr=False)
    _range_low: float | None = field(default=None, init=False, repr=False)
    _range_confirmed: bool = field(default=False, init=False, repr=False)
    _last_signal_time: float = field(default=0.0, init=False, repr=False)
    _trade_window_notified: bool = field(default=False, init=False, repr=False)
    _liquidity_tool: LiquidityStrategyTool = field(init=False, repr=False)

    def __post_init__(self) -> None:  # noqa: D401 - defer to parent docstring
        super().__post_init__()
        # ORB FVG relies on 1m candles for the three-bar FVG confirmation sequence.
        # Persisted interval edits silently starve the setup, so keep the runtime fixed.
        self._force_runtime_intervals(interval="1m", intervals=["1m"])
        
        self.default_quantity = max(1, int(self.default_quantity))
        self.opening_range_minutes = max(1, int(self.opening_range_minutes))
        self.trade_window_minutes = max(
            self.opening_range_minutes + 1, int(self.trade_window_minutes)
        )
        self.risk_threshold_points = max(0.0, float(self.risk_threshold_points))
        self.cooldown_seconds = max(0.0, float(self.cooldown_seconds))
        self.fvg_tolerance_points = max(0.0, float(self.fvg_tolerance_points))
        self._configure_liquidity_filter()
        self._session_tz = self._load_timezone(self.session_timezone)
        self._session_open_clock = self._parse_session_open(self.session_open_time)
        self.summary_points = [
            "自动订阅1分钟K线并在本地构建15分钟开盘区间",
            "突破区间后检测连续三根K线FVG形态并在第3根收盘触发入场",
            "以FVG边界为止损，依据风险点数设置1R或2R止盈目标",
        ]
        self.file_path = "src/strategies/orb_fvg.py"
        self._register_parameters()

    def _bootstrap_history_target(self) -> int:
        interval_minutes = max(1, int(self._interval_delta.total_seconds() // 60))
        opening_range_bars = max(3, (int(self.opening_range_minutes) // interval_minutes) + 20)
        liquidity_need = (
            int(self.liquidity_lookback_bars)
            if bool(self.enable_liquidity_filter)
            else 0
        )
        indicator_need = max(opening_range_bars, liquidity_need, 120)
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
            
            # Calculate time range
            delta = timedelta(minutes=1)
            now = datetime.now(timezone.utc)
            missing = required_history - current_count
            start_time = _floor_timestamp(now - (self._interval_delta * missing), delta)
            
            request = DataSubscriptionRequest(
                channel=self._resolve_bar_channel(),
                symbol=self.symbol,
                interval="1m",
                options={
                    "interval": "1m",
                    "start": start_time,
                    "end": now,
                },
            )
            
            try:
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
                except asyncio.TimeoutError:
                    self.logger.warning("Backfill timed out")
                    records = []
                
                ingested_count = 0
                if records:
                    previous_replay_state = self._history_replay_in_progress
                    self._history_replay_in_progress = True
                    try:
                        # Reset session state before replaying
                        self._recent_candles.clear()
                        # We don't reset _current_session because _ensure_session_state will handle it
                        
                        for item in records:
                            if not item:
                                continue
                            
                            # Normalize and ingest
                            # orb_fvg uses on_candle which expects normalized event or dict
                            # We use MinuteBarAggregator._normalise_record to get a normalized dict
                            normalized = MinuteBarAggregator._normalise_record(item)
                            if normalized is None:
                                continue
                                
                            # on_candle handles session state and accumulation
                            await self.on_candle(normalized)
                            ingested_count += 1
                    finally:
                        self._history_replay_in_progress = previous_replay_state
                
                self.logger.warning("DEBUG_STRATEGY: Manual backfill loaded %d records", len(records) if records else 0)
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

    # ------------------------------------------------------------------
    def _register_parameters(self) -> None:
        symbol_default = (self.symbol or "").upper()
        definitions = {
            "symbol": {
                "type": "str",
                "allow_null": True,
                "default": symbol_default,
                "label": "Symbol",
            },
            "interval": {
                "type": "str",
                "default": "1m",
                "readonly": True,
                "label": "Source Interval",
            },
            "intervals": {
                "type": "list",
                "default": ["1m"],
                "readonly": True,
                "label": "Subscribed Intervals",
            },
            "default_quantity": {
                "type": "int",
                "default": self.default_quantity,
                "min": 1,
                "max": 200,
                "label": "Order Quantity",
            },
            "session_open_time": {
                "type": "str",
                "default": self.session_open_time,
                "label": "Session Open (HH:MM)",
            },
            "session_timezone": {
                "type": "str",
                "default": self.session_timezone,
                "label": "Session Timezone",
            },
            "opening_range_minutes": {
                "type": "int",
                "default": self.opening_range_minutes,
                "min": 5,
                "max": 60,
                "label": "Opening Range Minutes",
            },
            "trade_window_minutes": {
                "type": "int",
                "default": self.trade_window_minutes,
                "min": 30,
                "max": 480,
                "label": "Trade Window Minutes",
            },
            "fvg_tolerance_points": {
                "type": "float",
                "default": self.fvg_tolerance_points,
                "min": 0.0,
                "max": 5.0,
                "label": "FVG Tolerance (pts)",
            },
            "risk_threshold_points": {
                "type": "float",
                "default": self.risk_threshold_points,
                "min": 1.0,
                "max": 100.0,
                "label": "Risk Threshold (points)",
            },
            "cooldown_seconds": {
                "type": "float",
                "default": self.cooldown_seconds,
                "min": 0.0,
                "max": 3600.0,
                "label": "Signal Cooldown (s)",
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
        sanitized = dict(updates)
        sanitized.pop("interval", None)
        sanitized.pop("intervals", None)
        applied = super().apply_parameter_updates(sanitized)
        if not applied:
            return applied
        self._force_runtime_intervals(interval="1m", intervals=["1m"])
        if "session_timezone" in applied:
            self._session_tz = self._load_timezone(self.session_timezone)
            self._current_session = None
        if "session_open_time" in applied:
            self._session_open_clock = self._parse_session_open(self.session_open_time)
            self._current_session = None
        if any(key.startswith("liquidity_") for key in applied) or "enable_liquidity_filter" in applied:
            self._configure_liquidity_filter()
        self._register_parameters()
        return applied

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
        enabled = bool(self.enable_liquidity_filter)
        passed = (not enabled) or (not trap_detected)
        self._telemetry_log(
            "Liquidity filter evaluated before ORB entry",
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
                    "entry_zone_ok_advisory": entry_zone_ok,
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
    def _load_timezone(self, name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:  # pragma: no cover - fallback to UTC
            self.logger.warning("Unknown timezone '%s', defaulting to UTC", name)
            return ZoneInfo("UTC")

    # ------------------------------------------------------------------
    def _parse_session_open(self, value: str) -> dtime:
        try:
            hour_str, minute_str = value.strip().split(":", 1)
            hour = max(0, min(23, int(hour_str)))
            minute = max(0, min(59, int(minute_str)))
            return dtime(hour=hour, minute=minute)
        except Exception:  # pragma: no cover - fallback to 09:30
            self.logger.warning(
                "Invalid session_open_time '%s', falling back to 09:30", value
            )
            return dtime(hour=9, minute=30)

    # ------------------------------------------------------------------
    def _ensure_session_state(self, local_ts: datetime) -> None:
        session_date = local_ts.date()
        if self._current_session == session_date:
            return
        self._current_session = session_date
        self._session_open_dt = datetime.combine(
            session_date, self._session_open_clock, tzinfo=self._session_tz
        )
        self._range_end_local = self._session_open_dt + timedelta(
            minutes=self.opening_range_minutes
        )
        self._range_high = None
        self._range_low = None
        self._range_confirmed = False
        self._trade_window_notified = False
        self._recent_candles.clear()
        self.logger.info(
            "Reset ORB session state session_date=%s open=%s range_end=%s",
            session_date,
            self._session_open_dt,
            self._range_end_local,
        )

    # ------------------------------------------------------------------
    async def on_candle(self, candle: Mapping[str, Any]) -> None:  # noqa: D401 - event hook
        if not bool(candle.get("is_closed", True)):
            return
        parsed = self._parse_timestamp(candle)
        if parsed is None:
            self._telemetry_log_signal_waiting(
                step="时间戳解析失败",
                reason="缺少end/start字段或格式无效",
                details={"symbol": self.symbol},
                status_code="timestamp_invalid",
            )
            return
        local_ts = parsed.astimezone(self._session_tz)
        self._ensure_session_state(local_ts)
        if self._session_open_dt is None or self._range_end_local is None:
            self._telemetry_log_signal_waiting(
                step="ORB会话初始化",
                reason="等待会话时间窗口初始化",
                details={"symbol": self.symbol},
                comparison="status",
            )
            return
        if local_ts < self._session_open_dt:
            self._telemetry_log_signal_waiting(
                step="等待开盘",
                reason="交易时段尚未开始",
                details={
                    "session_open": self._session_open_dt.isoformat(),
                    "current_time": local_ts.isoformat(),
                    "symbol": self.symbol,
                },
            )
            return
        close_price = self._safe_float(candle.get("close"))
        high_price = self._safe_float(candle.get("high"))
        low_price = self._safe_float(candle.get("low"))
        open_price = self._safe_float(candle.get("open", close_price))
        volume = self._safe_float(candle.get("volume", 0.0))
        if close_price is None or high_price is None or low_price is None or open_price is None:
            return
        snapshot = {
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
            "end_ts": parsed,
        }
        self._recent_candles.append(snapshot)
        if local_ts < self._range_end_local:
            self._range_high = max(self._range_high or high_price, high_price)
            self._range_low = min(self._range_low or low_price, low_price)
            self._telemetry_log_signal_waiting(
                step="构建开盘区间",
                reason="正在记录开盘区间高低点",
                details={
                    "range_window_end": self._range_end_local.isoformat(),
                    "current_time": local_ts.isoformat(),
                    "symbol": self.symbol,
                },
            )
            return
        if not self._range_confirmed:
            if self._range_high is not None and self._range_low is not None:
                self._range_confirmed = True
                self.logger.info(
                    "Opening range locked high=%.2f low=%.2f", self._range_high, self._range_low
                )
            else:
                self._telemetry_log_signal_waiting(
                    step="确认开盘区间",
                    reason="等待开盘区间界限",
                    details={"symbol": self.symbol},
                )
                return
        trade_window_end = self._session_open_dt + timedelta(
            minutes=self.trade_window_minutes
        )
        if local_ts > trade_window_end:
            if not self._trade_window_notified:
                self._trade_window_notified = True
                self.logger.debug(
                    "ORB trade window closed",
                    extra={
                        "event": "strategy.signal.window_closed",
                        "strategy": self.name,
                        "symbol": self.symbol,
                        "session": str(self._current_session),
                    },
                )
            self._telemetry_log_signal_waiting(
                step="交易窗口结束",
                reason="开盘交易窗口已结束",
                details={
                    "session": str(self._current_session),
                    "symbol": self.symbol,
                },
                status_code="window_closed",
            )
            return
        if len(self._recent_candles) < 3:
            self._telemetry_log_signal_waiting(
                step="FVG检测",
                reason="等待形成3根K线以检测FVG",
                metric=float(len(self._recent_candles)),
                threshold=3.0,
                comparison="bars",
                details={"symbol": self.symbol},
            )
            return
        last_three = list(self._recent_candles)[-3:]
        now = parsed.timestamp()
        if self.cooldown_seconds > 0 and (now - self._last_signal_time) < self.cooldown_seconds:
            remaining = max(self.cooldown_seconds - (now - self._last_signal_time), 0.0)
            self._telemetry_log_signal_waiting(
                step="冷却中",
                reason="信号冷却中",
                metric=remaining,
                threshold=float(self.cooldown_seconds),
                comparison="seconds",
                details={"remaining_seconds": round(remaining, 2), "symbol": self.symbol},
                status_code="cooldown",
            )
            return

        # Check for existing position to prevent multiple orders
        current_pos = self._get_current_position()
        if abs(current_pos) > 0:
            # Already in position, skip signal generation
            return

        breakout_high = high_price
        breakout_low = low_price
        if (
            self._range_high is not None
            and breakout_high > self._range_high
        ):
            if self._is_bullish_fvg(last_three):
                entry_price = max(close_price, self._range_high)
                liquidity_ok, _ = self._evaluate_liquidity_gate(
                    side="BUY",
                    reference_price=entry_price,
                    timestamp=parsed,
                    reason="orb_fvg_bullish_breakout",
                )
                if not liquidity_ok:
                    return
                result = self._handle_entry(
                    side="BUY",
                    candles=last_three,
                    entry_price=entry_price,
                    range_boundary=self._range_high,
                    candle_ts=parsed,
                )
                if result.queued:
                    self.logger.info(
                        "ORB 多头信号已发单",
                        extra={
                            "event": "strategy.signal.dispatched",
                            "strategy": self.name,
                            "symbol": self.symbol,
                            "side": "BUY",
                            "status_code": result.status_code,
                            "reason": result.reason,
                        },
                    )
                    self._telemetry_log(
                        "ORB 多头信号已发送下单请求",
                        level="INFO",
                        tone="positive",
                        phase=self._PHASE_SIGNALS,
                        details={
                            "symbol": getattr(self, "symbol", "") or "",
                            "side": "BUY",
                            "status_code": result.status_code,
                            "reason": result.reason,
                        },
                        deduplicate=False,
                    )
                    self._last_signal_time = now
                elif result.risk_blocked:
                    self.logger.warning(
                        "ORB 多头信号被风控阻断",
                        extra={
                            "event": "strategy.signal.risk_blocked",
                            "strategy": self.name,
                            "symbol": self.symbol,
                            "side": "BUY",
                            "status_code": result.status_code,
                            "reason": result.reason,
                            "risk_log_level": "WARN",
                            "payload": {
                                "risk_action": "order_block",
                                "risk_status": "blocked",
                                "reason": result.reason,
                            },
                        },
                    )
                    self._telemetry_log(
                        "ORB 多头信号被风控阻断",
                        level="WARN",
                        tone="warning",
                        phase=self._PHASE_SIGNALS,
                        details={
                            "symbol": getattr(self, "symbol", "") or "",
                            "side": "BUY",
                            "status_code": result.status_code,
                            "reason": result.reason,
                        },
                        deduplicate=False,
                    )
                return
            else:
                 self.logger.debug("Breakout HIGH but no FVG. Range High: %.2f, Breakout: %.2f", self._range_high, breakout_high)
            fvg_ok = self._is_bullish_fvg(last_three)
            c2 = last_three[1]
            c1 = last_three[0]
            c3 = last_three[2]
            c2_close = self._safe_float(c2.get("close"))
            c2_open = self._safe_float(c2.get("open"))
            c1_high = self._safe_float(c1.get("high"))
            # c2_low is unused in evaluation; omit to satisfy lint
            c3_low = self._safe_float(c3.get("low"))
            tolerance = float(self.fvg_tolerance_points)
            evaluations = [
                {"condition": "c2_close>c2_open", "current": {"c2_close": c2_close, "c2_open": c2_open}, "passed": bool(c2_close is not None and c2_open is not None and c2_close > c2_open)},
                {"condition": f"c3_low-c1_high>=-{tolerance}", "current": (None if (c3_low is None or c1_high is None) else (c3_low - c1_high)), "threshold": -tolerance, "passed": bool(c3_low is not None and c1_high is not None and (c3_low - c1_high) >= -tolerance)},
            ]
            self._telemetry_set_phase_status(self._PHASE_SIGNALS, status="evaluated", status_code="conditions_checked")
            self._telemetry_log(
                "ORB bullish FVG conditions evaluated",
                level="INFO",
                tone="neutral",
                phase=self._PHASE_SIGNALS,
                details={
                    "symbol": getattr(self, "symbol", "") or "",
                    "range_high": float(self._range_high or 0.0),
                    "penetration": float(breakout_high - (self._range_high or breakout_high)),
                    "fvg_ok": bool(fvg_ok),
                    "evaluations": evaluations,
                },
                deduplicate=False,
            )
        if (
            self._range_low is not None
            and breakout_low < self._range_low
        ):
            if self._is_bearish_fvg(last_three):
                entry_price = min(close_price, self._range_low)
                liquidity_ok, _ = self._evaluate_liquidity_gate(
                    side="SELL",
                    reference_price=entry_price,
                    timestamp=parsed,
                    reason="orb_fvg_bearish_breakout",
                )
                if not liquidity_ok:
                    return
                result = self._handle_entry(
                    side="SELL",
                    candles=last_three,
                    entry_price=entry_price,
                    range_boundary=self._range_low,
                    candle_ts=parsed,
                )
                if result.queued:
                    self.logger.info(
                        "ORB 空头信号已发单",
                        extra={
                            "event": "strategy.signal.dispatched",
                            "strategy": self.name,
                            "symbol": self.symbol,
                            "side": "SELL",
                            "status_code": result.status_code,
                            "reason": result.reason,
                        },
                    )
                    self._telemetry_log(
                        "ORB 空头信号已发送下单请求",
                        level="INFO",
                        tone="positive",
                        phase=self._PHASE_SIGNALS,
                        details={
                            "symbol": getattr(self, "symbol", "") or "",
                            "side": "SELL",
                            "status_code": result.status_code,
                            "reason": result.reason,
                        },
                        deduplicate=False,
                    )
                    self._last_signal_time = now
                elif result.risk_blocked:
                    self.logger.warning(
                        "ORB 空头信号被风控阻断",
                        extra={
                            "event": "strategy.signal.risk_blocked",
                            "strategy": self.name,
                            "symbol": self.symbol,
                            "side": "SELL",
                            "status_code": result.status_code,
                            "reason": result.reason,
                            "risk_log_level": "WARN",
                            "payload": {
                                "risk_action": "order_block",
                                "risk_status": "blocked",
                                "reason": result.reason,
                            },
                        },
                    )
                    self._telemetry_log(
                        "ORB 空头信号被风控阻断",
                        level="WARN",
                        tone="warning",
                        phase=self._PHASE_SIGNALS,
                        details={
                            "symbol": getattr(self, "symbol", "") or "",
                            "side": "SELL",
                            "status_code": result.status_code,
                            "reason": result.reason,
                        },
                        deduplicate=False,
                    )
                return
            else:
                 self.logger.debug("Breakout LOW but no FVG. Range Low: %.2f, Breakout: %.2f", self._range_low, breakout_low)
            fvg_ok = self._is_bearish_fvg(last_three)
            c2 = last_three[1]
            c1 = last_three[0]
            c3 = last_three[2]
            c2_close = self._safe_float(c2.get("close"))
            c2_open = self._safe_float(c2.get("open"))
            c1_low = self._safe_float(c1.get("low"))
            # c2_high is unused in evaluation; omit to satisfy lint
            c3_high = self._safe_float(c3.get("high"))
            tolerance = float(self.fvg_tolerance_points)
            evaluations = [
                {"condition": "c2_close<c2_open", "current": {"c2_close": c2_close, "c2_open": c2_open}, "passed": bool(c2_close is not None and c2_open is not None and c2_close < c2_open)},
                {"condition": f"c1_low-c3_high>=-{tolerance}", "current": (None if (c1_low is None or c3_high is None) else (c1_low - c3_high)), "threshold": -tolerance, "passed": bool(c1_low is not None and c3_high is not None and (c1_low - c3_high) >= -tolerance)},
            ]
            self._telemetry_set_phase_status(self._PHASE_SIGNALS, status="evaluated", status_code="conditions_checked")
            self._telemetry_log(
                "ORB bearish FVG conditions evaluated",
                level="INFO",
                tone="neutral",
                phase=self._PHASE_SIGNALS,
                details={
                    "symbol": getattr(self, "symbol", "") or "",
                    "range_low": float(self._range_low or 0.0),
                    "penetration": float((self._range_low or breakout_low) - breakout_low),
                    "fvg_ok": bool(fvg_ok),
                    "evaluations": evaluations,
                },
                deduplicate=False,
            )

    # ------------------------------------------------------------------
    def _handle_entry(
        self,
        *,
        side: str,
        candles: list[Mapping[str, float]],
        entry_price: float,
        range_boundary: float,
        candle_ts: datetime,
    ) -> EntryResult:
        quantity = self._determine_quantity(entry_price)
        # Check if we can open a new trade
        if not self.can_open_new_trade(side, quantity):
            return EntryResult(False, True, "risk_blocked", "risk_blocked")
        if quantity <= 0:
            self.logger.warning(
                "Skipping ORB entry due to non-positive quantity",
                extra={
                    "strategy": self.name,
                    "symbol": self.symbol,
                    "side": side,
                    "status_code": "quantity_non_positive",
                    "reason": "quantity_non_positive",
                    "quantity": quantity,
                },
            )
            self._telemetry_log(
                "入场数量为 0，跳过下单",
                level="WARN",
                tone="warning",
                phase=self._PHASE_SIGNALS,
                details={
                    "symbol": getattr(self, "symbol", "") or "",
                    "side": side,
                    "quantity": float(quantity),
                    "status_code": "quantity_non_positive",
                    "reason": "quantity_non_positive",
                },
                deduplicate=False,
            )
            return EntryResult(False, False, "quantity_non_positive", "quantity_non_positive")
        stop_price = self._compute_stop(side, candles)
        if stop_price is None:
            self.logger.warning(
                "Skipping ORB entry due to invalid stop price",
                extra={
                    "strategy": self.name,
                    "symbol": self.symbol,
                    "side": side,
                    "status_code": "invalid_stop_loss",
                    "reason": "invalid_stop_loss",
                },
            )
            self._telemetry_log(
                "未能计算止损价格，跳过 ORB 入场",
                level="WARN",
                tone="warning",
                phase=self._PHASE_SIGNALS,
                details={
                    "symbol": getattr(self, "symbol", "") or "",
                    "side": side,
                    "status_code": "invalid_stop_loss",
                    "reason": "invalid_stop_loss",
                },
                deduplicate=False,
            )
            return EntryResult(False, False, "invalid_stop_loss", "invalid_stop_loss")
        risk = entry_price - stop_price if side == "BUY" else stop_price - entry_price
        if risk <= 0:
            self.logger.warning(
                "Skipping ORB entry due to non-positive risk distance",
                extra={
                    "strategy": self.name,
                    "symbol": self.symbol,
                    "side": side,
                    "status_code": "invalid_risk",
                    "reason": "invalid_risk",
                    "risk": float(risk),
                },
            )
            self._telemetry_log(
                "风险点数无效，跳过 ORB 入场",
                level="WARN",
                tone="warning",
                phase=self._PHASE_SIGNALS,
                details={
                    "symbol": getattr(self, "symbol", "") or "",
                    "side": side,
                    "status_code": "invalid_risk",
                    "reason": "invalid_risk",
                    "risk_points": float(risk),
                },
                deduplicate=False,
            )
            return EntryResult(False, False, "invalid_risk", "invalid_risk")
        target = self._compute_target(side, entry_price, risk)
        exchange, sec_type = self._resolve_instrument(self.symbol)
        metadata: Dict[str, Any] = {
            "entry_price": float(entry_price),
            "range_high": float(self._range_high or 0.0),
            "range_low": float(self._range_low or 0.0),
            "range_boundary": float(range_boundary),
            "risk_points": float(risk),
            "risk_multiple": 2.0 if risk <= self.risk_threshold_points else 1.0,
            "timestamp": candle_ts.astimezone(timezone.utc).isoformat(),
            "quantity": float(quantity),
        }
        exit_mode = getattr(getattr(self, "exit_config", None), "mode", ExitMode.NONE)
        if exit_mode is ExitMode.NONE:
            metadata["stop_loss"] = float(stop_price)
            metadata["take_profit"] = float(target)
        else:
            metadata["exit_mode"] = exit_mode.value
            exit_targets = self.evaluate_exit_signal(
                position=float(quantity) * (1.0 if side == "BUY" else -1.0),
                entry_price=float(entry_price),
                account_equity=getattr(self, "account_equity", None),
                bar=candles[-1],
                is_dom=False,
            )
            if exit_targets is not None:
                if exit_targets.stop_loss is not None:
                    metadata["evaluated_stop_loss"] = float(exit_targets.stop_loss)
                if exit_targets.take_profit is not None:
                    metadata["evaluated_take_profit"] = float(exit_targets.take_profit)

        order_payload: Dict[str, Any] = {
            "side": side,
            "quantity": float(quantity),
            "order_type": "MARKET",
            "symbol": self.symbol,
            "exchange": exchange,
            "sec_type": sec_type,
            "reason": "orb_fvg_entry",
            "metadata": metadata,
        }

        if not self.queue_order(order_payload):
            self.logger.warning(
                "Failed to queue ORB FVG order for runner",
                extra={
                    "event": "strategy.order.queue_failed",
                    "strategy": self.name,
                    "symbol": self.symbol,
                    "side": side,
                    "quantity": quantity,
                    "status_code": "queue_failed",
                    "reason": "queue_failed",
                },
            )
            self._telemetry_log(
                "下单请求队列失败",
                level="WARN",
                tone="warning",
                phase=self._PHASE_SIGNALS,
                details={
                    "symbol": getattr(self, "symbol", "") or "",
                    "side": side,
                    "status_code": "queue_failed",
                    "reason": "queue_failed",
                    "quantity": float(quantity),
                },
                deduplicate=False,
            )
            return EntryResult(False, False, "queue_failed", "queue_failed")

        log_extra = {
            "event": "strategy.order.signal",
            "strategy": self.name,
            "symbol": self.symbol,
            "side": side,
            "quantity": float(quantity),
            "entry_price": float(entry_price),
            "stop_loss": float(stop_price),
            "take_profit": float(target),
            "risk_points": float(risk),
        }
        self.logger.info("ORB FVG order queued for execution", extra=log_extra)
        self.logger.debug("ORB payload: %s", order_payload)
        return EntryResult(True, False, "order_queued", "order_queued")

    def _get_current_position(self) -> float:
        """Get current position quantity from dependencies or state."""
        # Check injected position provider first (used in simulation/backtest)
        # Try attribute first, then fallback to _dependencies dict (safeguard for slots/dataclass issues)
        provider = getattr(self, "position_provider", None)
        if provider is None:
            # _dependencies is likely on the base class or injected at runtime
            deps = getattr(self, "_dependencies", None)
            if isinstance(deps, dict):
                provider = deps.get("position_provider")
        
        if callable(provider):
            try:
                resolved = self._resolve_maybe_awaitable_float(
                    provider(self.symbol),
                    default=0.0,
                    label=f"{self.name}.position_provider",
                )
                return float(resolved or 0.0)
            except Exception:
                pass

        # Fallback to internal state if available
        risk = getattr(self, "risk_manager", None)
        if risk is None:
            return 0.0
        # In a real strategy, we might check self.state or similar, but for simulation
        # the provider is the source of truth.
        return 0.0

    # ------------------------------------------------------------------
    def _compute_stop(self, side: str, candles: list[Mapping[str, float]]) -> Optional[float]:
        if len(candles) < 3:
            return None
        if side == "BUY":
            candidates = [
                self._safe_float(candles[0].get("high")),
                self._safe_float(candles[1].get("low")),
                self._safe_float(candles[2].get("low")),
            ]
            levels = [value for value in candidates if value is not None]
            if not levels:
                return None
            base = min(levels)
            return base - float(self.fvg_tolerance_points)
        candidates = [
            self._safe_float(candles[0].get("low")),
            self._safe_float(candles[1].get("high")),
            self._safe_float(candles[2].get("high")),
        ]
        levels = [value for value in candidates if value is not None]
        if not levels:
            return None
        base = max(levels)
        return base + float(self.fvg_tolerance_points)

    # ------------------------------------------------------------------
    def _compute_target(self, side: str, entry: float, risk: float) -> float:
        multiple = 2.0 if risk <= self.risk_threshold_points else 1.0
        if side == "BUY":
            return entry + risk * multiple
        return entry - risk * multiple

    # ------------------------------------------------------------------
    def _determine_quantity(self, price: float) -> int:
        return max(1, int(self.default_quantity))

    # ------------------------------------------------------------------
    def _resolve_instrument(self, symbol: str) -> tuple[str, str]:
        base = symbol.upper()
        if base in DEFAULT_INSTRUMENT_DETAILS:
            details = DEFAULT_INSTRUMENT_DETAILS[base]
            return details.get("exchange", "CME"), details.get("sec_type", "FUT")
        for key, details in DEFAULT_INSTRUMENT_DETAILS.items():
            if base.startswith(key):
                return details.get("exchange", "CME"), details.get("sec_type", "FUT")
        return "SMART", "STK"

    def _parse_timestamp(self, candle: Mapping[str, Any]) -> Optional[datetime]:
        timestamp = (
            candle.get("end")
            or candle.get("close_time")
            or candle.get("timestamp")
            or candle.get("start")
            or candle.get("open_time")
        )
        if timestamp is None:
            self.logger.debug("Skipping candle without timestamp: %s", candle)
            return None
        if isinstance(timestamp, datetime):
            ts = timestamp
        elif isinstance(timestamp, str):
            text = timestamp.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                ts = datetime.fromisoformat(text)
            except ValueError:
                self.logger.debug("Failed to parse timestamp string '%s'", timestamp)
                return None
        else:
            self.logger.debug("Unsupported timestamp type %s", type(timestamp))
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)

    # ------------------------------------------------------------------
    def _safe_float(self, value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    def _is_bullish_fvg(self, candles: list[Mapping[str, float]]) -> bool:
        if len(candles) < 3:
            return False
        c1, c2, c3 = candles
        c1_high = self._safe_float(c1.get("high"))
        c3_low = self._safe_float(c3.get("low"))
        c2_close = self._safe_float(c2.get("close"))
        c2_open = self._safe_float(c2.get("open"))
        c2_low = self._safe_float(c2.get("low"))
        if None in {c1_high, c2_low, c3_low, c2_close, c2_open}:
            return False
        # if c2_close <= c2_open:
        #     self.logger.info("Bullish FVG Fail: C2 is not green (Close=%.2f, Open=%.2f)", c2_close, c2_open)
            #     return False
            tolerance = self.fvg_tolerance_points
            gaps = [
                c3_low - c1_high,
            ]
            passed = all(gap >= -tolerance for gap in gaps)
            if not passed:
                self.logger.debug("Bullish FVG Fail: Gap too small (C3L=%.2f, C1H=%.2f, Diff=%.2f, Tol=%.2f)", c3_low, c1_high, c3_low - c1_high, tolerance)
            return passed

    # ------------------------------------------------------------------
    def _is_bearish_fvg(self, candles: list[Mapping[str, float]]) -> bool:
        if len(candles) < 3:
            return False
        c1, c2, c3 = candles
        c1_low = self._safe_float(c1.get("low"))
        c3_high = self._safe_float(c3.get("high"))
        c2_close = self._safe_float(c2.get("close"))
        c2_open = self._safe_float(c2.get("open"))
        c2_high = self._safe_float(c2.get("high"))
        if None in {c1_low, c2_high, c3_high, c2_close, c2_open}:
            return False
        # if c2_close >= c2_open:
        #     self.logger.info("Bearish FVG Fail: C2 is not red (Close=%.2f, Open=%.2f)", c2_close, c2_open)
        #     return False
        tolerance = self.fvg_tolerance_points
        gaps = [
            c1_low - c3_high,
        ]
        passed = all(gap >= -tolerance for gap in gaps)
        if not passed:
            self.logger.info("Bearish FVG Fail: Gap too small (C1L=%.2f, C3H=%.2f, Diff=%.2f, Tol=%.2f)", c1_low, c3_high, c1_low - c3_high, tolerance)
        return passed
