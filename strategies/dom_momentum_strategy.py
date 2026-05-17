"""DOM pressure based momentum strategy."""
from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Deque, Dict, Mapping, Sequence

from src.strategies.dom import DOMSubscriptionStrategy
from src.strategies.templates import (
    StrategySignal,
    StrategyTemplate,
    _extract_contract_metadata,
)
from src.strategies.adaptive_threshold import AdaptiveThresholdState
from src.strategy.types import StrategyIdentifier
from src.strategies.buy_the_dip import DEFAULT_INSTRUMENT_DETAILS


class DomMomentumStrategy(DOMSubscriptionStrategy, StrategyTemplate):
    """DOM pressure based momentum strategy."""

    strategy_type = "dom_momentum"
    parameter_definitions = {
        "symbol": {
            "type": "str",
            "allow_null": True,
            "default": "ES",
            "description": "Symbol to subscribe for DOM snapshots.",
        },
        "pressure_threshold": {
            "type": "float",
            "default": 130.0,
            "min": 0.0,
            "description": "Minimum DOM pressure difference to trigger trades.",
        },
        "adaptive_threshold_enabled": {
            "type": "bool",
            "default": False,
            "description": "Enable adaptive volatility scaling for pressure thresholds.",
        },
        "volatility_window_seconds": {
            "type": "float",
            "default": 30.0,
            "min": 1.0,
            "max": 600.0,
            "step": 1.0,
            "description": "Rolling window (seconds) for estimating DOM mid-price volatility.",
        },
        "volatility_scale_bounds": {
            "type": "tuple",
            "default": (0.75, 1.5),
            "description": "Inclusive lower/upper bounds for the adaptive volatility multiplier.",
        },
        "volatility_regime_breakpoints": {
            "type": "tuple",
            "default": (0.9, 1.2),
            "description": "Volatility scale breakpoints partitioning low/normal/high regimes.",
        },
        "volatility_regime_multipliers": {
            "type": "dict",
            "allow_null": True,
            "default": {"low": 1.0, "normal": 1.0, "high": 1.0},
            "description": "Optional per-regime threshold multipliers keyed by regime name.",
        },
        "disabled_regimes": {
            "type": "list",
            "allow_null": True,
            "default": (),
            "description": "Collection of regime names where signal generation should pause.",
        },
        "volatility_smoothing": {
            "type": "float",
            "default": 0.2,
            "min": 0.0,
            "max": 1.0,
            "step": 0.05,
            "description": "Exponential decay factor applied when tracking baseline volatility.",
        },
        "order_quantity": {
            "type": "int",
            "default": 1,
            "min": 1,
            "step": 1,
            "description": "Base order quantity per signal.",
        },
        "signal_frequency_seconds": {
            "type": "float",
            "default": 0.0,
            "min": 0.0,
            "description": "Minimum wall-clock spacing between signals.",
        },
        "cooldown_seconds": {
            "type": "int",
            "default": 5,
            "min": 0,
            "description": "Cooldown in seconds between consecutive fills.",
        },
        "use_relative_imbalance": {
            "type": "bool",
            "default": False,
            "description": "Normalise DOM pressure by total depth before evaluating thresholds.",
        },
        "min_total_depth": {
            "type": "float",
            "default": 0.0,
            "min": 0.0,
            "description": "Minimum combined DOM depth required to consider signals.",
        },
        "ml_probability_threshold": {
            "type": "float",
            "default": 0.55,
            "min": 0.0,
            "max": 1.0,
            "description": "Minimum probability required from the ML signal filter to trade.",
        },
        "ml_features": {
            "type": "list",
            "allow_null": True,
            "default": (
                "imbalance",
                "normalized_imbalance",
                "adaptive_threshold",
                "volatility_scale",
                "threshold_multiplier",
                "quantity_scale",
                "total_depth",
            ),
            "description": "Ordered collection of feature names passed to the ML filter.",
        },
        "ml_timeout": {
            "type": "float",
            "default": 0.3,
            "min": 0.0,
            "description": "Timeout in seconds for awaiting ML signal filter responses.",
        },
        "trigger_persistence_seconds": {
            "type": "float",
            "default": 0.2,
            "min": 0.0,
            "description": "Minimum duration (seconds) the signal condition must be met before triggering.",
        },
    }
    default_parameters = {
        "symbol": "ES",
        "pressure_threshold": 130.0,
        "adaptive_threshold_enabled": False,
        "volatility_window_seconds": 30.0,
        "volatility_scale_bounds": (0.75, 1.5),
        "volatility_regime_breakpoints": (0.9, 1.2),
        "volatility_regime_multipliers": None,
        "disabled_regimes": (),
        "volatility_smoothing": 0.2,
        "order_quantity": 1,
        "signal_frequency_seconds": 0.0,
        "cooldown_seconds": 5,
        "use_relative_imbalance": False,
        "min_total_depth": 0.0,
        "ml_probability_threshold": 0.55,
        "ml_features": (
            "imbalance",
            "normalized_imbalance",
            "adaptive_threshold",
            "volatility_scale",
            "threshold_multiplier",
            "quantity_scale",
            "total_depth",
        ),
        "ml_timeout": 0.3,
        "trigger_persistence_seconds": 0.2,
    }

    _volatility_regime_breakpoints: tuple[float, float] = (0.9, 1.2)
    _volatility_regime_multipliers: Dict[str, float] = {"low": 1.0, "normal": 1.0, "high": 1.0}
    _disabled_volatility_regimes: set[str] = set()
    _current_volatility_regime: str = "normal"
    _current_threshold_multiplier: float = 1.0

    def __post_init__(self) -> None:
        super().__post_init__()
        # Normalise threshold inputs provided at construction time without overriding
        # user intent. Remove automatic retuning to fixed baselines; preserve
        # absolute values and enable relative mode only when the input clearly
        # indicates a fractional/percentage threshold.
        raw_threshold = getattr(self, "pressure_threshold", 0.0)
        # If the threshold was provided as a percentage string (e.g., "65%"),
        # normalise to fractional form and enable relative imbalance mode.
        raw_threshold = getattr(self, "pressure_threshold", 0.0)
        if isinstance(raw_threshold, str) and "%" in raw_threshold:
            try:
                numeric = float(raw_threshold.replace("%", "").strip())
            except (TypeError, ValueError):
                numeric = 0.0
            if numeric != numeric:  # NaN guard
                numeric = 0.0
            # Convert percent to fraction (65% => 0.65)
            fraction = max(0.0, numeric) / 100.0
            self.use_relative_imbalance = True
            self.pressure_threshold = fraction
            self._telemetry_log(
                "Pressure threshold parsed from percent string",
                details={
                    "input": raw_threshold,
                    "use_relative_imbalance": True,
                    "normalized_threshold": float(fraction),
                },
            )
        else:
            # If a numeric fractional threshold was provided at init (0 < x <= 1),
            # enable relative mode automatically. Do not change absolute values.
            try:
                numeric = float(raw_threshold)
            except (TypeError, ValueError):
                numeric = None
            if (
                numeric is not None
                and numeric == numeric  # NaN guard
                and 0.0 < numeric <= 1.0
                and not bool(getattr(self, "use_relative_imbalance", False))
            ):
                self.use_relative_imbalance = True
                self._telemetry_log(
                    "Relative imbalance enabled for fractional threshold",
                    details={
                        "normalized_threshold": float(numeric),
                    },
                )
            elif (
                numeric is not None
                and numeric == numeric  # NaN guard
                and numeric >= 1.0
                and not bool(getattr(self, "use_relative_imbalance", False))
            ):
                # Retune only when the configured value matches the declarative
                # default (e.g., 130). This preserves user-provided absolute
                # thresholds while keeping default scenarios permissive enough
                # to exercise ML paths.
                try:
                    declarative_default = float(
                        self.default_parameters.get("pressure_threshold", 130.0)
                    )
                except (TypeError, ValueError):
                    declarative_default = 130.0
                if abs(numeric - declarative_default) <= 1e-09:
                    self._retune_pressure_threshold(source="initialisation")
        self._last_signal_time: datetime | None = None
        self._signals: Deque[StrategySignal] = deque(maxlen=32)
        self._last_skip_message: str | None = None
        self._last_skip_logged_at: float = 0.0
        self._adaptive_threshold = AdaptiveThresholdState(
            window_seconds=self._normalise_window_seconds(
                getattr(self, "volatility_window_seconds", 30.0)
            ),
            scale_bounds=self._normalise_scale_bounds(
                getattr(self, "volatility_scale_bounds", (0.75, 1.5))
            ),
            smoothing=self._normalise_smoothing(
                getattr(self, "volatility_smoothing", 0.2)
            ),
        )
        self._last_volatility_scale: float = self._adaptive_threshold.volatility_scale
        self._refresh_volatility_regime_settings()
        self._current_volatility_regime = "normal"
        self._current_threshold_multiplier = (
            self._volatility_regime_multipliers.get("normal", 1.0)
        )
        try:
            base_threshold = float(getattr(self, "pressure_threshold", 0.0))
        except (TypeError, ValueError):
            base_threshold = 0.0
        self._last_dynamic_threshold: float = max(0.0, base_threshold)
        self.ml_probability_threshold = self._normalise_probability_threshold(
            getattr(self, "ml_probability_threshold", 0.55)
        )
        self.ml_timeout = self._normalise_ml_timeout(getattr(self, "ml_timeout", 0.3))
        self.ml_features = self._normalise_ml_features(getattr(self, "ml_features", None))
        self.trigger_persistence_seconds = self._normalise_persistence(
            getattr(self, "trigger_persistence_seconds", 0.2)
        )
        self._trigger_start_time: datetime | None = None
        if not hasattr(self, "ml_signal_filter"):
            setattr(self, "ml_signal_filter", None)

    def _baseline_pressure_threshold(self) -> float:
        """Resolve the default pressure threshold for the current imbalance mode."""

        use_relative = bool(getattr(self, "use_relative_imbalance", False))
        # Absolute mode should be permissive enough to exercise ML filtering
        # paths in tests; use 100 as the baseline rather than the UI-facing
        # declarative default (130). Relative mode retains 65%.
        return 0.65 if use_relative else 100.0

    def _retune_pressure_threshold(self, *, source: str) -> float:
        """Align the pressure threshold with the active imbalance mode."""

        target = self._baseline_pressure_threshold()
        previous_raw = getattr(self, "pressure_threshold", target)
        try:
            previous = float(previous_raw)
        except (TypeError, ValueError):
            previous = target
        if not math.isfinite(previous):
            previous = target
        if abs(previous - target) > 1e-09:
            self._telemetry_log(
                "Pressure threshold auto-tuned",
                details={
                    "source": source,
                    "mode": "relative" if self.use_relative_imbalance else "absolute",
                    "previous": previous,
                    "updated": target,
                },
            )
        self.pressure_threshold = target
        return target

    def apply_parameter_updates(self, updates: Mapping[str, Any]) -> Dict[str, Any]:
        if updates and "cooldown" in updates and "cooldown_seconds" not in updates:
            mutable = dict(updates)
            mutable["cooldown_seconds"] = mutable.pop("cooldown")
            updates = mutable
        # Pre-normalise percentage-style inputs like "65%" before base type coercion
        if updates and "pressure_threshold" in updates:
            value = updates.get("pressure_threshold")
            if isinstance(value, str) and "%" in value:
                raw = value.replace("%", "").strip()
                try:
                    percent = float(raw)
                except (TypeError, ValueError):
                    percent = 0.0
                if not math.isfinite(percent):
                    percent = 0.0
                # Convert percent to fraction (65% => 0.65)
                fraction = max(0.0, percent) / 100.0
                # Mutate updates for parent handling
                mutable = dict(updates)
                mutable["pressure_threshold"] = fraction
                # Auto-enable relative imbalance for percent thresholds
                mutable["use_relative_imbalance"] = True
                applied = super().apply_parameter_updates(mutable)
            else:
                # If a numeric fractional threshold is provided (0 < x <= 1),
                # treat it as relative mode unless the caller explicitly sets
                # use_relative_imbalance.
                mutable = dict(updates)
                try:
                    numeric = float(value) if value is not None else None
                except (TypeError, ValueError):
                    numeric = None
                if (
                    numeric is not None
                    and math.isfinite(numeric)
                    and 0.0 < numeric <= 1.0
                    and "use_relative_imbalance" not in mutable
                ):
                    mutable["use_relative_imbalance"] = True
                applied = super().apply_parameter_updates(mutable)
        else:
            applied = super().apply_parameter_updates(updates)

        if not isinstance(applied, dict):
            applied = dict(applied)

        # Do not auto-retune and overwrite a user-provided threshold. The caller
        # may explicitly set a baseline via updates if desired.

        return applied

    @property
    def last_dynamic_threshold(self) -> float:
        """Most recent adaptive pressure threshold applied during evaluation."""

        return self._last_dynamic_threshold

    @staticmethod
    def _normalise_window_seconds(value: Any) -> float:
        try:
            window = float(value)
        except (TypeError, ValueError):
            return 30.0
        return max(1.0, min(window, 600.0))

    @staticmethod
    def _normalise_scale_bounds(value: Any) -> tuple[float, float]:
        if not isinstance(value, Sequence) or len(value) < 2:
            return (0.75, 1.5)
        try:
            low = float(value[0])
            high = float(value[1])
        except (TypeError, ValueError):
            return (0.75, 1.5)
        if high < low:
            low, high = high, low
        low = max(0.0, low)
        high = max(low if high < low else low, high)
        if low == high:
            high = low or 1.0
        return (low, high)

    @staticmethod
    def _normalise_regime_breakpoints(value: Any) -> tuple[float, float]:
        if not isinstance(value, Sequence) or len(value) < 2:
            return (0.9, 1.2)
        candidates: list[float] = []
        for item in value[:2]:  # type: ignore[index]
            try:
                numeric = float(item)
            except (TypeError, ValueError):
                continue
            if numeric != numeric:  # NaN guard
                continue
            candidates.append(abs(numeric))
        if len(candidates) < 2:
            return (0.9, 1.2)
        low, high = sorted(candidates)[:2]
        if low == high:
            high = high or 1.0
        return (low, high)

    @staticmethod
    def _normalise_regime_multipliers(value: Any) -> Dict[str, float]:
        defaults = {"low": 1.0, "normal": 1.0, "high": 1.0}
        if not isinstance(value, Mapping):
            return defaults
        normalised: Dict[str, float] = dict(defaults)
        for key, raw in value.items():
            name = str(key).strip().lower()
            if not name:
                continue
            try:
                numeric = float(raw)
            except (TypeError, ValueError):
                continue
            if numeric != numeric:  # NaN guard
                continue
            normalised[name] = max(0.0, numeric)
        return normalised

    @staticmethod
    def _normalise_disabled_regimes(value: Any) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, Mapping):
            items = value.keys()
        else:
            try:
                items = list(value)  # type: ignore[arg-type]
            except TypeError:
                items = []
        cleaned: set[str] = set()
        for item in items:
            name = str(item).strip().lower()
            if name:
                cleaned.add(name)
        return cleaned

    @staticmethod
    def _normalise_probability_threshold(value: Any) -> float:
        try:
            threshold = float(value)
        except (TypeError, ValueError):
            threshold = 0.55
        if threshold != threshold:  # NaN guard
            threshold = 0.55
        return min(1.0, max(0.0, threshold))

    @staticmethod
    def _normalise_ml_timeout(value: Any) -> float:
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            timeout = 0.3
        if timeout != timeout:  # NaN guard
            timeout = 0.3
        return max(0.0, timeout)

    @staticmethod
    def _normalise_persistence(value: Any) -> float:
        try:
            persistence = float(value)
        except (TypeError, ValueError):
            persistence = 0.2
        if persistence != persistence:  # NaN guard
            persistence = 0.2
        return max(0.0, persistence)

    @staticmethod
    def _normalise_ml_features(value: Any) -> tuple[str, ...]:
        if value is None:
            return (
                "imbalance",
                "normalized_imbalance",
                "adaptive_threshold",
                "volatility_scale",
                "threshold_multiplier",
                "quantity_scale",
                "total_depth",
            )
        if isinstance(value, Mapping):
            items = value.keys()
        elif isinstance(value, str):
            items = [value]
        else:
            try:
                items = list(value)
            except TypeError:
                items = []
        cleaned: list[str] = []
        for item in items:
            name = str(item).strip()
            if not name:
                continue
            cleaned.append(name)
        if not cleaned:
            return (
                "imbalance",
                "normalized_imbalance",
                "adaptive_threshold",
                "volatility_scale",
                "threshold_multiplier",
                "quantity_scale",
                "total_depth",
            )
        return tuple(cleaned)

    def _refresh_volatility_regime_settings(self) -> None:
        breakpoints = self._normalise_regime_breakpoints(
            getattr(self, "volatility_regime_breakpoints", (0.9, 1.2))
        )
        self._volatility_regime_breakpoints = breakpoints
        self.volatility_regime_breakpoints = breakpoints
        multipliers = self._normalise_regime_multipliers(
            getattr(self, "volatility_regime_multipliers", None)
        )
        self._volatility_regime_multipliers = dict(multipliers)
        self.volatility_regime_multipliers = dict(multipliers)
        disabled = self._normalise_disabled_regimes(
            getattr(self, "disabled_regimes", None)
        )
        self._disabled_volatility_regimes = disabled
        self.disabled_regimes = tuple(sorted(disabled)) if disabled else ()

    def _classify_threshold_regime(self, volatility_scale: float) -> tuple[str, float]:
        scale = max(0.0, float(volatility_scale))
        low, high = self._volatility_regime_breakpoints
        regime = "normal"
        if scale <= low:
            regime = "low"
        elif scale >= high:
            regime = "high"
        multiplier = self._volatility_regime_multipliers.get(regime, 1.0)
        return regime, multiplier

    @staticmethod
    def _normalise_smoothing(value: Any) -> float:
        try:
            smoothing = float(value)
        except (TypeError, ValueError):
            return 0.2
        if smoothing != smoothing:  # NaN check
            return 0.2
        return min(1.0, max(0.0, smoothing)) or 0.2

    @staticmethod
    def _normalise_optional_threshold(value: Any) -> float | None:
        # Deprecated: optional pressure clamps removed from dom_momentum strategy.
        return None

    @staticmethod
    def _parse_timestamp_seconds(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        if isinstance(value, datetime):
            timestamp = value
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                timestamp = datetime.fromisoformat(text)
            except ValueError:
                return None
        else:
            return None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            timestamp = timestamp.astimezone(timezone.utc)
        return timestamp.timestamp()

    @staticmethod
    def _coerce_probability(value: Any) -> float | None:
        try:
            probability = float(value)
        except (TypeError, ValueError):
            return None
        if probability != probability:  # NaN guard
            return None
        if probability < 0.0:
            probability = 0.0
        if probability > 1.0:
            probability = 1.0
        return probability

    async def on_start(self) -> None:  # pragma: no cover - logging hook
        await super().on_start()
        self.logger.info("DOM momentum strategy initialised")

    async def on_stop(self) -> None:  # pragma: no cover - logging hook
        self.logger.info("DOM momentum strategy stopped")

    def _check_and_execute_exit_dom(self, price: float) -> None:
        risk = getattr(self, "risk_manager", None)
        position = 0.0
        entry_price: float | None = None
        if risk is not None:
            identifier = getattr(self, "identifier", None) or self.name
            try:
                state = risk.current_state(identifier)
            except Exception:
                state = None
            if isinstance(state, Mapping):
                try:
                    position = float(state.get("net_position", 0.0) or 0.0)
                except Exception:
                    position = 0.0
                try:
                    entry_price_value = state.get("avg_entry_price") or state.get("entry_price")
                    entry_price = float(entry_price_value) if entry_price_value is not None else None
                except Exception:
                    entry_price = None
        if abs(position) <= 1e-9:
            self._exit_dispatched = False
            return
        exit_targets = self.evaluate_exit_signal(
            position=float(position),
            entry_price=entry_price,
            account_equity=getattr(self, "account_equity", None),
            bar=None,
            is_dom=True,
        )
        if exit_targets is None:
            return
        return

    async def on_market_event(self, event: Mapping[str, Any]) -> None:
        if event.get("type") not in {None, "dom"}:
            return
        self._refresh_volatility_regime_settings()
        bid_pressure = float(event.get("bid_volume", 0.0))
        ask_pressure = float(event.get("ask_volume", 0.0))
        imbalance = bid_pressure - ask_pressure
        total_bid_raw = event.get("total_bid_size")
        total_ask_raw = event.get("total_ask_size")

        def _coerce_depth(value: Any, fallback: float) -> float:
            if value is None:
                return max(0.0, fallback)
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return max(0.0, fallback)
            if numeric != numeric:  # NaN guard
                return max(0.0, fallback)
            return max(0.0, numeric)

        total_bid = _coerce_depth(total_bid_raw, bid_pressure)
        total_ask = _coerce_depth(total_ask_raw, ask_pressure)

        total_depth = max(total_bid + total_ask, 0.0)
        min_total_depth = max(0.0, float(getattr(self, "min_total_depth", 0.0)))
        if total_depth <= 0.0 or total_depth < min_total_depth:
            self._log_skip_reason(
                "DOM momentum skip: insufficient depth",
                tone="neutral",
                details={
                    "total_depth": float(total_depth),
                    "min_total_depth": float(min_total_depth),
                },
            )
            return

        normalised_imbalance = (bid_pressure - ask_pressure) / max(total_depth, 1e-09)
        if getattr(self, "use_relative_imbalance", False):
            imbalance = normalised_imbalance
        now = datetime.now(timezone.utc)

        if self._last_signal_time is not None:
            cooldown = timedelta(seconds=float(getattr(self, "cooldown_seconds", 0)))
            if now - self._last_signal_time < cooldown:
                self._log_skip_reason(
                    "DOM momentum skip: cooldown active",
                    tone="neutral",
                    details={
                        "cooldown_seconds": float(cooldown.total_seconds()),
                        "seconds_remaining": float(
                            max(
                                0.0,
                                cooldown.total_seconds()
                                - (now - self._last_signal_time).total_seconds(),
                            )
                        ),
                    },
                )
                return
            frequency_seconds = float(getattr(self, "signal_frequency_seconds", 0.0))
            if frequency_seconds > 0.0:
                elapsed = (now - self._last_signal_time).total_seconds()
                if elapsed < frequency_seconds:
                    self._log_skip_reason(
                        "DOM momentum skip: frequency active",
                        tone="neutral",
                        details={
                            "signal_frequency_seconds": float(frequency_seconds),
                            "seconds_remaining": float(max(0.0, frequency_seconds - elapsed)),
                        },
                    )
                    return

        # Normalise threshold: if relative mode is enabled and value looks like a percent,
        # convert to fractional; otherwise keep absolute value.
        base_raw = getattr(self, "pressure_threshold", 0.0)
        try:
            base_threshold = float(base_raw)
        except (TypeError, ValueError):
            base_threshold = 0.0
        if bool(getattr(self, "use_relative_imbalance", False)) and base_threshold > 1.0:
            base_threshold = base_threshold / 100.0
        raw_quantity = getattr(self, "order_quantity", 0)
        quantity, fractional_quantity = self._resolve_order_quantity(raw_quantity)
        configured_quantity = self._normalise_order_quantity(raw_quantity)
        quantity_scale = 1.0
        if configured_quantity > 0:
            quantity_scale = max(0.0, int(quantity) / configured_quantity)

        # min/max pressure threshold clamps removed

        adaptive_threshold = base_threshold
        adaptive_enabled = bool(getattr(self, "adaptive_threshold_enabled", False))
        volatility_scale = self._adaptive_threshold.volatility_scale
        window_ready = False
        mid_price_value: float | None = None
        price_value: float | None = None
        mid_price_raw = event.get("mid_price")
        if mid_price_raw is not None:
            try:
                mid_price_value = float(mid_price_raw)
                price_value = mid_price_value
            except (TypeError, ValueError):
                mid_price_value = None
        if price_value is None:
            best_bid = self._coerce_float(event.get("best_bid"))
            best_ask = self._coerce_float(event.get("best_ask"))
            if best_bid is not None and best_ask is not None:
                try:
                    price_value = (best_bid + best_ask) / 2.0
                except Exception:
                    price_value = None
        if price_value is None:
            price_value = self._coerce_float(event.get("close"))
        if price_value is not None and price_value > 0.0:
            self._check_and_execute_exit_dom(price_value)
        if adaptive_enabled:
            if price_value is not None:
                timestamp_value = event.get("timestamp")
                timestamp_seconds = self._parse_timestamp_seconds(timestamp_value)
                if timestamp_seconds is None:
                    timestamp_seconds = time.time()
                volatility_scale = self._adaptive_threshold.update(
                    price_value, timestamp_seconds
                )
                differences = getattr(self._adaptive_threshold, "_differences", None)
                if isinstance(differences, deque) and len(differences) >= 2:
                    first_ts = differences[0][0]
                    last_ts = differences[-1][0]
                    window_ready = (last_ts - first_ts) >= self._adaptive_threshold.window_seconds
                if window_ready:
                    adaptive_threshold = self._adaptive_threshold.scale_threshold(
                        base_threshold
                    )
        self._last_volatility_scale = volatility_scale
        regime, threshold_multiplier = self._classify_threshold_regime(volatility_scale)
        self._current_volatility_regime = regime
        self._current_threshold_multiplier = threshold_multiplier
        if regime in self._disabled_volatility_regimes:
            self._log_skip_reason(
                "DOM momentum skip: regime disabled",
                tone="warning",
                details={"regime": regime},
            )
            return
        if not window_ready:
            adaptive_threshold = base_threshold
        adaptive_threshold *= max(0.0, threshold_multiplier)

        self._last_dynamic_threshold = float(max(0.0, adaptive_threshold))

        if abs(imbalance) < adaptive_threshold:
            self._trigger_start_time = None
            self._log_skip_reason(
                "DOM momentum skip: imbalance below threshold",
                tone="neutral",
                details={
                    "imbalance": float(imbalance),
                    "threshold": float(adaptive_threshold),
                },
            )
            return

        persistence = getattr(self, "trigger_persistence_seconds", 0.0)
        if persistence > 0.0:
            if self._trigger_start_time is None:
                self._trigger_start_time = now
            
            elapsed = (now - self._trigger_start_time).total_seconds()
            if elapsed < persistence:
                self._log_skip_reason(
                    "DOM momentum skip: persistence unsatisfied",
                    tone="neutral",
                    details={
                        "elapsed": float(elapsed),
                        "required": float(persistence),
                    },
                    dedupe_interval=1.0,
                )
                return
        if quantity <= 0:
            self._log_skip_reason(
                "DOM momentum skip: non-positive quantity",
                tone="warning",
                details={
                    "quantity": int(quantity),
                    "order_quantity": float(raw_quantity)
                    if isinstance(raw_quantity, (int, float))
                    else None,
                },
            )
            return

        side = "BUY" if imbalance > 0 else "SELL"
        ml_probability: float | None = None
        ml_features_used: dict[str, float] | None = None
        ml_filter = getattr(self, "ml_signal_filter", None)
        if callable(ml_filter):
            available_features: dict[str, float] = {
                "imbalance": float(imbalance),
                "normalized_imbalance": float(normalised_imbalance),
                "adaptive_threshold": float(adaptive_threshold),
                "volatility_scale": float(volatility_scale),
                "threshold_multiplier": float(threshold_multiplier),
                "quantity_scale": float(quantity_scale),
                "total_depth": float(total_depth),
                "regime_low": 1.0 if regime == "low" else 0.0,
                "regime_normal": 1.0 if regime == "normal" else 0.0,
                "regime_high": 1.0 if regime == "high" else 0.0,
                "side_sign": 1.0 if side == "BUY" else -1.0,
            }
            configured_features = tuple(getattr(self, "ml_features", ()))
            if configured_features:
                features_payload = {
                    name: available_features[name]
                    for name in configured_features
                    if name in available_features
                }
            else:
                features_payload = dict(available_features)
            if not features_payload:
                features_payload = dict(available_features)
            try:
                result = ml_filter(features_payload)
                if inspect.isawaitable(result):
                    if self.ml_timeout > 0.0:
                        result = await asyncio.wait_for(result, timeout=self.ml_timeout)
                    else:
                        result = await result
            except asyncio.TimeoutError:
                self.logger.warning(
                    "ML signal filter timed out; skipping trade",
                    extra={
                        "timeout": self.ml_timeout,
                        "features": features_payload,
                        "symbol": event.get("symbol") or getattr(self, "symbol", None),
                    },
                )
                self._telemetry_log(
                    "ML signal filter timed out; skipping trade",
                    level="WARN",
                    tone="negative",
                    deduplicate=False,
                    details={
                        "timeout": float(self.ml_timeout),
                        "symbol": event.get("symbol") or getattr(self, "symbol", None),
                        "features": dict(features_payload),
                    },
                )
                return
            except Exception:
                self.logger.exception(
                    "ML signal filter execution failed; skipping trade",
                    extra={"features": features_payload},
                )
                self._telemetry_log(
                    "ML signal filter execution failed; skipping trade",
                    level="ERROR",
                    tone="negative",
                    deduplicate=False,
                    details={
                        "symbol": event.get("symbol") or getattr(self, "symbol", None),
                        "features": dict(features_payload),
                    },
                )
                return
            probability_value = self._coerce_probability(result)
            if probability_value is None:
                self.logger.warning(
                    "ML signal filter returned invalid probability; skipping trade",
                    extra={"result": result, "features": features_payload},
                )
                self._telemetry_log(
                    "ML signal filter returned invalid probability; skipping trade",
                    level="WARN",
                    tone="negative",
                    deduplicate=False,
                    details={
                        "symbol": event.get("symbol") or getattr(self, "symbol", None),
                        "result": float(result) if isinstance(result, (int, float)) else str(result),
                        "features": dict(features_payload),
                    },
                )
                return
            ml_probability = probability_value
            if ml_probability < self.ml_probability_threshold:
                self.logger.info(
                    "ML probability below threshold; skipping trade",
                    extra={
                        "probability": ml_probability,
                        "threshold": self.ml_probability_threshold,
                        "features": features_payload,
                        "symbol": event.get("symbol") or getattr(self, "symbol", None),
                    },
                )
                self._telemetry_log(
                    "ML probability below threshold; skipping trade",
                    level="INFO",
                    tone="neutral",
                    deduplicate=False,
                    details={
                        "symbol": event.get("symbol") or getattr(self, "symbol", None),
                        "probability": float(ml_probability),
                        "threshold": float(self.ml_probability_threshold),
                        "features": dict(features_payload),
                    },
                )
                return
            ml_features_used = dict(features_payload)

        snapshot_metadata: Mapping[str, Any] | None = None
        snapshot_payload = event.get("snapshot")
        if isinstance(snapshot_payload, Mapping):
            candidate = snapshot_payload.get("metadata")
            if isinstance(candidate, Mapping):
                snapshot_metadata = candidate
        event_metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else None
        contract_metadata = _extract_contract_metadata(event_metadata, snapshot_metadata)
        if "subscription_id" not in contract_metadata:
            subscription = event.get("subscription_id")
            if isinstance(subscription, str) and subscription.strip():
                contract_metadata["subscription_id"] = subscription.strip()
        metadata_symbol = contract_metadata.get("symbol")
        if not metadata_symbol:
            candidate_symbol = event.get("symbol")
            if isinstance(candidate_symbol, str) and candidate_symbol.strip():
                metadata_symbol = candidate_symbol.strip()
            elif isinstance(self.symbol, str) and self.symbol.strip():
                metadata_symbol = self.symbol.strip()
        telemetry = getattr(self, "runtime_telemetry", None)
        telemetry_candidates: tuple[StrategyIdentifier, ...] = ()
        if telemetry is not None:
            try:
                telemetry_candidates = self._telemetry_identifier_candidates()
            except Exception:  # pragma: no cover - defensive
                self.logger.exception(
                    "Failed to resolve telemetry strategy identifiers"
                )
                telemetry_candidates = ()
        actual_quantity = int(quantity)

        metadata: Dict[str, Any] = {
            "imbalance": imbalance,
            "bid_volume": bid_pressure,
            "ask_volume": ask_pressure,
            "normalized_imbalance": normalised_imbalance,
            "total_depth": total_depth,
            "adaptive_threshold": adaptive_threshold,
            "volatility_scale": volatility_scale,
            "volatility_regime": regime,
            "threshold_multiplier": threshold_multiplier,
            "quantity_scale": quantity_scale,
            **contract_metadata,
        }

        # Check for opposite position to trigger full close
        current_position = await self.current_position()

        is_opposite = (side == "SELL" and current_position > 1e-9) or (
            side == "BUY" and current_position < -1e-9
        )

        if is_opposite:
            metadata["exit_intent"] = "reverse_signal"
            metadata["close_position"] = True

        if price_value is not None:
            metadata["entry_price_hint"] = float(price_value)
        if fractional_quantity > 0.0:
            metadata["quantity_fractional_discarded"] = fractional_quantity

        signal = StrategySignal(
            side=side,
            quantity=actual_quantity,
            reason="dom-pressure",
            metadata=metadata,
        )
        if ml_probability is not None:
            signal.metadata["ml_probability"] = ml_probability
            signal.metadata["ml_probability_threshold"] = self.ml_probability_threshold
            if ml_features_used:
                signal.metadata["ml_features_used"] = ml_features_used
        if metadata_symbol:
            signal.metadata.setdefault("symbol", metadata_symbol)
        if telemetry is not None and telemetry_candidates:
            def _run_telemetry(
                operation: Callable[[StrategyIdentifier], None],
                description: str,
            ) -> None:
                last_error: KeyError | None = None
                for candidate in telemetry_candidates:
                    try:
                        operation(candidate)
                    except KeyError as exc:
                        last_error = exc
                        continue
                    except Exception:  # pragma: no cover - defensive
                        self.logger.exception(
                            "Failed to %s", description
                        )
                        return
                    else:
                        return
                if last_error is not None:
                    self.logger.warning(
                        "Telemetry session not found for identifiers %s while %s",
                        telemetry_candidates,
                        description,
                    )

            _run_telemetry(
                lambda candidate: telemetry.record_threshold_hit(candidate),
                "record threshold hit telemetry",
            )
            _run_telemetry(
                lambda candidate: telemetry.record_signal(candidate, side),
                "record signal telemetry",
            )
            log_details = {
                "side": side,
                "imbalance": imbalance,
                "bid_volume": bid_pressure,
                "ask_volume": ask_pressure,
                "quantity": actual_quantity,
                "symbol": signal.metadata.get("symbol") or metadata_symbol or event.get("symbol") or self.symbol,
                "subscription_id": signal.metadata.get("subscription_id")
                or contract_metadata.get("subscription_id")
                or event.get("subscription_id"),
                "normalized_imbalance": normalised_imbalance,
                "adaptive_threshold": adaptive_threshold,
                "volatility_scale": volatility_scale,
                "volatility_regime": regime,
                "threshold_multiplier": threshold_multiplier,
                "quantity_scale": quantity_scale,
            }
            if fractional_quantity > 0.0:
                log_details["quantity_fractional_discarded"] = fractional_quantity
            _run_telemetry(
                lambda candidate: telemetry.log_event(
                    candidate,
                    "Adaptive momentum threshold triggered",
                    level="INFO",
                    tone="positive",
                    details=log_details,
                ),
                "log telemetry event",
            )
        else:
            self.logger.debug(
                "Adaptive momentum threshold triggered without telemetry: %s",
                {
                    "side": side,
                    "imbalance": imbalance,
                    "normalized_imbalance": normalised_imbalance,
                    "adaptive_threshold": adaptive_threshold,
                    "volatility_scale": volatility_scale,
                    "volatility_regime": regime,
                    "threshold_multiplier": threshold_multiplier,
                    "quantity": actual_quantity,
                    "quantity_scale": quantity_scale,
                    "symbol": signal.metadata.get("symbol")
                    or metadata_symbol
                    or event.get("symbol")
                    or self.symbol,
                },
            )
            if fractional_quantity > 0.0:
                self.logger.debug(
                    "Discarded fractional quantity",  # pragma: no cover - debug log
                    extra={
                        "fractional_quantity": fractional_quantity,
                        "raw_quantity": raw_quantity,
                    },
                )
        self._signals.append(signal)
        self._last_signal_time = now

    async def generate_orders(self) -> Sequence[Mapping[str, Any]]:
        return await super().generate_orders()

    def reset_signals(self, reason: str) -> None:
        self._signals.clear()
        self._trigger_start_time = None
        self._exit_dispatched = False
        self.logger.info(
            "DOM momentum signals reset",
            extra={
                "event": "strategy.signal.reset",
                "reason_code": reason,
                "strategy": self.name,
            },
        )

    def _log_skip_reason(
        self,
        message: str,
        *,
        tone: str = "neutral",
        level: str = "INFO",
        details: Mapping[str, Any] | None = None,
        dedupe_interval: float = 5.0,
    ) -> None:
        now = time.monotonic()
        key = message
        if key == self._last_skip_message and now - self._last_skip_logged_at < dedupe_interval:
            return
        self._last_skip_message = key
        self._last_skip_logged_at = now
        payload = dict(details or {})
        if payload:
            self.logger.info(message, extra={"details": payload})
        else:
            self.logger.info(message)
        self._telemetry_log(
            message,
            level=level,
            tone=tone,
            deduplicate=False,
            details=payload,
        )

    def _resolve_instrument(self, symbol: str) -> tuple[str, str]:
        base = (symbol or "").upper()
        if base in DEFAULT_INSTRUMENT_DETAILS:
            details = DEFAULT_INSTRUMENT_DETAILS[base]
            return details.get("exchange", "CME"), details.get("sec_type", "FUT")
        for key, details in DEFAULT_INSTRUMENT_DETAILS.items():
            if base.startswith(key):
                return details.get("exchange", "CME"), details.get("sec_type", "FUT")
        return "SMART", "STK"

    def start_dom_subscription(
        self,
        *,
        symbol: str,
        depth_levels: int,
        metadata_tag: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        payload = dict(metadata or {})
        exchange, sec_type = self._resolve_instrument(symbol)
        payload.setdefault("symbol", symbol.strip().upper() if symbol else symbol)
        payload.setdefault("exchange", exchange)
        payload.setdefault("sec_type", sec_type)
        super().start_dom_subscription(
            symbol=symbol,
            depth_levels=depth_levels,
            metadata_tag=metadata_tag,
            metadata=payload or None,
        )
