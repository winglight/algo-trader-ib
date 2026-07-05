"""Liquidity structure evaluator for strategy-side signal filtering."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from typing import Any, Dict, List, Mapping, Sequence

try:  # pragma: no cover - Python 3.8 fallback
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - fallback for systems without zoneinfo
    from backports.zoneinfo import ZoneInfo  # type: ignore[assignment]


BIAS_LONG = "LONG"
BIAS_SHORT = "SHORT"
BIAS_NONE = "NONE"


def _safe_float(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _safe_timestamp(value: Any) -> datetime | None:
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
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


@dataclass
class LiquidityFilterConfig:
    """Configuration set for liquidity structure evaluation."""

    interval: str = "1m"
    lookback_bars: int = 120
    atr_period: int = 14
    swing_window: int = 2
    eq_tolerance_ticks: float = 3.0
    min_penetration_ticks: float = 3.0
    max_reclaim_bars: int = 2
    displacement_atr_multiplier: float = 1.0
    structure_lookback: int = 8
    tick_size: float = 0.25
    invalidate_buffer_ticks: float = 1.0

    def normalized(self) -> "LiquidityFilterConfig":
        return LiquidityFilterConfig(
            interval=(self.interval or "1m").strip() or "1m",
            lookback_bars=max(40, int(self.lookback_bars)),
            atr_period=max(2, int(self.atr_period)),
            swing_window=max(1, int(self.swing_window)),
            eq_tolerance_ticks=max(0.5, float(self.eq_tolerance_ticks)),
            min_penetration_ticks=max(0.5, float(self.min_penetration_ticks)),
            max_reclaim_bars=max(1, int(self.max_reclaim_bars)),
            displacement_atr_multiplier=max(0.1, float(self.displacement_atr_multiplier)),
            structure_lookback=max(2, int(self.structure_lookback)),
            tick_size=max(1e-6, float(self.tick_size)),
            invalidate_buffer_ticks=max(0.0, float(self.invalidate_buffer_ticks)),
        )


@dataclass
class LiquidityFilterMetrics:
    """Primary liquidity outputs plus detailed diagnostics."""

    trade_bias: str = BIAS_NONE
    entry_zone: Dict[str, float] = field(default_factory=lambda: {"low": 0.0, "high": 0.0})
    invalidate_level: float = 0.0
    confidence: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "trade_bias": self.trade_bias,
            "entry_zone": {
                "low": float(self.entry_zone.get("low", 0.0) or 0.0),
                "high": float(self.entry_zone.get("high", 0.0) or 0.0),
            },
            "invalidate_level": float(self.invalidate_level or 0.0),
            "confidence": float(self.confidence or 0.0),
        }


class LiquidityStrategyTool:
    """Evaluate EQH/EQL sweep and displacement structure on candle data."""

    _RTH_START = dtime(hour=10, minute=0)
    _RTH_END = dtime(hour=11, minute=30)
    _TRAP_MIN_BODY_ATR = 0.7
    _TRAP_MIN_RECLAIM_DISTANCE = 0.7

    def __init__(self, config: LiquidityFilterConfig | None = None) -> None:
        self._config = (config or LiquidityFilterConfig()).normalized()

    @property
    def config(self) -> LiquidityFilterConfig:
        return self._config

    def update_config(self, config: LiquidityFilterConfig) -> None:
        self._config = config.normalized()

    def evaluate(
        self, candles: Sequence[Mapping[str, Any]] | None
    ) -> LiquidityFilterMetrics:
        bars = self._normalize_candles(candles or [])
        min_required = max(
            self._config.lookback_bars,
            self._config.atr_period + self._config.structure_lookback + 6,
        )
        if len(bars) < min_required:
            return LiquidityFilterMetrics(
                trade_bias=BIAS_NONE,
                details={
                    "status": "insufficient_bars",
                    "have": len(bars),
                    "need": min_required,
                    "interval": self._config.interval,
                },
            )

        window = bars[-self._config.lookback_bars :]
        atr = self._calculate_atr(window, self._config.atr_period)
        if atr is None or atr <= 0.0:
            return LiquidityFilterMetrics(
                trade_bias=BIAS_NONE,
                details={
                    "status": "atr_unavailable",
                    "have": len(window),
                    "interval": self._config.interval,
                },
            )

        tolerance = self._config.tick_size * self._config.eq_tolerance_ticks
        min_penetration = self._config.tick_size * self._config.min_penetration_ticks

        swing_highs, swing_lows = self._extract_swings(window, self._config.swing_window)
        eqh_price, eqh_touches = self._find_liquidity_pool(swing_highs, tolerance)
        eql_price, eql_touches = self._find_liquidity_pool(swing_lows, tolerance)
        long_trap = self._find_false_breakout_trap(
            bars=window,
            direction=BIAS_LONG,
            eq_price=eqh_price,
            atr=atr,
            min_penetration=min_penetration,
        )
        short_trap = self._find_false_breakout_trap(
            bars=window,
            direction=BIAS_SHORT,
            eq_price=eql_price,
            atr=atr,
            min_penetration=min_penetration,
        )

        short_candidate = self._build_candidate(
            bars=window,
            direction=BIAS_SHORT,
            eq_price=eqh_price,
            eq_touches=eqh_touches,
            atr=atr,
            tolerance=tolerance,
            min_penetration=min_penetration,
        )
        long_candidate = self._build_candidate(
            bars=window,
            direction=BIAS_LONG,
            eq_price=eql_price,
            eq_touches=eql_touches,
            atr=atr,
            tolerance=tolerance,
            min_penetration=min_penetration,
        )

        chosen = self._choose_candidate(long_candidate, short_candidate)
        if chosen is None:
            return LiquidityFilterMetrics(
                trade_bias=BIAS_NONE,
                details={
                    "status": "structure_incomplete",
                    "atr": atr,
                    "eqh": eqh_price,
                    "eqh_touches": eqh_touches,
                    "eql": eql_price,
                    "eql_touches": eql_touches,
                    "interval": self._config.interval,
                    "long_false_breakout_trap": long_trap,
                    "short_false_breakout_trap": short_trap,
                },
            )
        chosen_details = dict(chosen["details"] or {})
        chosen_details["long_false_breakout_trap"] = long_trap
        chosen_details["short_false_breakout_trap"] = short_trap
        return LiquidityFilterMetrics(
            trade_bias=chosen["trade_bias"],
            entry_zone=chosen["entry_zone"],
            invalidate_level=chosen["invalidate_level"],
            confidence=chosen["confidence"],
            details=chosen_details,
        )

    def _normalize_candles(
        self, candles: Sequence[Mapping[str, Any]]
    ) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for candle in candles:
            open_price = _safe_float(candle.get("open"))
            high_price = _safe_float(candle.get("high"))
            low_price = _safe_float(candle.get("low"))
            close_price = _safe_float(candle.get("close"))
            if (
                open_price is None
                or high_price is None
                or low_price is None
                or close_price is None
            ):
                continue
            ts = (
                _safe_timestamp(candle.get("end"))
                or _safe_timestamp(candle.get("timestamp"))
                or _safe_timestamp(candle.get("time"))
                or _safe_timestamp(candle.get("start"))
            )
            normalized.append(
                {
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "timestamp": ts,
                }
            )
        return normalized

    def _calculate_atr(
        self, bars: Sequence[Mapping[str, Any]], period: int
    ) -> float | None:
        if len(bars) < 2:
            return None
        true_ranges: List[float] = []
        for index in range(1, len(bars)):
            high = float(bars[index]["high"])
            low = float(bars[index]["low"])
            prev_close = float(bars[index - 1]["close"])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)
        if not true_ranges:
            return None
        window = true_ranges[-max(1, period) :]
        if not window:
            return None
        return sum(window) / len(window)

    def _extract_swings(
        self, bars: Sequence[Mapping[str, Any]], swing_window: int
    ) -> tuple[List[float], List[float]]:
        highs: List[float] = []
        lows: List[float] = []
        span = max(1, swing_window)
        total = len(bars)
        for index in range(span, total - span):
            current_high = float(bars[index]["high"])
            current_low = float(bars[index]["low"])
            left = bars[index - span : index]
            right = bars[index + 1 : index + span + 1]
            if not left or not right:
                continue
            is_swing_high = all(
                current_high >= float(item["high"]) for item in (*left, *right)
            )
            is_swing_low = all(
                current_low <= float(item["low"]) for item in (*left, *right)
            )
            if is_swing_high:
                highs.append(current_high)
            if is_swing_low:
                lows.append(current_low)
        return highs, lows

    def _find_liquidity_pool(
        self, levels: Sequence[float], tolerance: float
    ) -> tuple[float | None, int]:
        if len(levels) < 2:
            return None, 0
        best_price: float | None = None
        best_count = 0
        for price in levels:
            cluster = [item for item in levels if abs(item - price) <= tolerance]
            count = len(cluster)
            if count < 2:
                continue
            if count > best_count:
                best_count = count
                best_price = sum(cluster) / float(count)
        return best_price, best_count

    def _build_candidate(
        self,
        *,
        bars: Sequence[Mapping[str, Any]],
        direction: str,
        eq_price: float | None,
        eq_touches: int,
        atr: float,
        tolerance: float,
        min_penetration: float,
    ) -> Dict[str, Any] | None:
        if eq_price is None or eq_touches < 2:
            return None
        sweep = self._find_sweep(
            bars=bars,
            direction=direction,
            eq_price=eq_price,
            min_penetration=min_penetration,
        )
        if sweep is None:
            return None
        displacement = self._find_displacement(
            bars=bars,
            direction=direction,
            start_index=int(sweep["reclaim_index"]),
            atr=atr,
        )
        if displacement is None:
            return None
        low = float(displacement["low"])
        high = float(displacement["high"])
        midpoint = (low + high) * 0.5
        if direction == BIAS_LONG:
            entry_low, entry_high = min(low, midpoint), max(low, midpoint)
            invalidate = float(sweep["sweep_extreme"]) - (
                self._config.tick_size * self._config.invalidate_buffer_ticks
            )
        else:
            entry_low, entry_high = min(midpoint, high), max(midpoint, high)
            invalidate = float(sweep["sweep_extreme"]) + (
                self._config.tick_size * self._config.invalidate_buffer_ticks
            )
        confidence = self._score_confidence(
            direction=direction,
            eq_touches=eq_touches,
            sweep=sweep,
            displacement=displacement,
            atr=atr,
            tolerance=tolerance,
        )
        details = {
            "interval": self._config.interval,
            "eq_price": float(eq_price),
            "eq_touches": int(eq_touches),
            "min_penetration": float(min_penetration),
            "sweep_index": int(sweep["sweep_index"]),
            "reclaim_index": int(sweep["reclaim_index"]),
            "reclaim_bars": int(sweep["reclaim_bars"]),
            "penetration": float(sweep["penetration"]),
            "sweep_extreme": float(sweep["sweep_extreme"]),
            "displacement_index": int(displacement["index"]),
            "displacement_body": float(displacement["body"]),
            "displacement_body_atr": (
                float(displacement["body"]) / atr if atr > 0 else 0.0
            ),
            "displacement_structure_break": bool(displacement["structure_break"]),
            "rth_opening_window": bool(displacement["is_rth_window"]),
            "atr": float(atr),
        }
        return {
            "trade_bias": direction,
            "entry_zone": {"low": entry_low, "high": entry_high},
            "invalidate_level": invalidate,
            "confidence": confidence,
            "details": details,
        }

    def _find_sweep(
        self,
        *,
        bars: Sequence[Mapping[str, Any]],
        direction: str,
        eq_price: float,
        min_penetration: float,
    ) -> Dict[str, Any] | None:
        best: Dict[str, Any] | None = None
        upper_bound = len(bars) - 1
        for index in range(0, upper_bound + 1):
            bar = bars[index]
            if direction == BIAS_LONG:
                penetration = eq_price - float(bar["low"])
                swept = penetration >= min_penetration
            else:
                penetration = float(bar["high"]) - eq_price
                swept = penetration >= min_penetration
            if not swept:
                continue
            reclaim_end = min(upper_bound, index + self._config.max_reclaim_bars)
            reclaim_index = None
            for candidate in range(index, reclaim_end + 1):
                close = float(bars[candidate]["close"])
                if direction == BIAS_LONG and close > eq_price:
                    reclaim_index = candidate
                    break
                if direction == BIAS_SHORT and close < eq_price:
                    reclaim_index = candidate
                    break
            if reclaim_index is None:
                continue
            segment = bars[index : reclaim_index + 1]
            if direction == BIAS_LONG:
                sweep_extreme = min(float(item["low"]) for item in segment)
            else:
                sweep_extreme = max(float(item["high"]) for item in segment)
            candidate = {
                "sweep_index": index,
                "reclaim_index": reclaim_index,
                "reclaim_bars": (reclaim_index - index + 1),
                "penetration": penetration,
                "sweep_extreme": sweep_extreme,
            }
            if best is None or reclaim_index >= int(best["reclaim_index"]):
                best = candidate
        return best

    def _find_displacement(
        self,
        *,
        bars: Sequence[Mapping[str, Any]],
        direction: str,
        start_index: int,
        atr: float,
    ) -> Dict[str, Any] | None:
        if start_index >= len(bars):
            return None
        for index in range(start_index, len(bars)):
            bar = bars[index]
            open_price = float(bar["open"])
            close_price = float(bar["close"])
            high_price = float(bar["high"])
            low_price = float(bar["low"])
            body = abs(close_price - open_price)
            body_strong = body >= (atr * self._config.displacement_atr_multiplier)
            start = max(0, index - self._config.structure_lookback)
            left = bars[start:index]
            if direction == BIAS_LONG:
                directional = close_price > open_price
                structure_break = bool(left) and close_price > max(
                    float(item["high"]) for item in left
                )
            else:
                directional = close_price < open_price
                structure_break = bool(left) and close_price < min(
                    float(item["low"]) for item in left
                )
            if directional and (body_strong or structure_break):
                return {
                    "index": index,
                    "body": body,
                    "high": high_price,
                    "low": low_price,
                    "structure_break": structure_break,
                    "is_rth_window": self._is_rth_opening_window(
                        bar.get("timestamp")
                    ),
                }
        return None

    def _empty_trap_payload(self, *, direction: str, eq_price: float | None) -> Dict[str, Any]:
        return {
            "direction": direction,
            "detected": False,
            "active": False,
            "eq_price": float(eq_price) if eq_price is not None else None,
            "break_index": None,
            "reclaim_index": None,
            "reclaim_bars": None,
            "age_bars": None,
            "body_atr": None,
            "reclaim_distance": None,
        }

    def _find_false_breakout_trap(
        self,
        *,
        bars: Sequence[Mapping[str, Any]],
        direction: str,
        eq_price: float | None,
        atr: float,
        min_penetration: float,
    ) -> Dict[str, Any]:
        empty = self._empty_trap_payload(direction=direction, eq_price=eq_price)
        if eq_price is None:
            return empty
        if len(bars) < 2:
            return empty
        last_index = len(bars) - 1
        max_reclaim = min(2, max(1, self._config.max_reclaim_bars))
        penetration_floor = max(min_penetration, self._config.tick_size, 1e-9)

        for break_index in range(last_index - 1, -1, -1):
            break_bar = bars[break_index]
            if direction == BIAS_LONG:
                broke = float(break_bar["high"]) >= (eq_price + min_penetration)
            else:
                broke = float(break_bar["low"]) <= (eq_price - min_penetration)
            if not broke:
                continue

            reclaim_deadline = min(last_index, break_index + max_reclaim)
            for reclaim_index in range(break_index + 1, reclaim_deadline + 1):
                reclaim_bar = bars[reclaim_index]
                open_price = float(reclaim_bar["open"])
                close_price = float(reclaim_bar["close"])
                if direction == BIAS_LONG:
                    reclaimed = close_price < eq_price and close_price < open_price
                    reclaim_distance = (eq_price - close_price) / penetration_floor
                else:
                    reclaimed = close_price > eq_price and close_price > open_price
                    reclaim_distance = (close_price - eq_price) / penetration_floor
                if not reclaimed:
                    continue

                body = abs(close_price - open_price)
                body_atr = body / max(atr, 1e-9)
                obvious = (
                    body_atr >= self._TRAP_MIN_BODY_ATR
                    and reclaim_distance >= self._TRAP_MIN_RECLAIM_DISTANCE
                )
                if not obvious:
                    continue
                age_bars = max(0, last_index - reclaim_index)
                return {
                    "direction": direction,
                    "detected": True,
                    "active": age_bars <= max_reclaim,
                    "eq_price": float(eq_price),
                    "break_index": break_index,
                    "reclaim_index": reclaim_index,
                    "reclaim_bars": (reclaim_index - break_index),
                    "age_bars": age_bars,
                    "body_atr": body_atr,
                    "reclaim_distance": reclaim_distance,
                }
        return empty

    def _score_confidence(
        self,
        *,
        direction: str,
        eq_touches: int,
        sweep: Mapping[str, Any],
        displacement: Mapping[str, Any],
        atr: float,
        tolerance: float,
    ) -> float:
        del direction  # kept for future directional weighting
        touch_score = min(1.0, max(0.0, (eq_touches - 1) / 3.0))
        base_penetration = max(
            self._config.tick_size * self._config.min_penetration_ticks,
            tolerance,
            1e-9,
        )
        penetration_score = min(
            1.0, float(sweep.get("penetration", 0.0) or 0.0) / (base_penetration * 2.0)
        )
        reclaim_bars = int(sweep.get("reclaim_bars", self._config.max_reclaim_bars + 1) or 0)
        reclaim_score = 1.0 - min(
            1.0, max(0.0, reclaim_bars - 1) / float(max(1, self._config.max_reclaim_bars))
        )
        displacement_body = float(displacement.get("body", 0.0) or 0.0)
        displacement_score = min(
            1.0,
            displacement_body
            / max(atr * self._config.displacement_atr_multiplier, 1e-9),
        )
        rth_score = 1.0 if displacement.get("is_rth_window") else 0.6
        confidence = (
            0.25 * touch_score
            + 0.25 * penetration_score
            + 0.2 * reclaim_score
            + 0.2 * displacement_score
            + 0.1 * rth_score
        )
        return max(0.0, min(1.0, confidence))

    def _is_rth_opening_window(self, ts: datetime | None) -> bool:
        if ts is None:
            return False
        try:
            local = ts.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            local = ts.astimezone(timezone.utc)
        if local.weekday() >= 5:
            return False
        local_time = local.timetz().replace(tzinfo=None)
        return self._RTH_START <= local_time <= self._RTH_END

    def _choose_candidate(
        self,
        long_candidate: Dict[str, Any] | None,
        short_candidate: Dict[str, Any] | None,
    ) -> Dict[str, Any] | None:
        if long_candidate is None and short_candidate is None:
            return None
        if long_candidate is None:
            return short_candidate
        if short_candidate is None:
            return long_candidate
        if float(long_candidate.get("confidence", 0.0)) >= float(
            short_candidate.get("confidence", 0.0)
        ):
            return long_candidate
        return short_candidate
