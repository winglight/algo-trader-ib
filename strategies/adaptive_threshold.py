"""Adaptive volatility scaling utilities for DOM strategies."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import floor, isfinite
from statistics import StatisticsError, pstdev, quantiles
from typing import Any, Deque, Dict, Mapping, Protocol, Tuple


class ThresholdModelClient(Protocol):
    """Protocol for model clients that suggest DOM thresholds."""

    def predict_thresholds(self, features: Mapping[str, float]) -> Mapping[str, float]:
        """Return model-proposed thresholds for the provided *features*."""
        ...


def _normalise_bounds(bounds: Tuple[float, float]) -> Tuple[float, float]:
    low, high = bounds
    low = float(low)
    high = float(high)
    if high < low:
        low, high = high, low
    low = max(0.0, low)
    high = max(low if high < low else low, high)
    if low == high:
        high = low or 1.0
    return low, high


@dataclass(slots=True)
class AdaptiveThresholdState:
    """Track short-term price volatility and scale thresholds accordingly."""

    window_seconds: float = 30.0
    scale_bounds: Tuple[float, float] = (0.75, 1.5)
    smoothing: float = 0.2
    regime_volatility_breakpoints: Tuple[float, float] = (0.9, 1.2)
    regime_trend_breakpoints: Tuple[float, float] = (0.1, 0.25)
    threshold_smoothing: float = 0.5
    threshold_hysteresis_ticks: float = 0.0
    _differences: Deque[tuple[float, float]] = field(default_factory=deque, init=False, repr=False)
    _last_mid_price: float | None = field(default=None, init=False, repr=False)
    _volatility: float = field(default=0.0, init=False, repr=False)
    _baseline_volatility: float | None = field(default=None, init=False, repr=False)
    _volatility_scale: float = field(default=1.0, init=False, repr=False)
    _metrics_window: Deque[Mapping[str, float]] | None = field(
        default=None, init=False, repr=False
    )
    _previous_thresholds: Dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _last_smoothing: Dict[str, Dict[str, float | bool]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self.window_seconds = max(1.0, float(self.window_seconds))
        self.scale_bounds = _normalise_bounds(tuple(self.scale_bounds))
        self.smoothing = min(1.0, max(0.0, float(self.smoothing))) or 0.2
        self.regime_volatility_breakpoints = _normalise_bounds(
            tuple(self.regime_volatility_breakpoints)
        )
        low, high = tuple(self.regime_trend_breakpoints)
        self.regime_trend_breakpoints = _normalise_bounds((abs(low), abs(high)))
        self.threshold_smoothing = min(1.0, max(0.0, float(self.threshold_smoothing)))
        self.threshold_hysteresis_ticks = max(0.0, float(self.threshold_hysteresis_ticks))

    # ------------------------------------------------------------------
    @property
    def volatility(self) -> float:
        """Return the latest realised standard deviation of mid-price changes."""

        return self._volatility

    # ------------------------------------------------------------------
    @property
    def volatility_scale(self) -> float:
        """Return the latest normalised volatility multiplier."""

        return self._volatility_scale

    # ------------------------------------------------------------------
    def configure(
        self,
        *,
        window_seconds: float | None = None,
        scale_bounds: Tuple[float, float] | None = None,
        regime_volatility_breakpoints: Tuple[float, float] | None = None,
        regime_trend_breakpoints: Tuple[float, float] | None = None,
        threshold_smoothing: float | None = None,
        threshold_hysteresis_ticks: float | None = None,
    ) -> None:
        """Update configuration parameters and trim the observation window."""

        if window_seconds is not None:
            self.window_seconds = max(1.0, float(window_seconds))
        if scale_bounds is not None:
            self.scale_bounds = _normalise_bounds(tuple(scale_bounds))
        if regime_volatility_breakpoints is not None:
            self.regime_volatility_breakpoints = _normalise_bounds(
                tuple(regime_volatility_breakpoints)
            )
        if regime_trend_breakpoints is not None:
            low, high = tuple(regime_trend_breakpoints)
            self.regime_trend_breakpoints = _normalise_bounds((abs(low), abs(high)))
        if threshold_smoothing is not None:
            self.threshold_smoothing = min(1.0, max(0.0, float(threshold_smoothing)))
        if threshold_hysteresis_ticks is not None:
            self.threshold_hysteresis_ticks = max(0.0, float(threshold_hysteresis_ticks))
        self._trim()

    # ------------------------------------------------------------------
    def set_metrics_window(self, metrics_window: Deque[Mapping[str, float]] | None) -> None:
        """Bind the rolling metrics window used for quantile adjustments."""

        self._metrics_window = metrics_window

    # ------------------------------------------------------------------
    @property
    def smoothing_results(self) -> Mapping[str, Mapping[str, float | bool]]:
        """Return the latest smoothing outcomes for adaptive thresholds."""

        return dict(self._last_smoothing)

    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Reset the rolling state used for adaptive calculations."""

        self._differences.clear()
        self._last_mid_price = None
        self._volatility = 0.0
        self._baseline_volatility = None
        self._volatility_scale = 1.0
        self._previous_thresholds.clear()
        self._last_smoothing.clear()

    # ------------------------------------------------------------------
    def apply_overrides(self, overrides: Mapping[str, Any]) -> Dict[str, float]:
        """Apply manual overrides for adaptive thresholds."""

        if not overrides:
            return {}

        applied: Dict[str, float] = {}
        for key, value in overrides.items():
            try:
                numeric = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue

            if key == "volatility_scale":
                low, high = self.scale_bounds
                numeric = max(low, min(high, numeric))
                self._volatility_scale = numeric
                snapshot = {
                    "raw": numeric,
                    "smoothed": numeric,
                    "hysteresis_applied": False,
                }
            else:
                self._previous_thresholds[key] = numeric
                snapshot = {
                    "raw": numeric,
                    "smoothed": numeric,
                    "hysteresis_applied": False,
                }

            self._last_smoothing[key] = snapshot
            applied[key] = numeric

        return applied

    # ------------------------------------------------------------------
    def smooth(
        self,
        value: Mapping[str, float] | float,
        smoothing_factor: float,
        hysteresis_ticks: float,
    ) -> Dict[str, Dict[str, float | bool]]:
        """Smooth ``value`` using exponential decay and hysteresis gating."""

        smoothing = min(1.0, max(0.0, float(smoothing_factor)))
        hysteresis = max(0.0, float(hysteresis_ticks))
        if isinstance(value, Mapping):
            items = value.items()
        else:
            items = (("value", float(value)),)
        results: Dict[str, Dict[str, float | bool]] = {}
        for key, raw_value in items:
            raw = float(raw_value)
            previous = self._previous_thresholds.get(key)
            hysteresis_applied = False
            if previous is None:
                smoothed = raw
            else:
                if abs(raw - previous) <= hysteresis:
                    smoothed = previous
                    hysteresis_applied = True
                elif smoothing <= 0.0:
                    smoothed = raw
                elif smoothing >= 1.0:
                    smoothed = raw
                else:
                    smoothed = previous + smoothing * (raw - previous)
            self._previous_thresholds[key] = smoothed
            details: Dict[str, float | bool] = {
                "raw": raw,
                "smoothed": smoothed,
                "hysteresis_applied": hysteresis_applied,
            }
            results[key] = details
        self._last_smoothing.update(results)
        return results

    # ------------------------------------------------------------------
    def update(self, mid_price: float, timestamp: float) -> float:
        """Ingest a new mid-price observation and return the current scale."""

        mid_price = float(mid_price)
        timestamp = float(timestamp)
        previous = self._last_mid_price
        self._last_mid_price = mid_price
        if previous is not None:
            diff = mid_price - previous
            self._differences.append((timestamp, diff))
            self._trim()
            self._update_volatility()
        return self._volatility_scale

    # ------------------------------------------------------------------
    def scale_threshold(
        self,
        threshold: float,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        """Scale *threshold* using the current volatility multiplier and clamp."""

        value = float(threshold) * self._volatility_scale
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    # ------------------------------------------------------------------
    def quantile_adjust(
        self, metric_name: str, target_quantile: float
    ) -> float | None:
        """Return the rolling quantile for *metric_name* when available.

        The quantile is computed over the bound metrics window and clamped to the
        requested ``target_quantile`` in the inclusive ``[0, 1]`` interval. ``None``
        is returned when insufficient data is available.
        """

        window = self._metrics_window
        if not window:
            return None
        values = []
        for entry in window:
            value = entry.get(metric_name)
            if value is None:
                continue
            value = float(value)
            if isfinite(value):
                values.append(value)
        if not values:
            return None
        q = max(0.0, min(1.0, float(target_quantile)))
        if q <= 0.0 or len(values) == 1:
            return min(values)
        if q >= 1.0:
            return max(values)
        ordered = sorted(values)
        sample_size = min(len(ordered), 100)
        if sample_size < 2:
            return ordered[0]
        try:
            cut_points = quantiles(ordered, n=sample_size, method="inclusive")
        except (StatisticsError, ValueError):
            index = round(q * (len(ordered) - 1))
            index = max(0, min(len(ordered) - 1, index))
            return ordered[index]
        if not cut_points:
            index = round(q * (len(ordered) - 1))
            index = max(0, min(len(ordered) - 1, index))
            return ordered[index]
        position = q * (sample_size - 1)
        lower_index = max(0, min(len(cut_points) - 1, int(floor(position))))
        upper_index = max(0, min(len(cut_points) - 1, lower_index + 1))
        lower_value = cut_points[lower_index]
        if upper_index == lower_index:
            return lower_value
        fraction = position - floor(position)
        upper_value = cut_points[upper_index]
        return lower_value + fraction * (upper_value - lower_value)

    # ------------------------------------------------------------------
    def classify_regime(self, volatility: float | None, trend_score: float | None) -> str:
        """Classify the current market regime.

        The regime is determined using the realised volatility and the absolute
        trend score. Volatility and trend thresholds are provided via the
        ``regime_volatility_breakpoints`` and ``regime_trend_breakpoints``
        parameters respectively. The method returns one of
        ``{"calm", "normal", "volatile"}``.
        """

        calm_vol, volatile_vol = self.regime_volatility_breakpoints
        calm_trend, volatile_trend = self.regime_trend_breakpoints
        vol_value = self._volatility_scale
        if volatility is not None:
            try:
                vol_candidate = float(volatility)
            except (TypeError, ValueError):
                vol_candidate = self._volatility_scale
            else:
                if isfinite(vol_candidate):
                    vol_value = abs(vol_candidate)
        abs_trend = 0.0
        if trend_score is not None:
            try:
                trend_candidate = float(trend_score)
            except (TypeError, ValueError):
                pass
            else:
                if isfinite(trend_candidate):
                    abs_trend = abs(trend_candidate)

        regime = "normal"
        if vol_value <= calm_vol:
            regime = "calm"
        elif vol_value >= volatile_vol:
            regime = "volatile"

        if abs_trend >= volatile_trend:
            regime = "volatile"
        elif abs_trend <= calm_trend and regime != "volatile":
            regime = "calm"

        return regime

    # ------------------------------------------------------------------
    def _trim(self) -> None:
        cutoff = self._differences[-1][0] - self.window_seconds if self._differences else 0.0
        while self._differences and self._differences[0][0] < cutoff:
            self._differences.popleft()

    # ------------------------------------------------------------------
    def _update_volatility(self) -> None:
        if len(self._differences) < 2:
            return
        values = [diff for _, diff in self._differences]
        std_dev = float(pstdev(values))
        self._volatility = std_dev
        baseline = self._baseline_volatility
        alpha = self.smoothing
        if baseline is None:
            baseline = std_dev or 1.0
        else:
            baseline = (1.0 - alpha) * baseline + alpha * std_dev
            if baseline <= 0.0:
                baseline = std_dev or 1.0
        self._baseline_volatility = baseline
        if baseline <= 0.0:
            scale = 1.0
        else:
            scale = std_dev / baseline if baseline else 1.0
        low, high = self.scale_bounds
        clamped = min(high, max(low, scale))
        smoothed = self.smooth(
            {"volatility_scale": clamped},
            self.threshold_smoothing,
            self.threshold_hysteresis_ticks,
        )["volatility_scale"]
        self._volatility_scale = float(smoothed["smoothed"])
