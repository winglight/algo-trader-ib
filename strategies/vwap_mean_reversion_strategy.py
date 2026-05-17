from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timezone
from statistics import mean
from typing import Any, Deque, Dict, Mapping

from src.strategies.candle import CandleSubscriptionStrategy
from src.strategies.templates import StrategySignal, StrategyTemplate


class VwapMeanReversionStrategy(CandleSubscriptionStrategy, StrategyTemplate):
    strategy_type = "vwap_mean_reversion"
    _PHASE_EXECUTION = "execution"
    interval: str = "4h"
    parameter_definitions = {
        "symbol": {
            "type": "str",
            "allow_null": True,
            "default": "SPY",
            "description": "Symbol to subscribe for candle updates.",
        },
        "interval": {
            "type": "str",
            "default": "4h",
            "description": "Candle interval (e.g., 1m, 1h, 4h).",
        },
        "deviation_lookback": {
            "type": "int",
            "default": 20,
            "min": 3,
            "description": "Rolling window for VWAP deviation std.",
        },
        "stddev_multiplier": {
            "type": "float",
            "default": 2.0,
            "min": 0.1,
            "description": "Standard deviation multiplier (n).",
        },
        "enable_long": {
            "type": "bool",
            "default": True,
            "description": "Allow long entries.",
        },
        "enable_short": {
            "type": "bool",
            "default": True,
            "description": "Allow short entries.",
        },
        "min_hold_bars": {
            "type": "int",
            "default": 90,
            "min": 0,
            "description": "Minimum bars to hold before exit.",
        },
        "order_quantity": {
            "type": "int",
            "default": 1,
            "min": 1,
            "step": 1,
            "description": "Order quantity when entering/exiting positions.",
        },
    }
    default_parameters = {
        "symbol": "SPY",
        "interval": "4h",
        "deviation_lookback": 20,
        "stddev_multiplier": 2.0,
        "enable_long": True,
        "enable_short": True,
        "min_hold_bars": 90,
        "order_quantity": 1,
    }

    def __post_init__(self) -> None:
        if not hasattr(self, "interval") or not self.interval:
            self.interval = "4h"
        super().__post_init__()
        self.intervals = [self.interval]
        lookback = int(getattr(self, "deviation_lookback", 20))
        self._deviation_history: Deque[float] = deque(maxlen=lookback)
        self._position: int = 0
        self._entry_price: float | None = None
        self._bars_since_entry: int = 0
        self._last_closed_candle_id: str | None = None
        self._vwap_price_volume: float = 0.0
        self._vwap_volume: float = 0.0
        self._vwap_session_date: datetime | None = None

    async def on_start(self) -> None:
        await super().on_start()
        await self._await_market_data_ready_and_subscribe()

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        if isinstance(value, datetime):
            ts = value
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                ts = datetime.now(timezone.utc)
            else:
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
            except Exception:
                ts = datetime.now(timezone.utc)
            else:
                if abs(seconds) > 1_000_000_000_000:
                    seconds /= 1000.0
                ts = datetime.fromtimestamp(seconds, tz=timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)

    def _reset_vwap_session(self, session_date: datetime) -> None:
        self._vwap_session_date = session_date
        self._vwap_price_volume = 0.0
        self._vwap_volume = 0.0
        self._deviation_history.clear()

    async def _process_candle_event(self, candle: Mapping[str, Any]) -> None:
        if candle.get("type") not in {None, "candle"}:
            return
        if not bool(candle.get("is_closed", False)):
            return
        expected_interval = getattr(self, "interval", "")
        interval = candle.get("interval")
        if interval and expected_interval and interval != expected_interval:
            return
        if getattr(self, "_history_replay_in_progress", False):
            return

        price = candle.get("close")
        if price is None:
            price = candle.get("price")
        if price is None:
            return

        ts_value = candle.get("end") or candle.get("timestamp") or candle.get("time")
        ts = self._parse_timestamp(ts_value)
        candle_id = ts.astimezone(timezone.utc).isoformat()
        if self._last_closed_candle_id == candle_id:
            return
        self._last_closed_candle_id = candle_id

        session_date = datetime(ts.year, ts.month, ts.day, tzinfo=timezone.utc)
        if self._vwap_session_date is None or self._vwap_session_date != session_date:
            self._reset_vwap_session(session_date)

        price_value = float(price)
        try:
            volume = float(candle.get("volume", 0.0) or 0.0)
        except (TypeError, ValueError):
            volume = 0.0
        if volume <= 0.0:
            volume = 1.0

        self._vwap_price_volume += price_value * volume
        self._vwap_volume += volume
        if self._vwap_volume <= 0.0:
            return
        vwap = self._vwap_price_volume / self._vwap_volume

        lookback = int(getattr(self, "deviation_lookback", 20))
        if self._deviation_history.maxlen != lookback:
            self._deviation_history = deque(self._deviation_history, maxlen=lookback)
        deviation = price_value - vwap
        self._deviation_history.append(deviation)
        if len(self._deviation_history) < self._deviation_history.maxlen:
            return

        avg_dev = mean(self._deviation_history)
        variance = sum((item - avg_dev) ** 2 for item in self._deviation_history)
        variance /= max(len(self._deviation_history) - 1, 1)
        std_dev = math.sqrt(variance) if variance > 0 else 0.0
        if std_dev <= 0.0:
            return

        std_multiplier = float(getattr(self, "stddev_multiplier", 2.0))
        upper = vwap + std_multiplier * std_dev
        lower = vwap - std_multiplier * std_dev
        min_hold_bars = int(getattr(self, "min_hold_bars", 0))
        enable_long = bool(getattr(self, "enable_long", True))
        enable_short = bool(getattr(self, "enable_short", True))

        raw_quantity = getattr(self, "order_quantity", 0)
        quantity, fractional_quantity = self._resolve_order_quantity(raw_quantity)
        quantity = int(quantity)
        if quantity <= 0:
            return

        if self._position != 0:
            self._bars_since_entry += 1
        else:
            self._bars_since_entry = 0

        def build_metadata() -> Dict[str, Any]:
            metadata = {
                "vwap": vwap,
                "std_dev": std_dev,
                "upper_band": upper,
                "lower_band": lower,
                "stddev_multiplier": std_multiplier,
                "price": price_value,
                "entry_price": price_value,
                "bars_since_entry": self._bars_since_entry,
                "symbol": getattr(self, "symbol", "") or "",
            }
            if fractional_quantity > 0.0:
                metadata["quantity_fractional_discarded"] = fractional_quantity
            return metadata

        def enqueue_signal(
            signal: StrategySignal,
            *,
            new_position: int,
            is_exit_like: bool,
        ) -> None:
            if not self.produce_signal(signal, is_exit_like=is_exit_like):
                return
            self._position = new_position
            if new_position == 0:
                self._entry_price = None
                self._bars_since_entry = 0
            else:
                self._entry_price = price_value
                self._bars_since_entry = 0

        if self._position == 0:
            if enable_short and price_value >= upper:
                enqueue_signal(
                    StrategySignal(
                        side="SELL",
                        quantity=quantity,
                        reason="vwap-mean-reversion-entry",
                        metadata=build_metadata(),
                    ),
                    new_position=-1,
                    is_exit_like=False,
                )
                return
            if enable_long and price_value <= lower:
                enqueue_signal(
                    StrategySignal(
                        side="BUY",
                        quantity=quantity,
                        reason="vwap-mean-reversion-entry",
                        metadata=build_metadata(),
                    ),
                    new_position=1,
                    is_exit_like=False,
                )
                return

        if self._position > 0 and self._bars_since_entry >= min_hold_bars:
            if price_value >= vwap:
                enqueue_signal(
                    StrategySignal(
                        side="SELL",
                        quantity=quantity,
                        reason="vwap-mean-reversion-exit",
                        metadata={**build_metadata(), "close_position": True},
                    ),
                    new_position=0,
                    is_exit_like=True,
                )
                return

        if self._position < 0 and self._bars_since_entry >= min_hold_bars:
            if price_value <= vwap:
                enqueue_signal(
                    StrategySignal(
                        side="BUY",
                        quantity=quantity,
                        reason="vwap-mean-reversion-exit",
                        metadata={**build_metadata(), "close_position": True},
                    ),
                    new_position=0,
                    is_exit_like=True,
                )

    async def on_candle(self, candle: Mapping[str, Any]) -> None:
        await self._process_candle_event(candle)

    async def on_market_event(self, event: Mapping[str, Any]) -> None:
        await super().on_market_event(event)
