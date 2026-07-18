"""Momentum-support buy-the-dip strategy using unified candle data."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional
from ati_shared_sdk.common.market_data.aggregation import floor_timestamp as _floor_timestamp
from ati_shared_sdk.data_layer import DataSubscriptionRequest
from .candle import CandleSubscriptionStrategy
from .templates import StrategyTemplate

DEFAULT_INSTRUMENT_DETAILS: Mapping[str, Mapping[str, str]] = {
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


@dataclass
class BuyTheDipStrategy(CandleSubscriptionStrategy, StrategyTemplate):
    """Buy when price pulls back to support but momentum/flow turn positive."""

    name: str = "Buy The Dip"
    strategy_type = "buy_the_dip"
    use_base_exit = False
    description: str = (
        "当价格回踩短期均线且动量指标恢复向上时自动买入，结合成交量差确认买盘"
    )
    symbol: str = "ES"
    interval: str = "5m"
    history_limit: int = 240
    default_quantity: int = 1
    cooldown_seconds: float = 120.0
    rsi_period: int = 14
    rsi_oversold: float = 35.0
    rsi_overbought: float = 65.0
    ma_fast_period: int = 20
    ma_slow_period: int = 50
    dip_threshold: float = 0.001
    cvd_window: int = 20
    cvd_z_entry: float = 0.0
    cvd_z_entry_short: float = 0.0
    trend_tolerance: float = 0.001
    _last_order_payload: Optional[Dict[str, Any]] = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:  # noqa: D401 - defer to parent docstring
        super().__post_init__()
        self.intervals = [self.interval]
        self.default_quantity = max(1, int(self.default_quantity))
        self.cooldown_seconds = max(0.0, float(self.cooldown_seconds))
        self.rsi_period = max(2, int(self.rsi_period))
        self.rsi_oversold = float(self.rsi_oversold)
        self.rsi_overbought = float(self.rsi_overbought)
        self.ma_fast_period = max(2, int(self.ma_fast_period))
        self.ma_slow_period = max(self.ma_fast_period + 1, int(self.ma_slow_period))
        self.dip_threshold = float(self.dip_threshold)  # Allow negative for testing
        self.cvd_window = max(5, int(self.cvd_window))
        self.cvd_z_entry = float(self.cvd_z_entry)
        self.cvd_z_entry_short = float(self.cvd_z_entry_short)
        self.trend_tolerance = max(0.0, float(self.trend_tolerance))
        self.summary_points = [
            "订阅统一K线数据并计算RSI、均线乖离与CVD z-score",
            "价格跌破快线、RSI超卖且CVD转正时触发买入",
            "通过冷却期与风控参数控制重复信号，队列统一执行",
        ]
        self.file_path = "src/strategies/buy_the_dip.py"
        self._register_parameters()

    def _bar_inactivity_threshold_seconds(self, interval: str | None = None) -> float:
        return max(super()._bar_inactivity_threshold_seconds(interval), 300.0)

    def _bootstrap_history_target(self) -> int:
        indicator_need = max(
            int(self.ma_slow_period),
            int(self.cvd_window),
            int(self.rsi_period) * 2,
        ) + 20
        return max(60, min(int(self.history_limit), indicator_need))

    async def on_start(self) -> None:
        """Initialize strategy and perform active history backfill."""
        await super().on_start()
        await self._await_market_data_ready_and_subscribe()
        if self._history_replay_in_progress:
            return

        # Set initial status
        self._telemetry_set_phase_status(
            self._PHASE_SIGNALS,
            status="running",
            status_code="starting",
            status_reason="Strategy starting...",
            timestamp=datetime.now(timezone.utc),
        )

        # Active history retrieval (Backfill)
        with self._candles_lock:
            candles = self.get_candles(self.interval)
            current_count = len(candles)

        required_history = self._bootstrap_history_target()
        if current_count < required_history:
            self._telemetry_set_phase_status(
                self._PHASE_SIGNALS,
                status="running",
                status_code="backfilling",
                status_reason=f"Backfilling history ({current_count}/{required_history})",
                timestamp=datetime.now(timezone.utc),
            )
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
                    # Temporarily set replay flag to prevent duplicate signal processing during ingestion
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
                                    if not isinstance(event, Mapping):
                                        continue
                                    with self._candles_lock:
                                        target = self.get_candles(self.interval)
                                        while target and not isinstance(target[-1], Mapping):
                                            target.pop()
                                        # Avoid duplicates based on end time
                                        if not target or target[-1].get("end") != event.get("end"):
                                            target.append(event)
                                            ingested_count += 1
                                    # We can optionally call on_candle here if we want to update indicators state
                                    # But since we are just filling the buffer, we might skip logic execution
                                    # However, for indicators like EMA/RSI to be correct, we SHOULD update them.
                                    # Since on_candle handles indicator updates, let's call it.
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
    def _register_parameters(self) -> None:
        symbol_default = (self.symbol or "").upper()
        definitions = {
            "symbol": {
                "type": "str",
                "allow_null": True,
                "default": symbol_default,
                "label": "Symbol",
                "description": "订阅与交易的标的代码。",
            },
            "default_quantity": {
                "type": "int",
                "default": self.default_quantity,
                "min": 1,
                "max": 200,
                "label": "Order Quantity",
            },
            "cooldown_seconds": {
                "type": "float",
                "default": self.cooldown_seconds,
                "min": 0.0,
                "max": 1800.0,
                "label": "Signal Cooldown (s)",
                "step": 5.0,
            },
            "signal_frequency_seconds": {
                "type": "float",
                "default": getattr(self, "signal_frequency_seconds", 0.0),
                "min": 0.0,
                "max": 3600.0,
                "label": "Execution Frequency (s)",
                "step": 5.0,
            },
            "max_loss_streak": {
                "type": "int",
                "default": getattr(self, "max_loss_streak", 3),
                "min": 1,
                "max": 10,
                "label": "Breaker Loss Streak",
            },
            "rsi_period": {
                "type": "int",
                "default": self.rsi_period,
                "min": 5,
                "max": 50,
                "label": "RSI Period",
            },
            "rsi_oversold": {
                "type": "float",
                "default": self.rsi_oversold,
                "min": 5.0,
                "max": 60.0,
                "label": "RSI Oversold Threshold",
            },
            "rsi_overbought": {
                "type": "float",
                "default": self.rsi_overbought,
                "min": 40.0,
                "max": 95.0,
                "label": "RSI Overbought Threshold",
            },
            "ma_fast_period": {
                "type": "int",
                "default": self.ma_fast_period,
                "min": 5,
                "max": 100,
                "label": "Fast MA Period",
            },
            "ma_slow_period": {
                "type": "int",
                "default": self.ma_slow_period,
                "min": 10,
                "max": 200,
                "label": "Slow MA Period",
            },
            "dip_threshold": {
                "type": "float",
                "default": self.dip_threshold,
                "min": 0.0005,
                "max": 0.05,
                "label": "MA Pullback Threshold",
                "description": "价格低于快线的最小回调比例 (0.001=0.1%)。",
                "step": 0.0005,
            },
            "cvd_window": {
                "type": "int",
                "default": self.cvd_window,
                "min": 10,
                "max": 200,
                "label": "CVD Window",
            },
            "cvd_z_entry": {
                "type": "float",
                "default": self.cvd_z_entry,
                "min": -5.0,
                "max": 5.0,
                "label": "CVD Z-score Threshold",
            },
            "cvd_z_entry_short": {
                "type": "float",
                "default": self.cvd_z_entry_short,
                "min": -5.0,
                "max": 5.0,
                "label": "CVD Z-score Threshold (Short)",
            },
            "trend_tolerance": {
                "type": "float",
                "default": self.trend_tolerance,
                "min": 0.0,
                "max": 0.01,
                "label": "Trend Tolerance",
                "description": "允许快慢线之间的最大回撤百分比 (0.001=0.1%)。",
                "step": 0.0005,
            },
        }
        self.set_parameter_definitions(definitions)

    # ------------------------------------------------------------------
    def apply_parameter_updates(self, updates: Mapping[str, Any]) -> Dict[str, Any]:
        return super().apply_parameter_updates(updates)

    # ------------------------------------------------------------------
    async def on_candle(self, candle: Mapping[str, Any]) -> None:  # noqa: D401 - event hook
        is_backtest = getattr(self, "runtime_mode", "") == "backtest"
        # Add immediate heartbeat to confirm data reception
        ts = self._extract_candle_end(candle)
        if not is_backtest:
            self._telemetry_set_phase_status(
                self._PHASE_SIGNALS,
                status="running",
                status_code="receiving_data",
                status_reason="Received candle event",
                status_details={"symbol": self.symbol},
                timestamp=ts if isinstance(ts, datetime) else None,
            )

        bar_end_dt = self._extract_candle_end(candle)
        bar_end = bar_end_dt.isoformat() if isinstance(bar_end_dt, datetime) else None
        bar_close = None
        try:
            bar_close = float(candle.get("close"))
        except (TypeError, ValueError):
            bar_close = None
        if getattr(self, "_history_replay_in_progress", False):
            self.logger.debug("Skipping on_candle: history replay in progress")
            return
        if isinstance(bar_end_dt, datetime):
            # Use wall clock time (simulation-aware) instead of system time
            try:
                now_ts = self._wall_clock_now()
                now = datetime.fromtimestamp(now_ts, tz=timezone.utc)
            except Exception:
                now = datetime.now(timezone.utc)
            
            # Debug logging for time check
            time_diff = (now - bar_end_dt).total_seconds()
            delta_seconds = self._interval_delta.total_seconds()
            if time_diff > delta_seconds:
                self.logger.debug(f"Skipping stale candle: time_diff={time_diff}s > delta={delta_seconds}s. Now={now}, Bar={bar_end_dt}")
                return

        if getattr(self, "runtime_mode", "") != "backtest":
            self.logger.info(f"Processing candle: {bar_end} (Close: {bar_close})")

        candles = [
            item
            for item in self.get_candles()
            if bool(item.get("is_closed", True))
        ]
        minimum_history = max(self.ma_slow_period, self.cvd_window) + 5
        history_window = max(minimum_history, int(self.history_limit or minimum_history))
        if len(candles) > history_window:
            candles = candles[-history_window:]
        self.logger.debug(f"on_candle: have {len(candles)} candles, need {minimum_history}")
        
        # Active history retrieval (Backfill)
        if len(candles) < minimum_history:
            if getattr(self, "_use_unified_data", False) and not getattr(self, "_initial_backfill_requested", False):
                try:
                    have = len(candles)
                    missing = max(minimum_history - have, 1)
                    delta = self._interval_delta
                    
                    try:
                        now_ts = self._wall_clock_now()
                        now = datetime.fromtimestamp(now_ts, tz=timezone.utc)
                    except Exception:
                        now = datetime.now(timezone.utc)
                        
                    start = _floor_timestamp(now - (delta * missing), delta)
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
                    # Use base class loader which handles backoff and data source fallback
                    records = await self._load_history_records(
                        request=request,
                        start=start,
                        end=now,
                        interval=delta,
                        # Use default config
                    )
                    
                    self._reset_unified_bucket()
                    ingested_count = 0
                    if records:
                        for item in records:
                            if not item:
                                continue
                            # Ingest into base class logic
                            closed = self._ingest_bar_payload(item)
                            if closed:
                                closed_events = closed if isinstance(closed, list) else [closed]
                                for event in closed_events:
                                    if not isinstance(event, Mapping):
                                        continue
                                    with self._candles_lock:
                                        target = self.get_candles(self.interval)
                                        while target and not isinstance(target[-1], Mapping):
                                            target.pop()
                                        if target and target[-1].get("end") == event.get("end"):
                                            target[-1] = event
                                        else:
                                            target.append(event)
                                    ingested_count += 1
                    
                    self.logger.info(
                        "BuyTheDip backfilled history",
                        extra={
                             "requested": missing,
                             "ingested": ingested_count,
                             "symbol": self.symbol
                        }
                    )
                    
                    # Refresh local candles list after backfill
                    candles = [
                        item
                        for item in self.get_candles()
                        if bool(item.get("is_closed", True))
                    ]
                except Exception:
                    self.logger.exception("BuyTheDip history backfill failed")
                finally:
                    self._initial_backfill_requested = True

        if len(candles) < minimum_history:
            if not is_backtest:
                self._telemetry_set_phase_status(
                    self._PHASE_SIGNALS,
                    status="running",
                    status_code="accumulating_history",
                    status_reason=f"Accumulating history ({len(candles)}/{minimum_history})",
                    status_details={
                        "have_bars": len(candles),
                        "need_bars": minimum_history,
                        "symbol": self.symbol,
                    },
                )
                self._telemetry_log_signal_waiting(
                    step="等待K线历史",
                    reason="等待足够的历史K线以计算指标",
                    metric=float(len(candles)),
                    threshold=float(minimum_history),
                    comparison="bars",
                    details={
                        "have_bars": len(candles),
                        "need_bars": minimum_history,
                        "symbol": self.symbol,
                    },
                )
            self.logger.debug(
                "Buy-the-dip shortfall: insufficient candle history",
                extra={
                    "event": "strategy.signal.history_short",
                    "strategy": self.name,
                    "symbol": self.symbol,
                    "have": len(candles),
                    "need": minimum_history,
                },
            )
            return
        processed: List[Dict[str, float]] = []
        for item in candles:
            try:
                close = float(item.get("close"))
                open_ = float(item.get("open", close))
                volume = float(item.get("volume", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            processed.append({"close": close, "open": open_, "volume": volume})
        if len(processed) < minimum_history:
            self.logger.debug(f"Insufficient processed candles: {len(processed)} < {minimum_history}")
            if not is_backtest:
                self._telemetry_set_phase_status(
                    self._PHASE_SIGNALS,
                    status="running",
                    status_code="accumulating_history",
                    status_reason=f"Accumulating valid history ({len(processed)}/{minimum_history})",
                    status_details={
                        "valid_bars": len(processed),
                        "need_bars": minimum_history,
                        "symbol": self.symbol,
                    },
                )
                self._telemetry_log_signal_waiting(
                    step="K线清洗",
                    reason="有效K线数量不足，继续等待",
                    metric=float(len(processed)),
                    threshold=float(minimum_history),
                    comparison="bars",
                    details={
                        "valid_bars": len(processed),
                        "need_bars": minimum_history,
                        "symbol": self.symbol,
                    },
                )
            self.logger.debug(
                "Buy-the-dip filtered: missing valid candles after coercion",
                extra={
                    "event": "strategy.signal.history_filtered",
                    "strategy": self.name,
                    "symbol": self.symbol,
                    "have": len(processed),
                    "need": minimum_history,
                },
            )
            return
        closes = [entry["close"] for entry in processed]
        opens = [entry["open"] for entry in processed]
        volumes = [entry["volume"] for entry in processed]
        ma_fast_series = self._moving_average(closes, self.ma_fast_period)
        ma_slow_series = self._moving_average(closes, self.ma_slow_period)
        fast = ma_fast_series[-1]
        slow = ma_slow_series[-1]
        if fast is None or slow is None or fast <= 0 or slow <= 0:
            self.logger.debug(f"Invalid MAs: fast={fast}, slow={slow}")
            self.logger.debug(
                "Buy-the-dip filtered: invalid moving averages",
                extra={
                    "event": "strategy.signal.metrics_unavailable",
                    "strategy": self.name,
                    "symbol": self.symbol,
                    "ma_fast": fast,
                    "ma_slow": slow,
                },
            )
            return
        rsi_series = self._compute_rsi(closes, self.rsi_period)
        rsi_value = rsi_series[-1]
        if rsi_value is None or math.isnan(rsi_value):
            self.logger.debug(f"Invalid RSI: {rsi_value}")
            if not is_backtest:
                self._telemetry_log_signal_waiting(
                    step="RSI计算",
                    reason="RSI尚未计算完成，等待更多数据",
                    details={"symbol": self.symbol},
                    comparison="status",
                )
            self.logger.debug(
                "Buy-the-dip filtered: RSI unavailable",
                extra={
                    "event": "strategy.signal.metrics_unavailable",
                    "strategy": self.name,
                    "symbol": self.symbol,
                },
            )
            return
        cvd_series = self._compute_cvd(opens, closes, volumes)
        z_cvd = self._rolling_zscore(cvd_series, self.cvd_window)
        ma_gap = (closes[-1] - fast) / fast
        dip_ok = ma_gap <= -self.dip_threshold
        rsi_ok = rsi_value <= self.rsi_oversold
        cvd_ok = z_cvd >= self.cvd_z_entry
        trend_ok = fast >= slow * (1 - self.trend_tolerance)
        short_gap_ok = ma_gap >= self.dip_threshold
        short_rsi_ok = rsi_value >= self.rsi_overbought
        short_cvd_threshold = abs(self.cvd_z_entry_short)
        short_cvd_ok = z_cvd <= -short_cvd_threshold
        short_trend_ok = fast <= slow * (1 + self.trend_tolerance)
        price = closes[-1]
        long_gates = {
            "dip_ok": bool(dip_ok),
            "rsi_ok": bool(rsi_ok),
            "cvd_ok": bool(cvd_ok),
            "trend_ok": bool(trend_ok),
        }

        short_gates = {
            "dip_ok": bool(short_gap_ok),
            "rsi_ok": bool(short_rsi_ok),
            "cvd_ok": bool(short_cvd_ok),
            "trend_ok": bool(short_trend_ok),
        }

        long_pass = all(long_gates.values())
        short_pass = all(short_gates.values())
        
        self.logger.debug(f"Conditions: long={long_pass} {long_gates}, short={short_pass} {short_gates}")
        self.logger.debug(f"Metrics: dip={ma_gap} vs {-self.dip_threshold}, rsi={rsi_value} vs {self.rsi_oversold}, cvd={z_cvd} vs {self.cvd_z_entry}, trend={fast} vs {slow*(1-self.trend_tolerance)}")

        ma_threshold = float(slow) * (1 - self.trend_tolerance)
        ma_threshold_short = float(slow) * (1 + self.trend_tolerance)
        timestamp = bar_end_dt
        if not is_backtest:
            self._telemetry_log(
                "Buy-the-dip long conditions evaluated",
                level="INFO",
                tone="neutral",
                phase=self._PHASE_SIGNALS,
                details={
                    "symbol": getattr(self, "symbol", "") or "",
                    "interval": self.interval,
                    "bar_end": bar_end,
                    "close": bar_close,
                    "ma_gap": f"{float(ma_gap):.3f} <= {-float(self.dip_threshold):.3f} -> {'PASS' if dip_ok else 'FAIL'}",
                    "rsi": f"{float(rsi_value):.3f} <= {float(self.rsi_oversold):.3f} -> {'PASS' if rsi_ok else 'FAIL'}",
                    "z_cvd": f"{float(z_cvd):.3f} >= {float(self.cvd_z_entry):.3f} -> {'PASS' if cvd_ok else 'FAIL'}",
                    "ma_fast_vs_slow": f"{float(fast):.3f} >= {ma_threshold:.3f} -> {'PASS' if trend_ok else 'FAIL'}",
                },
                deduplicate=False,
                timestamp=timestamp,
            )
            self._telemetry_log_processing_step(
                step="ma_gap",
                metric=float(ma_gap),
                threshold=-float(self.dip_threshold),
                comparison="<=",
                passed=bool(dip_ok),
                stage=self._PHASE_SIGNALS,
                details={"symbol": self.symbol},
                timestamp=timestamp,
            )
            self._telemetry_log_processing_step(
                step="rsi",
                metric=float(rsi_value),
                threshold=float(self.rsi_oversold),
                comparison="<=",
                passed=bool(rsi_ok),
                stage=self._PHASE_SIGNALS,
                details={"symbol": self.symbol},
                timestamp=timestamp,
            )
            self._telemetry_log_processing_step(
                step="z_cvd",
                metric=float(z_cvd),
                threshold=float(self.cvd_z_entry),
                comparison=">=",
                passed=bool(cvd_ok),
                stage=self._PHASE_SIGNALS,
                details={"symbol": self.symbol},
                timestamp=timestamp,
            )
            self._telemetry_log_processing_step(
                step="ma_fast_vs_slow",
                metric=float(fast),
                threshold=float(slow) * (1 - self.trend_tolerance),
                comparison=">=",
                passed=bool(trend_ok),
                stage=self._PHASE_SIGNALS,
                details={"symbol": self.symbol},
                timestamp=timestamp,
            )
            self._telemetry_log(
                "Buy-the-dip short conditions evaluated",
                level="INFO",
                tone="neutral",
                phase=self._PHASE_SIGNALS,
                details={
                    "symbol": getattr(self, "symbol", "") or "",
                    "interval": self.interval,
                    "bar_end": bar_end,
                    "close": bar_close,
                    "ma_gap_short": f"{float(ma_gap):.3f} >= {float(self.dip_threshold):.3f} -> {'PASS' if short_gap_ok else 'FAIL'}",
                    "rsi_short": f"{float(rsi_value):.3f} >= {float(self.rsi_overbought):.3f} -> {'PASS' if short_rsi_ok else 'FAIL'}",
                    "z_cvd_short": f"{float(z_cvd):.3f} <= {-float(short_cvd_threshold):.3f} -> {'PASS' if short_cvd_ok else 'FAIL'}",
                    "ma_fast_vs_slow_short": f"{float(fast):.3f} <= {ma_threshold_short:.3f} -> {'PASS' if short_trend_ok else 'FAIL'}",
                },
                deduplicate=False,
                timestamp=timestamp,
            )
            self._telemetry_log_processing_step(
                step="ma_gap_short",
                metric=float(ma_gap),
                threshold=float(self.dip_threshold),
                comparison=">=",
                passed=bool(short_gap_ok),
                stage=self._PHASE_SIGNALS,
                details={"symbol": self.symbol},
                timestamp=timestamp,
            )
            self._telemetry_log_processing_step(
                step="rsi_short",
                metric=float(rsi_value),
                threshold=float(self.rsi_overbought),
                comparison=">=",
                passed=bool(short_rsi_ok),
                stage=self._PHASE_SIGNALS,
                details={"symbol": self.symbol},
                timestamp=timestamp,
            )
            self._telemetry_log_processing_step(
                step="z_cvd_short",
                metric=float(z_cvd),
                threshold=-float(short_cvd_threshold),
                comparison="<=",
                passed=bool(short_cvd_ok),
                stage=self._PHASE_SIGNALS,
                details={"symbol": self.symbol},
                timestamp=timestamp,
            )
            self._telemetry_log_processing_step(
                step="ma_fast_vs_slow_short",
                metric=float(fast),
                threshold=float(slow) * (1 + self.trend_tolerance),
                comparison="<=",
                passed=bool(short_trend_ok),
                stage=self._PHASE_SIGNALS,
                details={"symbol": self.symbol},
                timestamp=timestamp,
            )
        if not (long_pass or short_pass):
            if not is_backtest:
                self._telemetry_set_phase_status(
                    self._PHASE_SIGNALS,
                    status="evaluated",
                    status_code="conditions_checked",
                )
            return
        if price <= 0:
            return
        metrics = {
            "close": price,
            "ma_fast": fast,
            "ma_slow": slow,
            "rsi": rsi_value,
            "z_cvd": z_cvd,
            "ma_gap": ma_gap,
        }
        if long_pass:
            self._trigger_entry(price, metrics, candle)
        elif short_pass:
            self._trigger_entry_short(price, metrics, candle)

    # ------------------------------------------------------------------
    def _trigger_entry(
        self, price: float, latest_row: Mapping[str, float], candle: Mapping[str, Any]
    ) -> None:
        ok, reason = self.can_open_new_trade("BUY")
        if not ok:
            # Only log if it's a risk block, not just existing position (to avoid log spam)
            if "Risk" in reason:
                 self.logger.warning(
                    f"Buy-the-dip entry blocked: {reason}",
                    extra={
                        "event": "strategy.signal.risk_blocked",
                        "strategy": self.name,
                        "symbol": self.symbol,
                        "side": "BUY",
                    },
                )
            return

        quantity = self._determine_quantity(price)
        if quantity <= 0:
            return
        exchange, sec_type = self._resolve_instrument(self.symbol)
        metadata: Dict[str, Any] = {
            "entry_price": float(price),
            "rsi": float(latest_row.get("rsi", 0.0) or 0.0),
            "z_cvd": float(latest_row.get("z_cvd", 0.0) or 0.0),
            "ma_fast": float(latest_row.get("ma_fast", 0.0) or 0.0),
            "ma_slow": float(latest_row.get("ma_slow", 0.0) or 0.0),
            "ma_gap": float(latest_row.get("ma_gap", 0.0) or 0.0),
            "interval_end_ns": candle.get("interval_end_ns"),
            "quantity": float(quantity),
        }
        exit_targets = self.evaluate_exit_signal(
            position=float(quantity),
            entry_price=float(price),
            account_equity=getattr(self, "account_equity", None),
            bar=candle,
            is_dom=False,
        )
        if exit_targets is not None:
            stop_price = exit_targets.stop_loss
            take_profit = exit_targets.take_profit
            metadata["exit_mode"] = exit_targets.mode.value
            if stop_price is not None:
                metadata["stop_loss"] = float(stop_price)
            if take_profit is not None:
                metadata["take_profit"] = float(take_profit)

        order_payload: Dict[str, Any] = {
            "side": "BUY",
            "quantity": float(quantity),
            "order_type": "MARKET",
            "symbol": self.symbol,
            "exchange": exchange,
            "sec_type": sec_type,
            "reason": "buy_the_dip_entry",
            "metadata": metadata,
        }

        if not self.queue_order(order_payload):
            block_details = None
            extractor = getattr(self, "pop_last_order_block", None)
            if callable(extractor):
                block_details = extractor()
            log_extra = {
                "event": "strategy.order.queue_failed",
                "strategy": self.name,
                "symbol": self.symbol,
                "side": "BUY",
                "quantity": quantity,
            }
            if isinstance(block_details, Mapping):
                code = block_details.get("code")
                message = block_details.get("message")
                details = block_details.get("details")
                if code:
                    log_extra["block_code"] = code
                if message:
                    log_extra["block_message"] = message
                if isinstance(details, Mapping):
                    log_extra["block_details"] = dict(details)
            block_code = (
                block_details.get("code") if isinstance(block_details, Mapping) else None
            )
            if block_code in {"cooldown_active", "frequency_guard"}:
                reason_text = "cooldown guard" if block_code == "cooldown_active" else "frequency guard"
                self.logger.info(
                    "Buy-the-dip order suppressed by %s",
                    reason_text,
                    extra=log_extra,
                )
            else:
                self.logger.warning(
                    "Failed to queue buy-the-dip order for runner",
                    extra=log_extra,
                )
            return

        recorder = getattr(self, "_telemetry_record_signal", None)
        if callable(recorder):
            try:
                recorder("BUY")
            except Exception:  # pragma: no cover - defensive telemetry
                self.logger.debug("Failed to record telemetry signal", exc_info=True)
        signal_details = {
            "side": "BUY",
            "quantity": float(quantity),
            "symbol": self.symbol,
            "entry_price": float(price),
        }
        if exit_targets is not None:
            if exit_targets.stop_loss is not None:
                signal_details["stop_loss"] = float(exit_targets.stop_loss)
            if exit_targets.take_profit is not None:
                signal_details["take_profit"] = float(exit_targets.take_profit)
        self._telemetry_set_phase_status(
            self._PHASE_SIGNALS,
            status="triggered",
            status_code="entry_queued",
            status_reason="Buy signal queued for execution",
            status_details=signal_details,
        )
        self._telemetry_log(
            "Buy-the-dip entry signal queued",
            level="INFO",
            tone="positive",
            phase=self._PHASE_SIGNALS,
            deduplicate=False,
            details=dict(signal_details),
        )
        self._last_order_payload = order_payload
        log_extra = {
            "event": "strategy.order.signal",
            "strategy": self.name,
            "symbol": self.symbol,
            "side": "BUY",
            "quantity": float(quantity),
            "entry_price": float(price),
        }
        if stop_price is not None:
            log_extra["stop_loss"] = float(stop_price)
        if take_profit is not None:
            log_extra["take_profit"] = float(take_profit)
        self.logger.info("Buy-the-dip order queued for execution", extra=log_extra)

    # ------------------------------------------------------------------
    def _trigger_entry_short(
        self, price: float, latest_row: Mapping[str, float], candle: Mapping[str, Any]
    ) -> None:
        ok, reason = self.can_open_new_trade("SELL")
        if not ok:
            if "Risk" in reason:
                 self.logger.warning(
                    f"Buy-the-dip entry blocked: {reason}",
                    extra={
                        "event": "strategy.signal.risk_blocked",
                        "strategy": self.name,
                        "symbol": self.symbol,
                        "side": "SELL",
                    },
                )
            return

        quantity = self._determine_quantity(price)
        if quantity <= 0:
            return
        exchange, sec_type = self._resolve_instrument(self.symbol)
        metadata: Dict[str, Any] = {
            "entry_price": float(price),
            "rsi": float(latest_row.get("rsi", 0.0) or 0.0),
            "z_cvd": float(latest_row.get("z_cvd", 0.0) or 0.0),
            "ma_fast": float(latest_row.get("ma_fast", 0.0) or 0.0),
            "ma_slow": float(latest_row.get("ma_slow", 0.0) or 0.0),
            "ma_gap": float(latest_row.get("ma_gap", 0.0) or 0.0),
            "interval_end_ns": candle.get("interval_end_ns"),
            "quantity": float(quantity),
        }
        exit_targets = self.evaluate_exit_signal(
            position=-float(quantity),
            entry_price=float(price),
            account_equity=getattr(self, "account_equity", None),
            bar=candle,
            is_dom=False,
        )
        stop_price = None
        take_profit = None
        if exit_targets is not None:
            stop_price = exit_targets.stop_loss
            take_profit = exit_targets.take_profit
            metadata["exit_mode"] = exit_targets.mode.value
            if stop_price is not None:
                metadata["stop_loss"] = float(stop_price)
            if take_profit is not None:
                metadata["take_profit"] = float(take_profit)

        order_payload: Dict[str, Any] = {
            "side": "SELL",
            "quantity": float(quantity),
            "order_type": "MARKET",
            "symbol": self.symbol,
            "exchange": exchange,
            "sec_type": sec_type,
            "reason": "buy_the_dip_entry_short",
            "metadata": metadata,
        }

        if not self.queue_order(order_payload):
            block_details = None
            extractor = getattr(self, "pop_last_order_block", None)
            if callable(extractor):
                block_details = extractor()
            log_extra = {
                "event": "strategy.order.queue_failed",
                "strategy": self.name,
                "symbol": self.symbol,
                "side": "SELL",
                "quantity": quantity,
            }
            if isinstance(block_details, Mapping):
                code = block_details.get("code")
                message = block_details.get("message")
                details = block_details.get("details")
                if code:
                    log_extra["block_code"] = code
                if message:
                    log_extra["block_message"] = message
                if isinstance(details, Mapping):
                    log_extra["block_details"] = dict(details)
            block_code = (
                block_details.get("code") if isinstance(block_details, Mapping) else None
            )
            if block_code in {"cooldown_active", "frequency_guard"}:
                reason_text = "cooldown guard" if block_code == "cooldown_active" else "frequency guard"
                self.logger.info(
                    "Buy-the-dip order suppressed by %s",
                    reason_text,
                    extra=log_extra,
                )
            else:
                self.logger.warning(
                    "Failed to queue buy-the-dip order for runner",
                    extra=log_extra,
                )
            return

        recorder = getattr(self, "_telemetry_record_signal", None)
        if callable(recorder):
            try:
                recorder("SELL")
            except Exception:  # pragma: no cover - defensive telemetry
                self.logger.debug("Failed to record telemetry signal", exc_info=True)
        signal_details = {
            "side": "SELL",
            "quantity": float(quantity),
            "symbol": self.symbol,
            "entry_price": float(price),
        }
        if exit_targets is not None:
            if exit_targets.stop_loss is not None:
                signal_details["stop_loss"] = float(exit_targets.stop_loss)
            if exit_targets.take_profit is not None:
                signal_details["take_profit"] = float(exit_targets.take_profit)
        self._telemetry_set_phase_status(
            self._PHASE_SIGNALS,
            status="triggered",
            status_code="entry_queued",
            status_reason="Sell signal queued for execution",
            status_details=signal_details,
        )
        self._telemetry_log(
            "Buy-the-dip short entry signal queued",
            level="INFO",
            tone="positive",
            phase=self._PHASE_SIGNALS,
            deduplicate=False,
            details=dict(signal_details),
        )
        self._last_order_payload = order_payload
        log_extra = {
            "event": "strategy.order.signal",
            "strategy": self.name,
            "symbol": self.symbol,
            "side": "SELL",
            "quantity": float(quantity),
            "entry_price": float(price),
        }
        if stop_price is not None:
            log_extra["stop_loss"] = float(stop_price)
        if take_profit is not None:
            log_extra["take_profit"] = float(take_profit)
        self.logger.info("Buy-the-dip order queued for execution", extra=log_extra)

    # ------------------------------------------------------------------
    def _determine_quantity(self, price: float) -> int:  # noqa: D401 - simple helper
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

    # ------------------------------------------------------------------
    def _moving_average(self, values: List[float], period: int) -> List[Optional[float]]:
        if period <= 0:
            return [None for _ in values]
        window_sum = 0.0
        result: List[Optional[float]] = []
        for index, value in enumerate(values):
            window_sum += value
            if index >= period:
                window_sum -= values[index - period]
            if index + 1 >= period:
                result.append(window_sum / period)
            else:
                result.append(None)
        return result

    # ------------------------------------------------------------------
    def _compute_rsi(self, closes: List[float], period: int) -> List[Optional[float]]:
        length = len(closes)
        if length == 0:
            return []
        if period <= 0:
            return [50.0 for _ in closes]
        if length <= period:
            return [50.0 for _ in closes]
        gains = [0.0] * (length - 1)
        losses = [0.0] * (length - 1)
        for index in range(1, length):
            delta = closes[index] - closes[index - 1]
            gains[index - 1] = max(delta, 0.0)
            losses[index - 1] = max(-delta, 0.0)
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        rsis: List[Optional[float]] = [None] * length

        def compute(value_gain: float, value_loss: float) -> float:
            if value_loss == 0:
                return 100.0 if value_gain > 0 else 50.0
            rs = value_gain / value_loss
            return 100 - (100 / (1 + rs))

        first_rsi = compute(avg_gain, avg_loss)
        rsis[period] = first_rsi
        for index in range(period + 1, length):
            gain = gains[index - 1]
            loss = losses[index - 1]
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
            rsis[index] = compute(avg_gain, avg_loss)
        for index in range(period):
            rsis[index] = first_rsi
        return [
            max(0.0, min(100.0, value if value is not None else 50.0))
            for value in rsis
        ]

    # ------------------------------------------------------------------
    def _compute_cvd(
        self, opens: Iterable[float], closes: Iterable[float], volumes: Iterable[float]
    ) -> List[float]:
        cumulative = 0.0
        series: List[float] = []
        for open_, close, volume in zip(opens, closes, volumes):
            direction = 0.0
            if close > open_:
                direction = 1.0
            elif close < open_:
                direction = -1.0
            cumulative += direction * volume
            series.append(cumulative)
        return series

    # ------------------------------------------------------------------
    def _rolling_zscore(self, series: List[float], window: int) -> float:
        if window <= 1 or not series:
            return 0.0
        lookback = series[-window:]
        mean = sum(lookback) / len(lookback)
        variance = sum((value - mean) ** 2 for value in lookback) / len(lookback)
        std_dev = math.sqrt(variance)
        if std_dev == 0:
            return 0.0
        return (series[-1] - mean) / std_dev
