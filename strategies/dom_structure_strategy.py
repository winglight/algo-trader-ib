
"""Structure-aware DOM strategy orchestrated via DOM service."""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, ClassVar, Deque, Dict, Mapping, Sequence

from src.market_data.dom_metrics import DOMAnalyticsProcessor
from src.orders.models import OrderSide
from src.risk.engine import OrderEvaluationContext, RiskDecision
from src.strategy.runtime import DomRuntimeTelemetryService
from src.strategy.types import StrategyIdentifier
from src.strategies.adaptive_threshold import AdaptiveThresholdState, ThresholdModelClient
from src.strategies.dom import DOMSubscriptionStrategy
from .templates import (
    StrategySignal,
    StrategyTemplate,
    _DEFAULT_REGIME_CONDITION_OVERRIDES,
    _extract_contract_metadata,
)

try:  # pragma: no cover - optional dependency wiring
    from src.dom.service import DomSnapshot
except Exception:  # pragma: no cover - fallback for typing when dom package unavailable
    DomSnapshot = Any  # type: ignore[misc, assignment]


@dataclass
class DomStructureStrategy(DOMSubscriptionStrategy, StrategyTemplate):
    """Structure-aware DOM strategy orchestrated via the DOM service."""

    name: str = "DOM Structure Strategy"
    description: str = (
        "Automates DOM-driven trades by coordinating the DOM service, trend filters, "
        "structure windows, and loss streak controls."
    )
    symbol: str = ""
    subscription_id: str | None = None
    event_dispatcher: Callable[[str, Mapping[str, Any]], Awaitable[Any] | None] | None = field(
        default=None, init=False, repr=False
    )
    # Shared defaults (fit the "common" section in the UI screenshot).
    cooldown_seconds: float = 15.0
    max_loss_streak: int = 3
    signal_frequency_seconds: float = 60.0
    default_quantity: float = 1.0
    min_processing_interval: float = 0.05
    regime_volatility_breakpoints: tuple[float, float] = (0.9, 1.2)
    regime_trend_breakpoints: tuple[float, float] = (0.1, 0.25)
    regime_condition_overrides: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict
    )

    # DOM specific defaults (Strategy Parameters section in the screenshot).
    trend_threshold: float | None = 0.5
    structure_window_seconds: float = 10.0
    structure_tolerance_ticks: float = 2.0
    min_signal_conditions: int = 3
    depth_levels: int = 10
    stacking_intensity_threshold: float = 15.0
    ofi_threshold: float = 6.0
    obi_long_threshold: float = 0.58
    obi_short_threshold: float = 0.42
    volatility_window_seconds: float = 30.0
    volatility_scale_bounds: tuple[float, float] = (0.75, 1.5)
    fake_breakout_max: float | None = 0.6
    momentum_tick_threshold: float = 0.25
    adaptive_threshold_smoothing: float = 0.5
    adaptive_threshold_hysteresis_ticks: float = 0.25
    quantity_scale_exponent: float = 1.0
    quantity_scale_bounds: tuple[float, float] = (0.5, 2.0)
    cooldown_scale_exponent: float = 1.0
    cooldown_scale_bounds: tuple[float, float] = (0.5, 2.0)
    threshold_model_enabled: bool = False
    threshold_model_refresh_seconds: float = 1.0
    threshold_model_timeout_seconds: float = 0.25

    strategy_type = "dom_structure"

    COMMON_PARAMETER_DEFINITIONS: ClassVar[Mapping[str, Mapping[str, Any]]] = {
        "symbol": {
            "type": "str",
            "allow_null": True,
            "default": "ES",
            "label": "Primary Symbol",
            "description": "Ticker routed through the DOM service.",
        },
        "subscription_id": {
            "type": "str",
            "allow_null": True,
            "default": None,
            "label": "Subscription ID",
            "description": "Explicit DOM subscription identifier used when filtering events.",
        },
        "cooldown_seconds": {
            "type": "float",
            "default": 15.0,
            "min": 0.0,
            "max": 900.0,
            "label": "Signal Cooldown (s)",
            "description": "Seconds to wait after signalling before evaluating again.",
        },
        "max_loss_streak": {
            "type": "int",
            "default": 3,
            "min": 1,
            "max": 10,
            "label": "Breaker (Max Loss Streak)",
            "description": "Number of consecutive losing trades allowed before pausing signals.",
        },
        "signal_frequency_seconds": {
            "type": "float",
            "default": 60.0,
            "min": 0.0,
            "max": 1800.0,
            "label": "Execution Frequency (s)",
            "description": "Minimum spacing in seconds between emitted signals.",
        },
        "default_quantity": {
            "type": "int",
            "default": 1,
            "min": 0,
            "max": 100,
            "label": "Fallback Quantity (contracts)",
            "description": "Contract count used when risk controls do not override position sizing.",
        },
        "quantity_scale_exponent": {
            "type": "float",
            "default": 1.0,
            "min": 0.0,
            "max": 4.0,
            "label": "Quantity Scale Exponent",
            "description": (
                "Exponent applied to the volatility scale before inverting for quantity sizing.",
            ),
        },
        "quantity_scale_bounds": {
            "type": "tuple",
            "default": (0.5, 2.0),
            "label": "Quantity Scale Bounds",
            "description": "Lower and upper limits for the computed quantity multiplier.",
        },
        "min_processing_interval": {
            "type": "float",
            "default": 0.05,
            "min": 0.0,
            "max": 1.0,
            "label": "Metrics Sampling Interval (s)",
            "description": "Throttle interval for processing incoming DOM snapshots.",
        },
        "cooldown_scale_exponent": {
            "type": "float",
            "default": 1.0,
            "min": 0.0,
            "max": 4.0,
            "label": "Cooldown Scale Exponent",
            "description": (
                "Exponent applied to the volatility scale when stretching cooldown periods.",
            ),
        },
        "cooldown_scale_bounds": {
            "type": "tuple",
            "default": (0.5, 2.0),
            "label": "Cooldown Scale Bounds",
            "description": "Lower and upper limits for the computed cooldown multiplier.",
        },
    }

    DOM_PARAMETER_DEFINITIONS: ClassVar[Mapping[str, Mapping[str, Any]]] = {
        "trend_threshold": {
            "type": "float",
            "allow_null": True,
            "default": 0.5,
            "min": 0.0,
            "max": 5.0,
            "label": "Trend Score Threshold",
            "description": "Minimum DOM trend score (OBI-based) required; set null to disable.",
        },
        "structure_window_seconds": {
            "type": "float",
            "default": 10.0,
            "min": 1.0,
            "max": 120.0,
            "label": "Structure Window (s)",
            "description": "Lookback window used to derive support/resistance zones.",
        },
        "structure_tolerance_ticks": {
            "type": "float",
            "default": 2.0,
            "min": 0.0,
            "max": 10.0,
            "label": "Zone Tolerance (ticks)",
            "description": "Tick offset accepted when matching mid price to structure levels.",
        },
        "min_signal_conditions": {
            "type": "int",
            "default": 3,
            "min": 1,
            "max": 6,
            "label": "Confirmation Count",
            "description": "Number of confirmation conditions that must hold before signalling.",
        },
        "depth_levels": {
            "type": "int",
            "default": 10,
            "min": 1,
            "max": 40,
            "label": "Depth Levels",
            "description": "Number of DOM levels requested from the service.",
        },
        "stacking_intensity_threshold": {
            "type": "float",
            "default": 15.0,
            "min": 0.0,
            "max": 500.0,
            "label": "Stacking Threshold (contracts)",
            "description": "Minimum increase in resting depth on the signal side within the structure window.",
        },
        "ofi_threshold": {
            "type": "float",
            "default": 6.0,
            "min": 0.0,
            "max": 500.0,
            "label": "Order Flow Threshold",
            "description": "Absolute order flow imbalance required in favour of the trade direction.",
        },
        "obi_long_threshold": {
            "type": "float",
            "default": 0.58,
            "min": 0.0,
            "max": 1.0,
            "step": 0.01,
            "label": "Long OBI Threshold",
            "description": "Minimum order book imbalance ratio (0-1) favouring bids before going long.",
        },
        "obi_short_threshold": {
            "type": "float",
            "default": 0.42,
            "min": 0.0,
            "max": 1.0,
            "step": 0.01,
            "label": "Short OBI Threshold",
            "description": "Maximum order book imbalance ratio permitted when entering short positions.",
        },
        "volatility_window_seconds": {
            "type": "float",
            "default": 30.0,
            "min": 1.0,
            "max": 600.0,
            "step": 1.0,
            "label": "Volatility Window (s)",
            "description": "Lookback window for measuring mid-price volatility in seconds.",
        },
        "volatility_scale_bounds": {
            "type": "tuple",
            "default": (0.75, 1.5),
            "label": "Volatility Scale Bounds",
            "description": "Minimum and maximum multipliers applied to adaptive thresholds.",
        },
        "fake_breakout_max": {
            "type": "float",
            "allow_null": True,
            "default": 0.6,
            "min": 0.0,
            "max": 1.0,
            "step": 0.01,
            "label": "Fake Breakout Limit",
            "description": "Upper bound for the fake breakout probability metric; null disables the filter.",
        },
        "momentum_tick_threshold": {
            "type": "float",
            "default": 0.25,
            "min": 0.0,
            "max": 5.0,
            "label": "Momentum Threshold (ticks)",
            "description": "Required mid-price change (in ticks) since the previous snapshot.",
        },
        "threshold_model_enabled": {
            "type": "bool",
            "default": False,
            "label": "Enable Threshold Model",
            "description": (
                "Toggle the external threshold model client for hybrid adaptive tuning."
            ),
        },
        "threshold_model_refresh_seconds": {
            "type": "float",
            "default": 1.0,
            "min": 0.0,
            "max": 60.0,
            "label": "Threshold Model Refresh (s)",
            "description": (
                "Minimum number of seconds between external threshold model refreshes."
            ),
        },
        "target_ofi_quantile": {
            "type": "float",
            "allow_null": True,
            "default": 0.75,
            "min": 0.0,
            "max": 1.0,
            "step": 0.01,
            "label": "Target OFI Quantile",
            "description": "Desired rolling quantile for the order flow imbalance magnitude.",
        },
        "target_stacking_quantile": {
            "type": "float",
            "allow_null": True,
            "default": 0.75,
            "min": 0.0,
            "max": 1.0,
            "step": 0.01,
            "label": "Target Stacking Quantile",
            "description": "Desired rolling quantile for stacking intensity on the active side.",
        },
        "target_obi_long_quantile": {
            "type": "float",
            "allow_null": True,
            "default": 0.7,
            "min": 0.0,
            "max": 1.0,
            "step": 0.01,
            "label": "Target Long OBI Quantile",
            "description": "Rolling quantile target for order book imbalance when going long.",
        },
        "target_obi_short_quantile": {
            "type": "float",
            "allow_null": True,
            "default": 0.3,
            "min": 0.0,
            "max": 1.0,
            "step": 0.01,
            "label": "Target Short OBI Quantile",
            "description": "Rolling quantile target for order book imbalance when going short.",
        },
        "recent_metrics_maxlen": {
            "type": "int",
            "default": 128,
            "min": 1,
            "max": 4096,
            "label": "Recent Metrics Window",
            "description": "Number of normalised metric snapshots retained for quantile blending.",
        },
        "regime_volatility_breakpoints": {
            "type": "tuple",
            "default": (0.9, 1.2),
            "label": "Regime Volatility Breakpoints",
            "description": (
                "Volatility scale thresholds separating calm/normal and normal/volatile regimes."
            ),
        },
        "regime_trend_breakpoints": {
            "type": "tuple",
            "default": (0.1, 0.25),
            "label": "Regime Trend Breakpoints",
            "description": (
                "Absolute trend score thresholds distinguishing calm and volatile regimes."
            ),
        },
        "adaptive_threshold_smoothing": {
            "type": "float",
            "default": 0.5,
            "label": "Adaptive Threshold Smoothing",
            "description": (
                "Exponential smoothing factor applied to adaptive threshold updates."
            ),
        },
        "adaptive_threshold_hysteresis_ticks": {
            "type": "float",
            "default": 0.25,
            "label": "Adaptive Threshold Hysteresis",
            "description": (
                "Minimum absolute change required before thresholds update from raw values."
            ),
        },
        "regime_condition_overrides": {
            "type": "dict",
            "allow_null": True,
            "default": _DEFAULT_REGIME_CONDITION_OVERRIDES,
            "label": "Regime Condition Overrides",
            "description": (
                "Mapping of regime names to overrides for required_hits, cooldown_seconds, "
                "and default_quantity."
            ),
        },
    }

    parameter_definitions = {
        **COMMON_PARAMETER_DEFINITIONS,
        **DOM_PARAMETER_DEFINITIONS,
    }

    COMMON_DEFAULTS: ClassVar[Mapping[str, Any]] = {
        "symbol": "ES",
        "subscription_id": None,
        "cooldown_seconds": 15.0,
        "max_loss_streak": 3,
        "signal_frequency_seconds": 60.0,
        "default_quantity": 1.0,
        "quantity_scale_exponent": 1.0,
        "quantity_scale_bounds": (0.5, 2.0),
        "min_processing_interval": 0.05,
        "cooldown_scale_exponent": 1.0,
        "cooldown_scale_bounds": (0.5, 2.0),
    }

    DOM_DEFAULTS: ClassVar[Mapping[str, Any]] = {
        "trend_threshold": 0.5,
        "structure_window_seconds": 10.0,
        "structure_tolerance_ticks": 2.0,
        "min_signal_conditions": 3,
        "depth_levels": 10,
        "stacking_intensity_threshold": 15.0,
        "ofi_threshold": 6.0,
        "obi_long_threshold": 0.58,
        "obi_short_threshold": 0.42,
        "volatility_window_seconds": 30.0,
        "volatility_scale_bounds": (0.75, 1.5),
        "fake_breakout_max": 0.6,
        "momentum_tick_threshold": 0.25,
        "target_ofi_quantile": 0.75,
        "target_stacking_quantile": 0.75,
        "target_obi_long_quantile": 0.7,
        "target_obi_short_quantile": 0.3,
        "recent_metrics_maxlen": 128,
        "regime_volatility_breakpoints": (0.9, 1.2),
        "regime_trend_breakpoints": (0.1, 0.25),
        "adaptive_threshold_smoothing": 0.5,
        "adaptive_threshold_hysteresis_ticks": 0.25,
        "threshold_model_enabled": False,
        "threshold_model_refresh_seconds": 1.0,
        "regime_condition_overrides": _DEFAULT_REGIME_CONDITION_OVERRIDES,
    }
    default_parameters = {
        **COMMON_DEFAULTS,
        **DOM_DEFAULTS,
    }

    _dom_service: Any = field(default=None, init=False, repr=False)
    _risk_engine: Any = field(default=None, init=False, repr=False)
    _listener_remove: Callable[[], None] | None = field(
        default=None, init=False, repr=False
    )
    _dom_subscription_active: bool = field(default=False, init=False, repr=False)
    _dom_subscription_retry_attempts: int = field(default=0, init=False, repr=False)
    _dom_subscription_symbol: str | None = field(default=None, init=False, repr=False)
    _dom_subscription_depth_levels: int | None = field(
        default=None, init=False, repr=False
    )
    _dom_subscription_metadata_tag: str | None = field(
        default=None, init=False, repr=False
    )
    _dom_subscription_metadata: Mapping[str, Any] | None = field(
        default=None, init=False, repr=False
    )
    _market_data_subscription_health: Any | None = field(
        default=None, init=False, repr=False
    )
    _dom_stream_refresh_inflight: bool = field(default=False, init=False, repr=False)
    _dom_stream_missing_logged: bool = field(default=False, init=False, repr=False)
    _structure_window: Deque[tuple[float, float]] = field(
        default_factory=deque, init=False, repr=False
    )
    _recent_metrics: Deque[Dict[str, float]] = field(
        default_factory=lambda: deque(maxlen=128), init=False, repr=False
    )
    _signals: Deque[StrategySignal] = field(
        default_factory=lambda: deque(maxlen=64), init=False, repr=False
    )
    _analytics: DOMAnalyticsProcessor | None = field(
        default=None, init=False, repr=False
    )
    _latest_metrics: Dict[str, float] = field(
        default_factory=dict, init=False, repr=False
    )
    _latest_adaptive_thresholds: Dict[str, Dict[str, float | bool]] = field(
        default_factory=dict, init=False, repr=False
    )
    _adaptive_threshold_overrides: Dict[str, float] = field(
        default_factory=dict, init=False, repr=False
    )
    _adaptive_threshold: AdaptiveThresholdState = field(
        default_factory=AdaptiveThresholdState, init=False, repr=False
    )
    _threshold_model_client: ThresholdModelClient | None = field(
        default=None, init=False, repr=False
    )
    _threshold_model_last_refresh: float = field(default=0.0, init=False, repr=False)
    _latest_model_thresholds: Dict[str, float] = field(
        default_factory=dict, init=False, repr=False
    )
    _last_volatility_scale: float = field(default=1.0, init=False, repr=False)
    _current_regime: str = field(default="normal", init=False, repr=False)
    _regime_overrides: Dict[str, Dict[str, Any]] = field(
        default_factory=dict, init=False, repr=False
    )
    _regime_runtime_settings: Dict[str, Any] = field(
        default_factory=dict, init=False, repr=False
    )
    _regime_runtime_context: Dict[str, Any] = field(
        default_factory=dict, init=False, repr=False
    )
    _momentum_ready: bool = field(default=False, init=False, repr=False)
    _last_mid_price: float | None = field(default=None, init=False, repr=False)
    _last_process_monotonic: float = field(default=0.0, init=False, repr=False)
    _last_signal_monotonic: float = field(default=0.0, init=False, repr=False)
    _last_signal_wall: float = field(default=0.0, init=False, repr=False)
    _cooldown_until: float = field(default=0.0, init=False, repr=False)
    _cooldown_notice_until: float = field(default=0.0, init=False, repr=False)
    loss_streak: int = field(default=0, init=False)
    breaker_tripped: bool = field(default=False, init=False)
    runtime_telemetry: DomRuntimeTelemetryService | None = field(
        default=None, init=False, repr=False
    )
    _last_runtime_status: str | None = field(default=None, init=False, repr=False)

    _TICK_SIZES: ClassVar[Mapping[str, float]] = {
        "ES": 0.25,
        "MES": 0.25,
        "NQ": 0.25,
        "MNQ": 0.25,
        "YM": 1.0,
        "MYM": 1.0,
        "RTY": 0.1,
        "M2K": 0.1,
        "VX": 0.05,
    }

    def __post_init__(self) -> None:
        super().__post_init__()
        self.symbol = self.symbol.upper().strip()
        if self.subscription_id is not None:
            text = str(self.subscription_id).strip()
            self.subscription_id = text or None
        if not self.subscription_id and self.symbol:
            self.subscription_id = self.symbol
        self.cooldown_seconds = max(0.0, float(self.cooldown_seconds))
        self.structure_window_seconds = max(1.0, float(self.structure_window_seconds))
        self.structure_tolerance_ticks = max(0.0, float(self.structure_tolerance_ticks))
        self.min_signal_conditions = max(1, int(self.min_signal_conditions))
        self.signal_frequency_seconds = max(0.0, float(self.signal_frequency_seconds))
        try:
            parsed_quantity = float(self.default_quantity)
        except (TypeError, ValueError):
            parsed_quantity = 0.0
        self.default_quantity = max(0, int(parsed_quantity))
        self.min_processing_interval = max(0.0, float(self.min_processing_interval))
        self.depth_levels = max(1, int(self.depth_levels))
        self.stacking_intensity_threshold = max(
            0.0, float(self.stacking_intensity_threshold)
        )
        self.ofi_threshold = max(0.0, float(self.ofi_threshold))
        self.obi_long_threshold = max(0.0, min(1.0, float(self.obi_long_threshold)))
        self.obi_short_threshold = max(0.0, min(1.0, float(self.obi_short_threshold)))
        self.volatility_window_seconds = max(1.0, float(self.volatility_window_seconds))
        self.volatility_scale_bounds = self._normalise_volatility_bounds(
            self.volatility_scale_bounds
        )
        self.quantity_scale_exponent = max(0.0, float(self.quantity_scale_exponent))
        self.cooldown_scale_exponent = max(0.0, float(self.cooldown_scale_exponent))
        self.quantity_scale_bounds = self._normalise_scale_bounds(
            self.quantity_scale_bounds, default=(0.5, 2.0)
        )
        self.cooldown_scale_bounds = self._normalise_scale_bounds(
            self.cooldown_scale_bounds, default=(0.5, 2.0)
        )
        self.regime_volatility_breakpoints = self._normalise_regime_breakpoints(
            self.regime_volatility_breakpoints,
            fallback=(0.9, 1.2),
        )
        self.regime_trend_breakpoints = self._normalise_regime_breakpoints(
            self.regime_trend_breakpoints,
            fallback=(0.1, 0.25),
            absolute=True,
        )
        if self.fake_breakout_max is not None:
            self.fake_breakout_max = max(0.0, min(1.0, float(self.fake_breakout_max)))
        self.momentum_tick_threshold = max(0.0, float(self.momentum_tick_threshold))
        self.recent_metrics_maxlen = max(1, int(getattr(self, "recent_metrics_maxlen", 128)))
        self._recent_metrics = deque(self._recent_metrics, maxlen=self.recent_metrics_maxlen)
        self.threshold_model_enabled = bool(self.threshold_model_enabled)
        self.threshold_model_refresh_seconds = max(
            0.0, float(self.threshold_model_refresh_seconds)
        )
        self.threshold_model_timeout_seconds = max(
            0.0, float(self.threshold_model_timeout_seconds)
        )
        for attr in (
            "target_ofi_quantile",
            "target_stacking_quantile",
            "target_obi_long_quantile",
            "target_obi_short_quantile",
        ):
            value = getattr(self, attr, None)
            if value is None:
                continue
            setattr(self, attr, max(0.0, min(1.0, float(value))))
        self.adaptive_threshold_smoothing = min(
            1.0, max(0.0, float(self.adaptive_threshold_smoothing))
        )
        self.adaptive_threshold_hysteresis_ticks = max(
            0.0, float(self.adaptive_threshold_hysteresis_ticks)
        )
        self._update_regime_overrides()
        self._adaptive_threshold = AdaptiveThresholdState(
            window_seconds=self.volatility_window_seconds,
            scale_bounds=self.volatility_scale_bounds,
            regime_volatility_breakpoints=self.regime_volatility_breakpoints,
            regime_trend_breakpoints=self.regime_trend_breakpoints,
            threshold_smoothing=self.adaptive_threshold_smoothing,
            threshold_hysteresis_ticks=self.adaptive_threshold_hysteresis_ticks,
        )
        self._adaptive_threshold.set_metrics_window(self._recent_metrics)
        self._last_volatility_scale = self._adaptive_threshold.volatility_scale
        self._current_regime = "normal"
        initial_settings = self._resolve_regime_settings(self._current_regime)
        self._regime_runtime_settings = dict(initial_settings)
        self._regime_runtime_context = {
            "regime": self._current_regime,
            "settings": dict(initial_settings),
            "volatility": 0.0,
            "volatility_scale": self._adaptive_threshold.volatility_scale,
            "trend_score": 0.0,
        }
        self._ensure_analytics(reset=True)
        self._momentum_ready = False

    # ------------------------------------------------------------------
    def set_dependencies(
        self,
        *,
        risk_engine: Any | None = None,
        position_provider: Callable[[str], float] | None = None,
        runtime_telemetry: DomRuntimeTelemetryService | None = None,
        event_dispatcher: Callable[[str, Mapping[str, Any]], Awaitable[Any] | None]
        | None = None,
        threshold_model_client: ThresholdModelClient | None = None,
        **dependencies: Any,
    ) -> None:
        super().set_dependencies(
            position_provider=position_provider,
            runtime_telemetry=runtime_telemetry,
            event_dispatcher=event_dispatcher,
            **dependencies,
        )
        if risk_engine is not None:
            self._risk_engine = risk_engine
        if runtime_telemetry is not None:
            self.runtime_telemetry = runtime_telemetry
        if event_dispatcher is not None:
            self.event_dispatcher = event_dispatcher
        facade = dependencies.get("market_data_subscription_health") or getattr(
            self, "market_data_subscription_health", None
        )
        self._market_data_subscription_health = facade
        if threshold_model_client is not None:
            self._threshold_model_client = threshold_model_client

    # ------------------------------------------------------------------
    def start(self) -> bool:
        if not self.symbol:
            self.logger.warning("DomStructureStrategy requires a symbol parameter")
            return False
        dispatcher = self.event_dispatcher or getattr(self, "event_dispatcher", None)
        if dispatcher is None or not callable(dispatcher):
            self.logger.warning("DomStructureStrategy missing event_dispatcher dependency")
            return False
        started = super().start()
        if not started:
            return False
        self._telemetry_start_session()
        return True

    # ------------------------------------------------------------------
    def stop(self) -> bool:
        stopped = super().stop()
        if not stopped:
            return False
        self._telemetry_stop_session("DOM structure strategy stopped")
        return True

    async def _verify_dom_stream_health(self) -> None:
        await super()._verify_dom_stream_health()

    # ------------------------------------------------------------------
    def record_trade_result(self, pnl: float) -> None:
        if pnl < 0:
            self.loss_streak += 1
            if self.loss_streak >= max(1, self.max_loss_streak):
                self.breaker_tripped = True
                self._telemetry_status(
                    "Loss breaker tripped",
                    level="WARN",
                    tone="warning",
                )
        else:
            self.loss_streak = 0
            self.breaker_tripped = False
            self._telemetry_status("Loss breaker reset", tone="neutral")

    # ------------------------------------------------------------------
    def _telemetry(self) -> DomRuntimeTelemetryService | None:
        return self.runtime_telemetry

    def _telemetry_strategy_id(self) -> StrategyIdentifier:
        identifier = getattr(self, "identifier", None)
        return identifier if identifier is not None else self.name

    def _telemetry_candidates(self) -> tuple[StrategyIdentifier, ...]:
        helper = getattr(self, "_telemetry_identifier_candidates", None)
        if callable(helper):
            try:
                candidates = tuple(helper())
            except Exception:  # pragma: no cover - defensive
                self.logger.exception("Failed to resolve telemetry candidate identifiers")
                candidates = ()
        else:
            candidates = ()
        if candidates:
            return candidates
        return (self._telemetry_strategy_id(),)

    def _telemetry_start_session(self) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        try:
            telemetry.start_session(
                self._telemetry_strategy_id(),
                subscription_id=self.subscription_id or self.symbol or None,
                symbol=self.symbol or None,
            )
        except Exception:  # pragma: no cover - defensive
            self.logger.exception("Failed to start DOM runtime telemetry session")

    def _telemetry_stop_session(self, reason: str | None = None) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        try:
            telemetry.stop_session(self._telemetry_strategy_id(), reason=reason)
        except KeyError:
            return
        except Exception:  # pragma: no cover - defensive
            self.logger.exception("Failed to stop DOM runtime telemetry session")

    def _telemetry_record_snapshot(self, snapshot: DomSnapshot | Mapping[str, Any]) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        if isinstance(snapshot, Mapping):
            subscription_id = (
                snapshot.get("subscription_id")
                or self.subscription_id
                or self.symbol
                or None
            )
            raw_ts = snapshot.get("timestamp")
            if isinstance(raw_ts, datetime):
                timestamp = raw_ts.replace(tzinfo=timezone.utc) if raw_ts.tzinfo is None else raw_ts.astimezone(timezone.utc)
            elif isinstance(raw_ts, str):
                text = raw_ts.strip()
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                try:
                    parsed = datetime.fromisoformat(text)
                except ValueError:
                    parsed = None
                if parsed is None:
                    timestamp = None
                else:
                    timestamp = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
            else:
                timestamp = None
            symbol = snapshot.get("symbol")
        else:
            subscription_id = (
                snapshot.metadata.get("subscription_id")
                or self.subscription_id
                or self.symbol
                or None
            )
            timestamp = snapshot.received_at
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            symbol = snapshot.symbol
        candidates = self._telemetry_candidates()
        last_error: KeyError | None = None
        for candidate in candidates:
            try:
                telemetry.record_dom_snapshot(
                    candidate,
                    timestamp=timestamp,
                    subscription_id=subscription_id,
                    symbol=symbol,
                )
            except KeyError as exc:
                last_error = exc
                continue
            except Exception:  # pragma: no cover - defensive
                self.logger.exception("Failed to record DOM snapshot telemetry")
                return
            else:
                return
        if last_error is not None:
            self.logger.warning(
                "Telemetry session not found for identifiers %s while recording DOM snapshot",
                candidates,
            )

    def _telemetry_record_threshold_hit(self) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        details_payload = self._compose_detail_payload()
        candidates = self._telemetry_candidates()
        last_error: KeyError | None = None
        for candidate in candidates:
            try:
                telemetry.record_threshold_hit(candidate)
                if details_payload:
                    telemetry.log_event(
                        candidate,
                        "Signal threshold reached",
                        level="INFO",
                        tone="neutral",
                        details=dict(details_payload),
                    )
            except KeyError as exc:
                last_error = exc
                continue
            except Exception:  # pragma: no cover - defensive
                self.logger.exception("Failed to record threshold hit telemetry")
                return
            else:
                return
        if last_error is not None:
            self.logger.warning(
                "Telemetry session not found for identifiers %s while recording threshold hit",
                candidates,
            )

    def _telemetry_record_signal(self, side: str) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        candidates = self._telemetry_candidates()
        last_error: KeyError | None = None
        for candidate in candidates:
            try:
                telemetry.record_signal(candidate, side)
            except KeyError as exc:
                last_error = exc
                continue
            except Exception:  # pragma: no cover - defensive
                self.logger.exception("Failed to record signal telemetry")
                return
            else:
                return
        if last_error is not None:
            self.logger.warning(
                "Telemetry session not found for identifiers %s while recording signal",
                candidates,
            )

    def _telemetry_log(
        self,
        message: str,
        *,
        level: str = "INFO",
        tone: str = "neutral",
        details: Mapping[str, Any] | None = None,
        deduplicate: bool = True,
    ) -> None:
        # Always log to the standard logger, regardless of telemetry availability.
        # Only suppress duplicates based on the last runtime status.
        if deduplicate and message == self._last_runtime_status:
            return
        try:
            log_level = getattr(logging, str(level).upper())
        except AttributeError:
            log_level = logging.INFO
        details_payload: Dict[str, Any] | None = None
        if details is not None:
            try:
                details_payload = dict(details)
            except Exception:  # pragma: no cover - defensive
                self.logger.exception("Failed to normalise telemetry details")
                details_payload = None
        extra: dict[str, Any] = {
            "event": "strategy.telemetry",
            "strategy": self.name,
            "tone": tone,
        }
        if details_payload:
            extra["telemetry_details"] = dict(details_payload)
        self.logger.log(log_level, message, extra=extra)
        # Attempt telemetry dispatch if available.
        telemetry = self._telemetry()
        if telemetry is None:
            if deduplicate:
                self._last_runtime_status = message
            else:
                self._last_runtime_status = None
            return
        candidates = self._telemetry_candidates()
        last_error: KeyError | None = None
        for candidate in candidates:
            try:
                telemetry.log_event(
                    candidate,
                    message,
                    level=level,
                    tone=tone,
                    details=dict(details_payload) if details_payload is not None else None,
                )
            except KeyError as exc:
                last_error = exc
                continue
            except Exception:  # pragma: no cover - defensive
                self.logger.exception("Failed to log telemetry event")
                return
            else:
                if deduplicate:
                    self._last_runtime_status = message
                else:
                    self._last_runtime_status = None
                return
        if last_error is not None:
            self.logger.warning(
                "Telemetry session not found for identifiers %s while logging telemetry event",
                candidates,
            )

    def _telemetry_status(
        self,
        message: str,
        *,
        level: str = "INFO",
        tone: str = "neutral",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self._telemetry_log(
            message,
            level=level,
            tone=tone,
            details=details,
            deduplicate=True,
        )

    def _telemetry_clear_status(self) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        candidates = self._telemetry_candidates()
        last_error: KeyError | None = None
        for candidate in candidates:
            try:
                telemetry.clear_status_cause(candidate)
            except KeyError as exc:
                last_error = exc
                continue
            except Exception:  # pragma: no cover - defensive
                self.logger.exception("Failed to clear telemetry status")
                return
            else:
                return
        if last_error is not None:
            self.logger.warning(
                "Telemetry session not found for identifiers %s while clearing telemetry status",
                candidates,
            )

    # ------------------------------------------------------------------
    async def _on_dom_snapshot(self, snapshot: DomSnapshot) -> None:
        if not self.active:
            return
        if snapshot.symbol.upper() != self.symbol:
            return
        self._telemetry_record_snapshot(snapshot)
        now_monotonic = time.monotonic()
        if (
            self.min_processing_interval > 0.0
            and now_monotonic - self._last_process_monotonic
            < self.min_processing_interval
        ):
            return
        self._last_process_monotonic = now_monotonic
        mid_price = self._extract_mid_price(snapshot)
        if mid_price is None:
            return
        imbalance, total_bid, total_ask = self._compute_imbalance(snapshot)
        total_depth = max(total_bid + total_ask, 0.0)
        normalised_imbalance = (
            float(imbalance) / max(total_depth, 1e-09) if total_depth > 0.0 else 0.0
        )
        spread = self._compute_spread(snapshot)
        now_wall = snapshot.received_at.timestamp()
        self._update_structure(now_wall, mid_price)
        support, resistance = self._structure_extremes()
        zone = self._detect_zone(mid_price, support, resistance)
        if zone is None:
            return
        trend_score = self._trend_score(imbalance, total_bid, total_ask)
        volatility_scale = self._adaptive_threshold.volatility_scale
        realised_volatility = self._adaptive_threshold.volatility
        regime = self._adaptive_threshold.classify_regime(
            volatility=volatility_scale, trend_score=trend_score
        )
        self._current_regime = regime
        momentum = self._compute_momentum(mid_price)
        metrics = self._update_metrics(snapshot, mid_price, spread)
        model_thresholds: Mapping[str, float] | None = None
        if self.threshold_model_enabled and self._threshold_model_client is not None:
            features = self._prepare_threshold_model_features(
                zone=zone,
                regime=regime,
                volatility_scale=volatility_scale,
                realised_volatility=realised_volatility,
                trend_score=trend_score,
                momentum=momentum,
                imbalance=imbalance,
                normalised_imbalance=normalised_imbalance,
                total_depth=total_depth,
                spread=spread,
                metrics=metrics,
            )
            refreshed = await self._maybe_refresh_threshold_model(
                features,
                monotonic_now=now_monotonic,
            )
            if refreshed:
                model_thresholds = refreshed
                model_scale = refreshed.get("volatility_scale")
                model_numeric = self._coerce_numeric(model_scale)
                if model_numeric is not None:
                    low, high = self._adaptive_threshold.scale_bounds
                    bounded_scale = max(low, min(high, model_numeric))
                    volatility_scale = bounded_scale
                    self._last_volatility_scale = bounded_scale
                    detail = dict(
                        self._latest_adaptive_thresholds.get("volatility_scale", {})
                    )
                    if not detail:
                        detail = {
                            "raw": bounded_scale,
                            "smoothed": bounded_scale,
                            "hysteresis_applied": False,
                            "quantile": None,
                        }
                    detail["smoothed"] = float(bounded_scale)
                    detail["model_applied"] = True
                    detail["model_value"] = float(model_numeric)
                    if model_numeric != bounded_scale:
                        detail["model_raw"] = float(model_numeric)
                    self._latest_adaptive_thresholds["volatility_scale"] = detail
            else:
                model_thresholds = None
        condition_flags = self._evaluate_conditions(
            zone=zone,
            imbalance=imbalance,
            spread=spread,
            trend_score=trend_score,
            momentum=momentum,
            metrics=metrics,
            model_thresholds=model_thresholds,
        )
        if not isinstance(condition_flags, Mapping):
            condition_flags = {
                str(index): bool(flag)
                for index, flag in enumerate(condition_flags)  # type: ignore[arg-type]
            }
        condition_hits = sum(1 for value in condition_flags.values() if value)
        runtime_settings = self._resolve_regime_settings(regime)
        required_hits = int(runtime_settings.get("required_hits", self.min_signal_conditions))
        cooldown_seconds = float(runtime_settings.get("cooldown_seconds", self.cooldown_seconds))
        default_quantity = float(
            runtime_settings.get("default_quantity", self.default_quantity)
        )
        runtime_settings = {
            "required_hits": required_hits,
            "cooldown_seconds": max(0.0, cooldown_seconds),
            "default_quantity": max(0.0, default_quantity),
        }
        self._regime_runtime_settings = dict(runtime_settings)
        base_cooldown_seconds = runtime_settings["cooldown_seconds"]
        base_default_quantity = runtime_settings["default_quantity"]
        quantity_scale = self._compute_quantity_scale(volatility_scale)
        cooldown_scale = self._compute_cooldown_scale(volatility_scale)
        scaled_quantity = max(0.0, base_default_quantity * quantity_scale)
        contract_quantity = 0
        if scaled_quantity > 0.0:
            contract_quantity = max(0, int(math.floor(scaled_quantity)))
        scaled_cooldown_seconds = max(0.0, base_cooldown_seconds * cooldown_scale)
        self._regime_runtime_context = {
            "regime": regime,
            "settings": dict(runtime_settings),
            "volatility": realised_volatility,
            "volatility_scale": volatility_scale,
            "trend_score": trend_score,
            "quantity_scale": quantity_scale,
            "cooldown_scale": cooldown_scale,
            "scaled_settings": {
                "default_quantity": contract_quantity,
                "default_quantity_raw": scaled_quantity,
                "cooldown_seconds": scaled_cooldown_seconds,
            },
        }
        required_hits = runtime_settings["required_hits"]
        cooldown_seconds = scaled_cooldown_seconds
        default_quantity = contract_quantity
        if condition_hits < required_hits:
            self._telemetry_log(
                "DOM confirmations below threshold",
                tone="neutral",
                details={
                    "hits": condition_hits,
                    "required": required_hits,
                    "conditions": dict(condition_flags),
                },
            )
            return
        self._telemetry_record_threshold_hit()
        now_monotonic = time.monotonic()
        if now_monotonic < self._cooldown_until:
            if self._cooldown_notice_until != self._cooldown_until:
                self._telemetry_status("Cooling down before next signal", tone="warning")
                self._cooldown_notice_until = self._cooldown_until
            return
        else:
            self._cooldown_notice_until = 0.0
        if self.breaker_tripped:
            self._telemetry_status(
                "Breaker tripped due to loss streak",
                tone="warning",
            )
            return
        if (
            self.signal_frequency_seconds > 0.0
            and now_wall - self._last_signal_wall < self.signal_frequency_seconds
        ):
            self._telemetry_status(
                "Signal suppressed by frequency guard",
                tone="warning",
            )
            return
        side = self._resolve_side(zone, imbalance)
        if side is None:
            self._telemetry_status("No valid signal side resolved", tone="neutral")
            return
        quantity = int(default_quantity) if default_quantity else 0
        if quantity <= 0:
            self._telemetry_status("Scaled quantity is zero", tone="warning")
            return
        risk_permitted, risk_reason = self._passes_risk(side, quantity)
        if not risk_permitted:
            message = "Signal blocked by risk controls"
            details = None
            if risk_reason:
                message = f"{message}: {risk_reason}"
                details = {"risk_reason": risk_reason}
            self._telemetry_status(
                message,
                tone="warning",
                details=details,
            )
            return
        tick_size = self._tick_size()
        stacking_key = "stacking_intensity_buy" if zone == "support" else "stacking_intensity_sell"
        contract_metadata = _extract_contract_metadata(snapshot.metadata)
        metadata = {
            "zone": zone,
            "mid_price": mid_price,
            "support": support,
            "resistance": resistance,
            "imbalance": imbalance,
            "normalized_imbalance": normalised_imbalance,
            "total_depth": total_depth,
            "trend_score": trend_score,
            "momentum": momentum,
            "spread": spread,
            "condition_hits": condition_hits,
            "volatility": realised_volatility,
            "volatility_scale": volatility_scale,
            "quantity_scale": quantity_scale,
            "cooldown_scale": cooldown_scale,
            "scaled_quantity": scaled_quantity,
            "order_quantity": quantity,
            "scaled_cooldown": cooldown_seconds,
            "regime": regime,
            "volatility_regime": regime,
            "regime_settings": dict(self._regime_runtime_settings),
            "obi": metrics.get("obi"),
            "ofi": metrics.get("ofi"),
            "stacking_intensity": metrics.get(stacking_key),
            "fake_breakout_prob": metrics.get("fake_breakout_prob"),
            "momentum_ticks": (momentum / tick_size) if tick_size else momentum,
            "metrics_snapshot": {
                "obi": metrics.get("obi"),
                "ofi": metrics.get("ofi"),
                "stacking": metrics.get(stacking_key),
                "imbalance_ratio": metrics.get("imbalance_ratio"),
                "fake_breakout_prob": metrics.get("fake_breakout_prob"),
            },
            "conditions": dict(condition_flags),
            "entry_price_hint": mid_price,
        }
        if model_thresholds:
            metadata["model_thresholds"] = dict(model_thresholds)
        if self._latest_adaptive_thresholds:
            metadata["adaptive_thresholds"] = {
                key: self._format_adaptive_detail(detail, include_hysteresis=True)
                for key, detail in self._latest_adaptive_thresholds.items()
            }
            metadata["adaptive_debug"] = {
                key: self._format_adaptive_detail(detail, include_hysteresis=False)
                for key, detail in self._latest_adaptive_thresholds.items()
            }
        if contract_metadata:
            metadata.update(contract_metadata)
        if "subscription_id" not in metadata:
            subscription = None
            if isinstance(snapshot.metadata, Mapping):
                subscription = snapshot.metadata.get("subscription_id")
            metadata["subscription_id"] = (
                subscription or self.subscription_id or self.symbol or snapshot.symbol
            )
        if "symbol" not in metadata or not metadata["symbol"]:
            fallback_symbol = None
            if isinstance(snapshot.metadata, Mapping):
                fallback_symbol = snapshot.metadata.get("symbol")
            metadata["symbol"] = fallback_symbol or snapshot.symbol
        signal = StrategySignal(
            side=side,
            quantity=quantity,
            reason="dom-structure",
            metadata=metadata,
        )
        self._signals.append(signal)
        await self._dispatch_signal_event(signal, snapshot)
        self._last_signal_monotonic = now_monotonic
        self._last_signal_wall = now_wall
        self._cooldown_until = now_monotonic + cooldown_seconds
        self._cooldown_notice_until = 0.0
        self._telemetry_record_signal(side)
        self._telemetry_log(
            "Signal generated",
            level="INFO",
            tone="positive",
            details=metadata,
            deduplicate=False,
        )

    # ------------------------------------------------------------------
    async def _dispatch_signal_event(
        self, signal: StrategySignal, snapshot: DomSnapshot
    ) -> None:
        dispatcher = self.event_dispatcher or getattr(self, "event_dispatcher", None)
        self.logger.info("dispatcher %s", dispatcher)
        if dispatcher is None:
            return
        timestamp = snapshot.received_at
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            event_timestamp = timestamp.isoformat()
        else:
            event_timestamp = datetime.now(timezone.utc).isoformat()
        event_metadata = dict(signal.metadata)
        if "subscription_id" not in event_metadata:
            subscription = None
            if isinstance(snapshot.metadata, Mapping):
                subscription = snapshot.metadata.get("subscription_id")
            event_metadata["subscription_id"] = (
                subscription or self.subscription_id or self.symbol or snapshot.symbol
            )
        if "symbol" not in event_metadata or not event_metadata["symbol"]:
            fallback_symbol = None
            if isinstance(snapshot.metadata, Mapping):
                fallback_symbol = snapshot.metadata.get("symbol")
            event_metadata["symbol"] = fallback_symbol or snapshot.symbol
        event: Dict[str, Any] = {
            "stream": "dom",
            "data_stream": "dom",
            "type": "dom_structure_signal",
            "side": signal.side,
            "quantity": signal.quantity,
            "reason": signal.reason,
            "symbol": event_metadata.get("symbol", snapshot.symbol),
            "timestamp": event_timestamp,
            "metadata": event_metadata,
        }
        contract_fields = _extract_contract_metadata(event_metadata)
        if contract_fields:
            event.update(contract_fields)
        try:
            result = dispatcher(self.name, event)
            self.logger.info("dispatcher result %s", result)
            if asyncio.iscoroutine(result):
                await result
                self.logger.info("DOM structure signal event dispatched successfully")
        except Exception:
            self.logger.exception("Failed to dispatch DOM structure signal event")

    # ------------------------------------------------------------------
    async def on_start(self) -> None:  # pragma: no cover - logging hook
        self.logger.info("DOM structure strategy initialised")

    # ------------------------------------------------------------------
    async def on_stop(self) -> None:  # pragma: no cover - logging hook
        self.logger.info("DOM structure strategy stopped")

    # ------------------------------------------------------------------
    async def on_market_event(self, event: Mapping[str, Any]) -> None:
        if event.get("type") not in {None, "dom"}:
            return
        if not self.active:
            return
        symbol = str(event.get("symbol") or "").strip().upper()
        if symbol and self.symbol and symbol != self.symbol:
            return
        snapshot_mapping = event.get("snapshot") or {}
        bids_raw = snapshot_mapping.get("bids") or []
        asks_raw = snapshot_mapping.get("asks") or []
        from decimal import Decimal
        from datetime import datetime, timezone
        from src.dom.service import DomLevel, DomSnapshot as _DomSnapshot
        def _to_decimal(x: Any) -> Decimal:
            try:
                return Decimal(str(x))
            except Exception:
                return Decimal("0")
        bids = tuple(
            DomLevel(price=_to_decimal(level.get("price")), size=_to_decimal(level.get("size")))
            for level in (item for item in bids_raw if isinstance(item, Mapping))
        )
        asks = tuple(
            DomLevel(price=_to_decimal(level.get("price")), size=_to_decimal(level.get("size")))
            for level in (item for item in asks_raw if isinstance(item, Mapping))
        )
        ts_raw = event.get("timestamp") or snapshot_mapping.get("timestamp")
        if isinstance(ts_raw, datetime):
            received_at = ts_raw.astimezone(timezone.utc) if ts_raw.tzinfo else ts_raw.replace(tzinfo=timezone.utc)
        elif isinstance(ts_raw, (int, float)):
            received_at = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
        elif isinstance(ts_raw, str):
            text = ts_raw.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                parsed = datetime.now(timezone.utc)
            received_at = parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        else:
            received_at = datetime.now(timezone.utc)
        # compose metadata with metrics from event
        metadata = {k: v for k, v in snapshot_mapping.items() if k not in {"bids", "asks", "symbol"}}
        metrics: dict[str, Any] = {}
        for key in ("mid_price", "spread", "total_bid_size", "total_ask_size", "best_bid", "best_ask", "depth"):
            value = event.get(key)
            if value is not None:
                metrics[key] = value
        if metrics:
            metadata.setdefault("metrics", {})
            metadata["metrics"].update(metrics)
        dom_snapshot = _DomSnapshot(
            symbol=symbol or (snapshot_mapping.get("symbol") or self.symbol or "").strip().upper(),
            bids=bids,
            asks=asks,
            received_at=received_at,
            metadata=metadata,
        )
        await self._on_dom_snapshot(dom_snapshot)

    # ------------------------------------------------------------------
    async def generate_orders(self) -> Sequence[Mapping[str, Any]]:
        pending_signals = len(self._signals)
        if not pending_signals:
            self.logger.debug("No pending DOM signals to convert into orders")
            return []
        orders: list[Mapping[str, Any]] = []
        while self._signals:
            signal = self._signals.popleft()
            order = signal.as_dict()
            exit_targets = self.evaluate_exit_signal(
                position=float(signal.quantity)
                * (1.0 if signal.side.upper() == OrderSide.BUY.value else -1.0),
                entry_price=self._coerce_float(signal.metadata.get("entry_price_hint")),
                account_equity=getattr(self, "account_equity", None),
                is_dom=True,
            )
            if exit_targets is not None:
                order["metadata"]["exit_mode"] = exit_targets.mode.value
                if exit_targets.stop_loss is not None:
                    order["metadata"]["evaluated_stop_loss"] = float(
                        exit_targets.stop_loss
                    )
                if exit_targets.take_profit is not None:
                    order["metadata"]["evaluated_take_profit"] = float(
                        exit_targets.take_profit
                    )
            orders.append(order)
            self.logger.debug("Converted DOM signal to order payload: %s", order)
        self.logger.info(
            "Generated %d order(s) from DOM signals", len(orders)
        )
        return orders

    # ------------------------------------------------------------------
    def _teardown_listener(self) -> None:
        pass

    # ------------------------------------------------------------------
    def _extract_mid_price(self, snapshot: DomSnapshot) -> float | None:
        best_bid = snapshot.bids[0] if snapshot.bids else None
        best_ask = snapshot.asks[0] if snapshot.asks else None
        if not best_bid or not best_ask:
            mid = snapshot.metadata.get("metrics", {}).get("mid_price")
            return float(mid) if mid is not None else None
        return float((best_bid.price + (best_ask.price - best_bid.price) / 2))

    # ------------------------------------------------------------------
    def _compute_imbalance(
        self, snapshot: DomSnapshot
    ) -> tuple[float, float, float]:
        total_bid = float(sum(level.size for level in snapshot.bids))
        total_ask = float(sum(level.size for level in snapshot.asks))
        metadata = snapshot.metadata.get("metrics", {})
        total_bid = float(metadata.get("total_bid_size", total_bid) or total_bid)
        total_ask = float(metadata.get("total_ask_size", total_ask) or total_ask)
        imbalance = total_bid - total_ask
        return imbalance, total_bid, total_ask

    # ------------------------------------------------------------------
    def _compute_spread(self, snapshot: DomSnapshot) -> float | None:
        metadata_spread = snapshot.metadata.get("metrics", {}).get("spread")
        if metadata_spread is not None:
            try:
                return float(metadata_spread)
            except (TypeError, ValueError):
                return None
        best_bid = snapshot.bids[0] if snapshot.bids else None
        best_ask = snapshot.asks[0] if snapshot.asks else None
        if not best_bid or not best_ask:
            return None
        return float(best_ask.price - best_bid.price)

    # ------------------------------------------------------------------
    def _update_structure(self, timestamp: float, mid_price: float) -> None:
        self._structure_window.append((timestamp, mid_price))
        cutoff = timestamp - self.structure_window_seconds
        while self._structure_window and self._structure_window[0][0] < cutoff:
            self._structure_window.popleft()
        scale = self._adaptive_threshold.update(mid_price, timestamp)
        override_applied = False
        override_value = self._adaptive_threshold_overrides.get("volatility_scale")
        if override_value is not None:
            applied_override = self._adaptive_threshold.apply_overrides(
                {"volatility_scale": override_value}
            )
            override_numeric = applied_override.get("volatility_scale")
            if override_numeric is not None:
                scale = float(override_numeric)
                override_applied = True
        self._last_volatility_scale = scale
        volatility_detail = self._adaptive_threshold.smoothing_results.get(
            "volatility_scale"
        )
        detail_payload: Dict[str, float | bool | None]
        if isinstance(volatility_detail, Mapping):
            detail_payload = {
                "raw": float(volatility_detail.get("raw", scale)),
                "smoothed": float(volatility_detail.get("smoothed", scale)),
                "hysteresis_applied": bool(
                    volatility_detail.get("hysteresis_applied", False)
                ),
                "quantile": None,
            }
        else:
            detail_payload = {
                "raw": float(scale),
                "smoothed": float(scale),
                "hysteresis_applied": False,
                "quantile": None,
            }
        if override_applied:
            detail_payload["override_applied"] = True
        self._latest_adaptive_thresholds["volatility_scale"] = detail_payload
        self._recent_metrics.append(
            {
                "timestamp": timestamp,
                "mid_price": mid_price,
                "volatility_scale": scale,
            }
        )

    # ------------------------------------------------------------------
    def _structure_extremes(self) -> tuple[float | None, float | None]:
        if not self._structure_window:
            return None, None
        prices = [price for _, price in self._structure_window]
        return min(prices), max(prices)

    # ------------------------------------------------------------------
    def _detect_zone(
        self, mid_price: float, support: float | None, resistance: float | None
    ) -> str | None:
        if support is None or resistance is None:
            return None
        tick = self._tick_size()
        tolerance = self.structure_tolerance_ticks * tick
        if mid_price <= support + tolerance:
            return "support"
        if mid_price >= resistance - tolerance:
            return "resistance"
        return None

    # ------------------------------------------------------------------
    def _trend_score(self, imbalance: float, total_bid: float, total_ask: float) -> float:
        total = total_bid + total_ask
        if total <= 0.0:
            return 0.0
        return imbalance / total

    # ------------------------------------------------------------------
    def _compute_momentum(self, mid_price: float) -> float:
        previous = self._last_mid_price
        self._last_mid_price = mid_price
        if previous is None:
            self._momentum_ready = False
            return 0.0
        self._momentum_ready = True
        return mid_price - previous

    # ------------------------------------------------------------------
    def _ensure_analytics(self, *, reset: bool = False) -> DOMAnalyticsProcessor:
        if reset or self._analytics is None:
            self._analytics = DOMAnalyticsProcessor(depth_levels=self.depth_levels)
            self._latest_metrics = {}
        return self._analytics

    # ------------------------------------------------------------------
    def _update_metrics(
        self, snapshot: DomSnapshot, mid_price: float | None, spread: float | None
    ) -> Mapping[str, float]:
        analytics = self._ensure_analytics()
        payload = {
            "timestamp": snapshot.received_at.isoformat(),
            "mid_price": mid_price,
            "spread": spread,
            "bids": [
                {
                    "price": float(getattr(level, "price", 0.0)),
                    "size": float(getattr(level, "size", 0.0)),
                }
                for level in tuple(snapshot.bids)[: self.depth_levels]
            ],
            "asks": [
                {
                    "price": float(getattr(level, "price", 0.0)),
                    "size": float(getattr(level, "size", 0.0)),
                }
                for level in tuple(snapshot.asks)[: self.depth_levels]
            ],
        }
        try:
            metrics = analytics.update(payload)
        except Exception:
            metrics = {}
        normalised: Dict[str, float] = {}
        if metrics:
            for key, value in metrics.items():
                try:
                    normalised[key] = float(value)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
            if normalised:
                self._latest_metrics.update(normalised)
                tracked_keys = (
                    "obi",
                    "ofi",
                    "stacking_intensity_buy",
                    "stacking_intensity_sell",
                )
                tracked = {key: normalised[key] for key in tracked_keys if key in normalised}
                if tracked:
                    self._recent_metrics.append(tracked)
        self._latest_metrics["volatility_scale"] = float(self._last_volatility_scale)
        return dict(self._latest_metrics)

    # ------------------------------------------------------------------
    def _evaluate_conditions(
        self,
        *,
        zone: str,
        imbalance: float,
        spread: float | None,
        trend_score: float,
        momentum: float,
        metrics: Mapping[str, float],
        model_thresholds: Mapping[str, float] | None = None,
    ) -> Mapping[str, bool]:
        tick = self._tick_size()
        results: Dict[str, bool] = {"zone_match": True}
        stacking_key = "stacking_intensity_buy" if zone == "support" else "stacking_intensity_sell"
        adaptive = self._adaptive_threshold
        smoothing_factor = self.adaptive_threshold_smoothing
        hysteresis_ticks = self.adaptive_threshold_hysteresis_ticks
        adaptive_snapshots: Dict[str, Dict[str, float | bool | None]] = {}

        def _coerce_override(
            key: str,
            *,
            minimum: float | None = None,
            maximum: float | None = None,
        ) -> tuple[float | None, bool]:
            value = self._adaptive_threshold_overrides.get(key)
            if value is None:
                return None, False
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return None, False
            if minimum is not None:
                numeric = max(minimum, numeric)
            if maximum is not None:
                numeric = min(maximum, numeric)
            return numeric, True

        def _apply_model_override(
            key: str,
            base_value: float,
            *,
            minimum: float | None = None,
            maximum: float | None = None,
        ) -> tuple[float, float | None, bool]:
            if not model_thresholds:
                return base_value, None, False
            candidate: Any | None
            if isinstance(model_thresholds, Mapping):
                candidate = model_thresholds.get(key)  # type: ignore[index]
            else:
                candidate = None
            if candidate is None:
                return base_value, None, False
            try:
                numeric = float(candidate)
            except (TypeError, ValueError):
                return base_value, None, False
            if not math.isfinite(numeric):
                return base_value, None, False
            raw_numeric = numeric
            if minimum is not None:
                numeric = max(minimum, numeric)
            if maximum is not None:
                numeric = min(maximum, numeric)
            return numeric, raw_numeric, True

        stacking_base = float(self.stacking_intensity_threshold)
        stacking_target = getattr(self, "target_stacking_quantile", None)
        stacking_quantile: float | None = None
        if stacking_target is not None:
            quantile_value = adaptive.quantile_adjust(stacking_key, stacking_target)
            if quantile_value is not None:
                stacking_quantile = float(quantile_value)
                stacking_base = (stacking_base + max(0.0, stacking_quantile)) / 2.0
        stacking_raw = adaptive.scale_threshold(stacking_base, minimum=0.0)
        stacking_override, stacking_override_applied = _coerce_override(
            stacking_key, minimum=0.0
        )
        if stacking_override is not None:
            stacking_raw = stacking_override
        stacking_model_raw: float | None = None
        stacking_model_applied = False
        if not stacking_override_applied:
            stacking_raw, stacking_model_raw, stacking_model_applied = _apply_model_override(
                stacking_key, stacking_raw, minimum=0.0
            )
        stacking_result = adaptive.smooth(
            {stacking_key: stacking_raw}, smoothing_factor, hysteresis_ticks
        )
        stacking_snapshot = stacking_result.get(stacking_key)
        if stacking_snapshot is not None:
            stacking_threshold = float(stacking_snapshot["smoothed"])
            hysteresis_applied = bool(
                stacking_snapshot.get("hysteresis_applied", False)
            )
            raw_value = float(stacking_snapshot.get("raw", stacking_raw))
        else:
            stacking_threshold = stacking_raw
            hysteresis_applied = False
            raw_value = float(stacking_raw)
        detail = {
            "raw": raw_value,
            "smoothed": stacking_threshold,
            "hysteresis_applied": hysteresis_applied,
            "quantile": stacking_quantile,
        }
        if stacking_override_applied:
            detail["override_applied"] = True
        if stacking_model_applied:
            detail["model_applied"] = True
            detail["model_value"] = float(stacking_raw)
            if stacking_model_raw is not None and stacking_model_raw != stacking_raw:
                detail["model_raw"] = float(stacking_model_raw)
        adaptive_snapshots[stacking_key] = detail
        if stacking_threshold > 0.0:
            stacking_value = float(metrics.get(stacking_key, 0.0) or 0.0)
            results["stacking_condition"] = stacking_value >= stacking_threshold
        else:
            results["stacking_condition"] = True
        obi_value = float(metrics.get("obi", 0.5) or 0.5)
        obi_long_base = float(self.obi_long_threshold)
        long_target = getattr(self, "target_obi_long_quantile", None)
        long_quantile: float | None = None
        if long_target is not None:
            quantile_value = adaptive.quantile_adjust("obi", long_target)
            if quantile_value is not None:
                long_quantile = max(0.0, min(1.0, float(quantile_value)))
                obi_long_base = (obi_long_base + long_quantile) / 2.0
        obi_long_raw = adaptive.scale_threshold(
            min(1.0, max(0.0, obi_long_base)), minimum=0.0, maximum=1.0
        )
        obi_long_override, obi_long_override_applied = _coerce_override(
            "obi_long_threshold", minimum=0.0, maximum=1.0
        )
        if obi_long_override is not None:
            obi_long_raw = obi_long_override
        obi_long_model_raw: float | None = None
        obi_long_model_applied = False
        if not obi_long_override_applied:
            obi_long_raw, obi_long_model_raw, obi_long_model_applied = _apply_model_override(
                "obi_long_threshold",
                obi_long_raw,
                minimum=0.0,
                maximum=1.0,
            )
        obi_short_base = float(self.obi_short_threshold)
        short_target = getattr(self, "target_obi_short_quantile", None)
        short_quantile: float | None = None
        if short_target is not None:
            quantile_value = adaptive.quantile_adjust("obi", short_target)
            if quantile_value is not None:
                short_quantile = max(0.0, min(1.0, float(quantile_value)))
                obi_short_base = (obi_short_base + short_quantile) / 2.0
        obi_short_raw = adaptive.scale_threshold(
            min(1.0, max(0.0, obi_short_base)), minimum=0.0, maximum=1.0
        )
        obi_short_override, obi_short_override_applied = _coerce_override(
            "obi_short_threshold", minimum=0.0, maximum=1.0
        )
        if obi_short_override is not None:
            obi_short_raw = obi_short_override
        obi_short_model_raw: float | None = None
        obi_short_model_applied = False
        if not obi_short_override_applied:
            (
                obi_short_raw,
                obi_short_model_raw,
                obi_short_model_applied,
            ) = _apply_model_override(
                "obi_short_threshold",
                obi_short_raw,
                minimum=0.0,
                maximum=1.0,
            )
        obi_results = adaptive.smooth(
            {
            "obi_long_threshold": obi_long_raw,
            "obi_short_threshold": obi_short_raw,
        },
        smoothing_factor,
        hysteresis_ticks,
        )
        obi_long_snapshot = obi_results.get("obi_long_threshold")
        if obi_long_snapshot is not None:
            obi_long_threshold = float(obi_long_snapshot["smoothed"])
            long_raw = float(obi_long_snapshot.get("raw", obi_long_raw))
            long_hysteresis = bool(
                obi_long_snapshot.get("hysteresis_applied", False)
            )
        else:
            obi_long_threshold = obi_long_raw
            long_raw = float(obi_long_raw)
            long_hysteresis = False
        long_detail = {
            "raw": long_raw,
            "smoothed": obi_long_threshold,
            "hysteresis_applied": long_hysteresis,
            "quantile": long_quantile,
        }
        if obi_long_override_applied:
            long_detail["override_applied"] = True
        if obi_long_model_applied:
            long_detail["model_applied"] = True
            long_detail["model_value"] = float(obi_long_raw)
            if (
                obi_long_model_raw is not None
                and obi_long_model_raw != obi_long_raw
            ):
                long_detail["model_raw"] = float(obi_long_model_raw)
        adaptive_snapshots["obi_long_threshold"] = long_detail

        obi_short_snapshot = obi_results.get("obi_short_threshold")
        if obi_short_snapshot is not None:
            obi_short_threshold = float(obi_short_snapshot["smoothed"])
            short_raw = float(obi_short_snapshot.get("raw", obi_short_raw))
            short_hysteresis = bool(
                obi_short_snapshot.get("hysteresis_applied", False)
            )
        else:
            obi_short_threshold = obi_short_raw
            short_raw = float(obi_short_raw)
            short_hysteresis = False
        short_detail = {
            "raw": short_raw,
            "smoothed": obi_short_threshold,
            "hysteresis_applied": short_hysteresis,
            "quantile": short_quantile,
        }
        if obi_short_override_applied:
            short_detail["override_applied"] = True
        if obi_short_model_applied:
            short_detail["model_applied"] = True
            short_detail["model_value"] = float(obi_short_raw)
            if (
                obi_short_model_raw is not None
                and obi_short_model_raw != obi_short_raw
            ):
                short_detail["model_raw"] = float(obi_short_model_raw)
        adaptive_snapshots["obi_short_threshold"] = short_detail
        if zone == "support":
            results["obi_condition"] = obi_value >= obi_long_threshold
        else:
            results["obi_condition"] = obi_value <= obi_short_threshold
        ofi_base = float(self.ofi_threshold)
        ofi_target = getattr(self, "target_ofi_quantile", None)
        ofi_quantile: float | None = None
        if ofi_base > 0.0 and ofi_target is not None:
            quantile_value = adaptive.quantile_adjust("ofi", ofi_target)
            if quantile_value is not None:
                ofi_quantile = abs(float(quantile_value))
                ofi_base = (ofi_base + ofi_quantile) / 2.0
        ofi_raw = adaptive.scale_threshold(ofi_base, minimum=0.0)
        ofi_override, ofi_override_applied = _coerce_override(
            "ofi_threshold", minimum=0.0
        )
        if ofi_override is not None:
            ofi_raw = ofi_override
        ofi_model_raw: float | None = None
        ofi_model_applied = False
        if not ofi_override_applied:
            ofi_raw, ofi_model_raw, ofi_model_applied = _apply_model_override(
                "ofi_threshold", ofi_raw, minimum=0.0
            )
        ofi_result = adaptive.smooth(
            {"ofi_threshold": ofi_raw}, smoothing_factor, hysteresis_ticks
        )
        ofi_snapshot = ofi_result.get("ofi_threshold")
        if ofi_snapshot is not None:
            ofi_threshold = float(ofi_snapshot["smoothed"])
            ofi_raw_value = float(ofi_snapshot.get("raw", ofi_raw))
            ofi_hysteresis = bool(ofi_snapshot.get("hysteresis_applied", False))
        else:
            ofi_threshold = ofi_raw
            ofi_raw_value = float(ofi_raw)
            ofi_hysteresis = False
        ofi_detail = {
            "raw": ofi_raw_value,
            "smoothed": ofi_threshold,
            "hysteresis_applied": ofi_hysteresis,
            "quantile": ofi_quantile,
        }
        if ofi_override_applied:
            ofi_detail["override_applied"] = True
        if ofi_model_applied:
            ofi_detail["model_applied"] = True
            ofi_detail["model_value"] = float(ofi_raw)
            if ofi_model_raw is not None and ofi_model_raw != ofi_raw:
                ofi_detail["model_raw"] = float(ofi_model_raw)
        adaptive_snapshots["ofi_threshold"] = ofi_detail
        if ofi_threshold > 0.0:
            ofi_value = float(metrics.get("ofi", 0.0) or 0.0)
            results["ofi_condition"] = (
                ofi_value >= ofi_threshold if zone == "support" else ofi_value <= -ofi_threshold
            )
        else:
            results["ofi_condition"] = True
        if spread is not None:
            tolerance = max(tick, self.structure_tolerance_ticks * tick)
            results["spread_condition"] = spread <= tolerance
        else:
            results["spread_condition"] = False
        threshold = self.trend_threshold
        if threshold is None:
            results["trend_condition"] = True
        else:
            results["trend_condition"] = abs(trend_score) >= float(threshold)
        fake_breakout_max = self.fake_breakout_max
        if fake_breakout_max is None:
            results["fake_breakout_filter"] = True
        else:
            fake_prob = float(metrics.get("fake_breakout_prob", 0.0) or 0.0)
            results["fake_breakout_filter"] = fake_prob <= float(fake_breakout_max)
        if self.momentum_tick_threshold > 0.0 and tick > 0.0:
            results["momentum_condition"] = (
                (self._momentum_ready and abs(momentum) >= tick * self.momentum_tick_threshold)
                or not self._momentum_ready
            )
        else:
            results["momentum_condition"] = True
        if adaptive_snapshots:
            merged = dict(self._latest_adaptive_thresholds)
            merged.update(adaptive_snapshots)
            self._latest_adaptive_thresholds = merged
            self.logger.debug(
                "Adaptive thresholds updated: %s", self._latest_adaptive_thresholds
            )
        return results

    # ------------------------------------------------------------------
    def _prepare_threshold_model_features(
        self,
        *,
        zone: str,
        regime: str,
        volatility_scale: float,
        realised_volatility: float,
        trend_score: float,
        momentum: float,
        imbalance: float,
        normalised_imbalance: float,
        total_depth: float,
        spread: float | None,
        metrics: Mapping[str, float],
    ) -> Dict[str, float]:
        features: Dict[str, float] = {}

        def _add(name: str, value: Any) -> None:
            numeric = self._coerce_numeric(value)
            if numeric is not None:
                features[name] = numeric

        _add("volatility_scale", volatility_scale)
        _add("realised_volatility", realised_volatility)
        _add("trend_score", trend_score)
        _add("momentum", momentum)
        _add("imbalance", imbalance)
        _add("normalized_imbalance", normalised_imbalance)
        _add("total_depth", total_depth)
        _add("spread", spread)
        _add("volatility_scale_last", self._last_volatility_scale)
        zone_lower = zone.lower()
        features["zone_support"] = 1.0 if zone_lower == "support" else 0.0
        features["zone_resistance"] = 1.0 if zone_lower == "resistance" else 0.0
        features["zone_unknown"] = 1.0 if zone_lower not in {"support", "resistance"} else 0.0
        recognised_regimes = {"calm", "normal", "volatile"}
        for candidate in recognised_regimes:
            features[f"regime_{candidate}"] = 1.0 if regime == candidate else 0.0
        features["regime_other"] = 1.0 if regime not in recognised_regimes else 0.0
        for metric_key in (
            "obi",
            "ofi",
            "imbalance_ratio",
            "stacking_intensity_buy",
            "stacking_intensity_sell",
            "fake_breakout_prob",
        ):
            _add(f"metric_{metric_key}", metrics.get(metric_key))
        for key, detail in self._latest_adaptive_thresholds.items():
            if not isinstance(detail, Mapping):
                continue
            _add(f"adaptive_{key}_smoothed", detail.get("smoothed"))
            _add(f"adaptive_{key}_raw", detail.get("raw"))
            _add(f"adaptive_{key}_quantile", detail.get("quantile"))
        return features

    # ------------------------------------------------------------------
    @staticmethod
    def _coerce_numeric(value: Any) -> float | None:
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return None
        return numeric

    # ------------------------------------------------------------------
    async def _maybe_refresh_threshold_model(
        self,
        features: Mapping[str, float],
        *,
        monotonic_now: float,
    ) -> Mapping[str, float] | None:
        if not self.threshold_model_enabled:
            return None
        if self._threshold_model_client is None:
            return None
        refresh_due = (
            not self._latest_model_thresholds
            or monotonic_now - self._threshold_model_last_refresh
            >= self.threshold_model_refresh_seconds
        )
        if refresh_due:
            result = await self._invoke_threshold_model(features)
            self._threshold_model_last_refresh = monotonic_now
            if result is None:
                self._latest_model_thresholds.clear()
            else:
                self._latest_model_thresholds = dict(result)
        if not self._latest_model_thresholds:
            return None
        return dict(self._latest_model_thresholds)

    # ------------------------------------------------------------------
    async def _invoke_threshold_model(
        self, features: Mapping[str, float]
    ) -> Dict[str, float] | None:
        client = self._threshold_model_client
        if client is None:
            return None
        loop = asyncio.get_running_loop()
        call = functools.partial(client.predict_thresholds, dict(features))
        timeout = self.threshold_model_timeout_seconds or None
        try:
            if timeout:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, call), timeout
                )
            else:
                result = await loop.run_in_executor(None, call)
        except asyncio.TimeoutError:
            self.logger.warning(
                "Threshold model prediction timed out after %.3fs",
                self.threshold_model_timeout_seconds,
            )
            self._telemetry_log(
                "Threshold model timeout; reverting to adaptive thresholds",
                tone="warning",
                details={"timeout_seconds": self.threshold_model_timeout_seconds},
            )
            return None
        except Exception as exc:  # pragma: no cover - defensive guard
            self.logger.exception("Threshold model prediction failed")
            self._telemetry_log(
                "Threshold model error; using adaptive thresholds",
                tone="warning",
                details={"error": str(exc)},
            )
            return None
        if not isinstance(result, Mapping):
            self.logger.warning(
                "Threshold model returned non-mapping payload: %r", result
            )
            self._telemetry_log(
                "Threshold model returned invalid payload",
                tone="warning",
                details={"type": type(result).__name__},
            )
            return None
        sanitized: Dict[str, float] = {}
        for key, value in result.items():
            numeric = self._coerce_numeric(value)
            if numeric is None:
                continue
            sanitized[str(key)] = numeric
        return sanitized

    # ------------------------------------------------------------------
    def _format_adaptive_detail(
        self, detail: Mapping[str, Any], *, include_hysteresis: bool
    ) -> Dict[str, Any]:
        raw = detail.get("raw")
        smoothed = detail.get("smoothed", raw)
        quantile = detail.get("quantile")
        formatted: Dict[str, Any] = {
            "raw": float(raw) if raw is not None else None,
            "smoothed": float(smoothed) if smoothed is not None else None,
            "quantile": float(quantile) if quantile is not None else None,
        }
        if include_hysteresis:
            formatted["hysteresis_applied"] = bool(
                detail.get("hysteresis_applied", False)
            )
            if "override_applied" in detail:
                formatted["override_applied"] = bool(detail.get("override_applied"))
        return formatted

    # ------------------------------------------------------------------
    def _compose_detail_payload(
        self, details: Mapping[str, Any] | None = None
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if details:
            payload.update(details)
        context = getattr(self, "_regime_runtime_context", {})
        if isinstance(context, Mapping):
            regime = context.get("regime")
            if regime is not None and "regime" not in payload:
                payload["regime"] = regime
            settings = context.get("settings")
            if isinstance(settings, Mapping) and "regime_settings" not in payload:
                payload["regime_settings"] = dict(settings)
            for key in ("volatility", "volatility_scale", "trend_score"):
                value = context.get(key)
                if value is not None and key not in payload:
                    payload[key] = value
        if "volatility_scale" not in payload:
            payload["volatility_scale"] = self._adaptive_threshold.volatility_scale
        if "regime" not in payload:
            payload["regime"] = getattr(self, "_current_regime", None)
        if self._latest_adaptive_thresholds and "adaptive_thresholds" not in payload:
            payload["adaptive_thresholds"] = {
                key: self._format_adaptive_detail(detail, include_hysteresis=True)
                for key, detail in self._latest_adaptive_thresholds.items()
            }
        if self._latest_adaptive_thresholds and "adaptive_debug" not in payload:
            payload["adaptive_debug"] = {
                key: self._format_adaptive_detail(detail, include_hysteresis=False)
                for key, detail in self._latest_adaptive_thresholds.items()
            }
        return {k: v for k, v in payload.items() if v is not None}

    # ------------------------------------------------------------------
    def apply_parameter_updates(self, updates: Mapping[str, Any]) -> Dict[str, Any]:
        if not updates:
            return {}

        mutable_updates = dict(updates)
        override_payload = mutable_updates.pop("adaptive_threshold_override", None)
        reset_payload = mutable_updates.pop("reset_adaptive_state", None)

        applied = super().apply_parameter_updates(mutable_updates)

        reset_requested = False
        if reset_payload is not None:
            if isinstance(reset_payload, str):
                reset_requested = reset_payload.strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
            else:
                reset_requested = bool(reset_payload)
        if reset_requested:
            self._adaptive_threshold.reset()
            self._recent_metrics.clear()
            self._latest_adaptive_thresholds.clear()
            self._adaptive_threshold_overrides.clear()
            self._adaptive_threshold.set_metrics_window(self._recent_metrics)
            self._last_volatility_scale = self._adaptive_threshold.volatility_scale
            if isinstance(self._regime_runtime_context, dict):
                self._regime_runtime_context["volatility_scale"] = (
                    self._adaptive_threshold.volatility_scale
                )
                self._regime_runtime_context["volatility"] = (
                    self._adaptive_threshold.volatility
                )
            applied["reset_adaptive_state"] = True

        if override_payload is not None:
            override_mapping = (
                override_payload if isinstance(override_payload, Mapping) else None
            )
            if override_mapping:
                applied_overrides = self._adaptive_threshold.apply_overrides(
                    override_mapping
                )
                if applied_overrides:
                    self._adaptive_threshold_overrides.update(applied_overrides)
                    merged = dict(self._latest_adaptive_thresholds)
                    for key, value in applied_overrides.items():
                        merged[key] = {
                            "raw": float(value),
                            "smoothed": float(value),
                            "hysteresis_applied": False,
                            "quantile": None,
                            "override_applied": True,
                        }
                    self._latest_adaptive_thresholds = merged
                    self._last_volatility_scale = (
                        self._adaptive_threshold.volatility_scale
                    )
                    if isinstance(self._regime_runtime_context, dict):
                        self._regime_runtime_context["volatility_scale"] = (
                            self._adaptive_threshold.volatility_scale
                        )
                    applied["adaptive_threshold_override"] = dict(
                        self._adaptive_threshold_overrides
                    )
            else:
                if self._adaptive_threshold_overrides:
                    self._adaptive_threshold_overrides.clear()
                    applied["adaptive_threshold_override"] = {}

        if "depth_levels" in applied:
            self.depth_levels = max(1, int(self.depth_levels))
            self._ensure_analytics(reset=True)
            self._momentum_ready = False
        if "stacking_intensity_threshold" in applied:
            self.stacking_intensity_threshold = max(
                0.0, float(self.stacking_intensity_threshold)
            )
        if "ofi_threshold" in applied:
            self.ofi_threshold = max(0.0, float(self.ofi_threshold))
        if "obi_long_threshold" in applied:
            self.obi_long_threshold = max(0.0, min(1.0, float(self.obi_long_threshold)))
        if "obi_short_threshold" in applied:
            self.obi_short_threshold = max(0.0, min(1.0, float(self.obi_short_threshold)))
        if "recent_metrics_maxlen" in applied:
            self.recent_metrics_maxlen = max(1, int(self.recent_metrics_maxlen))
            self._recent_metrics = deque(
                self._recent_metrics, maxlen=self.recent_metrics_maxlen
            )
            self._adaptive_threshold.set_metrics_window(self._recent_metrics)
        if "adaptive_threshold_smoothing" in applied:
            self.adaptive_threshold_smoothing = min(
                1.0, max(0.0, float(self.adaptive_threshold_smoothing))
            )
        if "adaptive_threshold_hysteresis_ticks" in applied:
            self.adaptive_threshold_hysteresis_ticks = max(
                0.0, float(self.adaptive_threshold_hysteresis_ticks)
            )
        for attr in (
            "target_ofi_quantile",
            "target_stacking_quantile",
            "target_obi_long_quantile",
            "target_obi_short_quantile",
        ):
            if attr in applied:
                value = getattr(self, attr)
                if value is None:
                    continue
                setattr(self, attr, max(0.0, min(1.0, float(value))))
        if "volatility_window_seconds" in applied:
            self.volatility_window_seconds = max(1.0, float(self.volatility_window_seconds))
        if "volatility_scale_bounds" in applied:
            self.volatility_scale_bounds = self._normalise_volatility_bounds(
                self.volatility_scale_bounds
            )
        if "quantity_scale_bounds" in applied:
            self.quantity_scale_bounds = self._normalise_scale_bounds(
                self.quantity_scale_bounds, default=(0.5, 2.0)
            )
        if "cooldown_scale_bounds" in applied:
            self.cooldown_scale_bounds = self._normalise_scale_bounds(
                self.cooldown_scale_bounds, default=(0.5, 2.0)
            )
        if "regime_volatility_breakpoints" in applied:
            self.regime_volatility_breakpoints = self._normalise_regime_breakpoints(
                self.regime_volatility_breakpoints,
                fallback=(0.9, 1.2),
            )
        if "regime_trend_breakpoints" in applied:
            self.regime_trend_breakpoints = self._normalise_regime_breakpoints(
                self.regime_trend_breakpoints,
                fallback=(0.1, 0.25),
                absolute=True,
            )
        if "regime_condition_overrides" in applied:
            self._update_regime_overrides()
        if set(applied) & {
            "volatility_window_seconds",
            "volatility_scale_bounds",
            "regime_volatility_breakpoints",
            "regime_trend_breakpoints",
            "adaptive_threshold_smoothing",
            "adaptive_threshold_hysteresis_ticks",
        }:
            self._adaptive_threshold.configure(
                window_seconds=self.volatility_window_seconds,
                scale_bounds=self.volatility_scale_bounds,
                regime_volatility_breakpoints=self.regime_volatility_breakpoints,
                regime_trend_breakpoints=self.regime_trend_breakpoints,
                threshold_smoothing=self.adaptive_threshold_smoothing,
                threshold_hysteresis_ticks=self.adaptive_threshold_hysteresis_ticks,
            )
            self._adaptive_threshold.set_metrics_window(self._recent_metrics)
            self._last_volatility_scale = self._adaptive_threshold.volatility_scale
        if set(applied) & {"regime_condition_overrides", "regime_volatility_breakpoints", "regime_trend_breakpoints"}:
            current_settings = self._resolve_regime_settings(self._current_regime)
            self._regime_runtime_settings = dict(current_settings)
            if not isinstance(self._regime_runtime_context, dict):
                self._regime_runtime_context = {}
            self._regime_runtime_context.setdefault("regime", self._current_regime)
            self._regime_runtime_context["settings"] = dict(current_settings)
            self._regime_runtime_context["volatility_scale"] = (
                self._adaptive_threshold.volatility_scale
            )
            self._regime_runtime_context["volatility"] = self._adaptive_threshold.volatility
        if "fake_breakout_max" in applied and self.fake_breakout_max is not None:
            self.fake_breakout_max = max(
                0.0, min(1.0, float(self.fake_breakout_max))
            )
        if "momentum_tick_threshold" in applied:
            self.momentum_tick_threshold = max(0.0, float(self.momentum_tick_threshold))
            self._momentum_ready = False
        return applied

    # ------------------------------------------------------------------
    def _resolve_side(self, zone: str, imbalance: float) -> str | None:
        if zone == "support" and imbalance >= 0.0:
            return OrderSide.BUY.value
        if zone == "resistance" and imbalance <= 0.0:
            return OrderSide.SELL.value
        return None

    # ------------------------------------------------------------------
    def _passes_risk(self, side: str, quantity: float) -> tuple[bool, str | None]:
        engine = self._risk_engine
        if engine is None:
            return True, None
        try:
            order_side = OrderSide(side)
        except ValueError:
            reason = f"Invalid order side: {side!r}"
            self.logger.info("Risk engine blocked DOM order: %s", reason)
            return False, reason
        current_position = self._current_position()
        context = OrderEvaluationContext(
            symbol=self.symbol,
            side=order_side,
            quantity=quantity,
            current_position=current_position,
        )
        decision: RiskDecision = engine.evaluate_order(context)
        if not decision.permitted:
            reason = decision.reason or "Risk engine denied order"
            self.logger.info("Risk engine blocked DOM order: %s", reason)
            return False, reason
        return True, decision.reason

    # ------------------------------------------------------------------
    def _tick_size(self) -> float:
        return float(self._TICK_SIZES.get(self.symbol.upper(), 0.25))

    # ------------------------------------------------------------------
    def _normalise_regime_breakpoints(
        self,
        values: Sequence[float] | tuple[float, float],
        *,
        fallback: tuple[float, float],
        absolute: bool = False,
    ) -> tuple[float, float]:
        try:
            low, high = values  # type: ignore[misc]
        except Exception:
            low, high = fallback
        low = float(low)
        high = float(high)
        if absolute:
            low = abs(low)
            high = abs(high)
        low = max(0.0, low)
        high = max(0.0, high)
        if high < low:
            low, high = high, low
        if high == low:
            increment = 1.0 if low == 0.0 else max(low * 0.1, 0.01)
            high = low + increment
        return float(low), float(high)

    # ------------------------------------------------------------------
    def _normalise_regime_overrides(
        self, overrides: Mapping[str, Mapping[str, Any]] | str | None
    ) -> Dict[str, Dict[str, Any]]:
        if not overrides:
            return {}
        if isinstance(overrides, str):
            data = overrides.strip()
            if not data:
                return {}
            try:
                overrides_obj = json.loads(data)
            except json.JSONDecodeError:
                return {}
            overrides = overrides_obj if isinstance(overrides_obj, Mapping) else {}
        if not isinstance(overrides, Mapping):
            return {}
        overrides = dict(overrides)
        normalised: Dict[str, Dict[str, Any]] = {}
        for regime, settings in overrides.items():
            if isinstance(settings, str):
                parsed = settings.strip()
                if parsed:
                    try:
                        settings_obj = json.loads(parsed)
                    except json.JSONDecodeError:
                        settings_obj = None
                    if isinstance(settings_obj, Mapping):
                        settings = settings_obj
            if not isinstance(settings, Mapping):
                continue
            key = str(regime).strip().lower()
            if key not in {"calm", "normal", "volatile"}:
                continue
            regime_settings: Dict[str, Any] = {}
            if "required_hits" in settings:
                try:
                    value = int(round(float(settings["required_hits"])))
                except (TypeError, ValueError):
                    value = None
                if value is not None and value > 0:
                    regime_settings["required_hits"] = value
            if "cooldown_seconds" in settings:
                try:
                    value = float(settings["cooldown_seconds"])
                except (TypeError, ValueError):
                    value = None
                if value is not None and value >= 0.0:
                    regime_settings["cooldown_seconds"] = value
            if "default_quantity" in settings:
                try:
                    value = float(settings["default_quantity"])
                except (TypeError, ValueError):
                    value = None
                if value is not None and value >= 0.0:
                    regime_settings["default_quantity"] = value
            if regime_settings:
                normalised[key] = regime_settings
        return normalised

    # ------------------------------------------------------------------
    def _update_regime_overrides(self) -> None:
        cleaned = self._normalise_regime_overrides(self.regime_condition_overrides)
        self._regime_overrides = cleaned
        self.regime_condition_overrides = cleaned

    # ------------------------------------------------------------------
    def _resolve_regime_settings(self, regime: str) -> Dict[str, Any]:
        regime_key = str(regime or "normal").strip().lower() or "normal"
        base_settings: Dict[str, Any] = {
            "required_hits": max(1, int(self.min_signal_conditions)),
            "cooldown_seconds": max(0.0, float(self.cooldown_seconds)),
            "default_quantity": max(0.0, float(self.default_quantity)),
        }
        overrides = self._regime_overrides.get(regime_key)
        if overrides is None and regime_key != "normal":
            overrides = self._regime_overrides.get("normal")
        if overrides:
            base_settings.update(overrides)
            base_settings["required_hits"] = max(
                1, int(round(float(base_settings["required_hits"])))
            )
            base_settings["cooldown_seconds"] = max(
                0.0, float(base_settings["cooldown_seconds"])
            )
            base_settings["default_quantity"] = max(
                0.0, float(base_settings["default_quantity"])
            )
        return base_settings

    # ------------------------------------------------------------------
    def _normalise_volatility_bounds(
        self, bounds: Sequence[float] | tuple[float, float]
    ) -> tuple[float, float]:
        try:
            low, high = bounds  # type: ignore[misc]
        except Exception:
            low, high = 0.75, 1.5
        low = max(0.0, float(low))
        high = float(high)
        if high < low:
            low, high = high, low
        if high <= 0.0:
            high = 1.0
        if low == high:
            high = low if low > 0.0 else 1.0
        return float(low), float(max(high, low))

    def _normalise_scale_bounds(
        self,
        bounds: Sequence[float] | tuple[float, float],
        *,
        default: tuple[float, float] = (0.5, 2.0),
    ) -> tuple[float, float]:
        try:
            low, high = bounds  # type: ignore[misc]
        except Exception:
            low, high = default
        low = max(0.0, float(low))
        high = max(0.0, float(high))
        if high < low:
            low, high = high, low
        if low == 0.0 and high == 0.0:
            high = 1.0
        if low == high:
            high = low if low > 0.0 else default[1]
        return float(low), float(max(high, low))

    def _clamp_scale(self, value: float, bounds: tuple[float, float] | Sequence[float]) -> float:
        try:
            low, high = bounds  # type: ignore[misc]
        except Exception:
            low, high = self._normalise_scale_bounds(bounds, default=(0.5, 2.0))
        if high < low:
            low, high = high, low
        return float(min(max(value, low), high))

    def _compute_quantity_scale(self, volatility_scale: float) -> float:
        base_scale = max(float(volatility_scale), 1e-9)
        exponent = max(0.0, float(self.quantity_scale_exponent))
        if exponent == 0.0:
            raw_scale = 1.0
        else:
            raw_scale = base_scale**exponent
        inverted = 1.0 / max(raw_scale, 1e-9)
        return self._clamp_scale(inverted, self.quantity_scale_bounds)

    def _compute_cooldown_scale(self, volatility_scale: float) -> float:
        base_scale = max(float(volatility_scale), 1e-9)
        exponent = max(0.0, float(self.cooldown_scale_exponent))
        if exponent == 0.0:
            raw_scale = 1.0
        else:
            raw_scale = base_scale**exponent
        return self._clamp_scale(raw_scale, self.cooldown_scale_bounds)

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
