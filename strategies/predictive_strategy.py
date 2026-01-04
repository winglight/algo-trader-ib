"""Predictive strategy that consumes AI Model Ops trend probability outputs."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Mapping, MutableSequence, Optional, Sequence

from src.ai_model_ops import schemas
from src.strategy.base import StrategyError

from .templates import StrategySignal, StrategyTemplate

__all__ = ["PredictiveModelState", "PredictiveModelRepository", "PredictiveStrategy"]


NewsSignalProvider = Callable[[str, str, Optional[int]], Sequence[Mapping[str, object]]]


@dataclass(frozen=True)
class PredictiveModelState:
    """Holds parameters required to project probabilities at runtime."""

    version: str
    posterior_mean: float
    posterior_std: float
    policy_score: float
    fusion_config: schemas.FusionConfig
    metrics: Mapping[str, float] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "PredictiveModelState":
        return cls(
            version="uninitialised",
            posterior_mean=0.0,
            posterior_std=1.0,
            policy_score=0.0,
            fusion_config=schemas.FusionConfig(),
            metrics={},
        )

    def project_probability(self, return_rate: float) -> float:
        std = max(self.posterior_std, 1e-6)
        scaled = (return_rate + self.posterior_mean) / std
        probability = 0.5 * (1.0 + math.tanh(scaled))
        return max(0.0, min(1.0, probability))


class PredictiveModelRepository:
    """In-memory repository of predictive model states."""

    def __init__(self) -> None:
        self._states: dict[str, PredictiveModelState] = {}
        self._active_version: Optional[str] = None
        self._listeners: MutableSequence[Callable[[PredictiveModelState], None]] = []

    def upsert(self, state: PredictiveModelState) -> None:
        self._states[state.version] = state
        if self._active_version is None:
            self._active_version = state.version
            self._notify(state)

    def activate(self, version: str) -> None:
        if version not in self._states:
            raise KeyError(f"model version {version!r} unknown")
        self._active_version = version
        self._notify(self._states[version])

    def get_active(self) -> PredictiveModelState:
        if not self._active_version:
            raise LookupError("no predictive model version has been activated")
        return self._states[self._active_version]

    def subscribe(self, listener: Callable[[PredictiveModelState], None]) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[PredictiveModelState], None]) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:  # pragma: no cover - defensive guard
            return

    def _notify(self, state: PredictiveModelState) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(state)
            except Exception:  # pragma: no cover - listeners are optional hooks
                continue


class PredictiveStrategy(StrategyTemplate):
    """Strategy template orchestrating predictive signals with news fusion."""

    name: str = "Predictive Strategy"
    description: str = (
        "Streams market ticks, applies the latest trend probability model, and "
        "optionally fuses news sentiment signals to produce trading orders."
    )
    symbol: str = ""
    timeframe: str = "1h"
    _DEFAULT_QUANTITY = 1
    _default_quantity: int = _DEFAULT_QUANTITY
    _raw_default_quantity: float = float(_DEFAULT_QUANTITY)
    cooldown_seconds: float = 60.0

    strategy_type = "predictive"

    parameter_definitions = {
        "symbol": {
            "type": "str",
            "allow_null": False,
            "label": "Symbol",
            "description": "Primary symbol the strategy consumes.",
        },
        "timeframe": {
            "type": "str",
            "default": "1h",
            "label": "Timeframe",
            "description": "Market data timeframe expected by the predictive model.",
        },
        "default_quantity": {
            "type": "int",
            "default": _DEFAULT_QUANTITY,
            "min": 1,
            "label": "Order Quantity",
            "description": "Base quantity emitted with generated orders.",
        },
    }

    def __post_init__(self) -> None:
        super().__post_init__()
        self.default_quantity = getattr(
            self, "_raw_default_quantity", float(self._DEFAULT_QUANTITY)
        )
        self._model_state = PredictiveModelState.empty()
        self._repository: PredictiveModelRepository | None = None
        self._fusion_defaults = schemas.FusionConfig()
        self._news_provider: NewsSignalProvider = lambda *args, **kwargs: ()
        self._last_price: Optional[float] = None
        self._pending_orders: list[Mapping[str, object]] = []
        self._last_news_payload: Mapping[str, object] | None = None

    # ------------------------------------------------------------------
    @property
    def default_quantity(self) -> int:
        return self._default_quantity

    @default_quantity.setter
    def default_quantity(self, value: object) -> None:
        try:
            raw = float(value)
        except (TypeError, ValueError):
            raw = float(self._DEFAULT_QUANTITY)
        if not math.isfinite(raw):
            raw = float(self._DEFAULT_QUANTITY)
        quantity = int(raw)
        if quantity < 0:
            quantity = 0
        self._raw_default_quantity = raw
        self._default_quantity = quantity

    # ------------------------------------------------------------------
    def set_dependencies(self, **dependencies: object) -> None:
        super().set_dependencies(**dependencies)
        repository = dependencies.get("predictive_model_repository")
        if isinstance(repository, PredictiveModelRepository):
            self._repository = repository
        defaults = dependencies.get("predictive_fusion_defaults")
        if isinstance(defaults, schemas.FusionConfig):
            self._fusion_defaults = defaults
        provider = dependencies.get("news_signal_provider")
        if callable(provider):
            self._news_provider = provider  # type: ignore[assignment]

    # ------------------------------------------------------------------
    async def on_start(self) -> None:
        if not self.symbol:
            raise StrategyError("PredictiveStrategy requires a symbol to start")
        if self._repository is None:
            raise StrategyError("PredictiveStrategy missing predictive model repository")
        self._repository.subscribe(self._on_model_update)
        try:
            state = self._repository.get_active()
        except LookupError:
            state = PredictiveModelState.empty()
        self._model_state = state
        self.logger.info(
            "Predictive strategy initialised with model %s", state.version
        )

    # ------------------------------------------------------------------
    async def on_stop(self) -> None:
        if self._repository is not None:
            self._repository.unsubscribe(self._on_model_update)
        self._pending_orders.clear()
        self._last_price = None
        self._last_news_payload = None

    # ------------------------------------------------------------------
    async def on_market_event(self, event: Mapping[str, object]) -> None:
        price = self._extract_price(event)
        if price is None:
            return
        timestamp = self._extract_timestamp(event)
        if self._last_price is None:
            self._last_price = price
            return
        return_rate = (price - self._last_price) / max(self._last_price, 1e-6)
        self._last_price = price

        base_probability = self._model_state.project_probability(return_rate)
        news_probability, news_confidence = self._resolve_news_probability(
            timestamp, base_probability
        )
        final_probability = news_probability

        long_threshold, short_threshold = self._policy_thresholds(self._model_state)
        self._pending_orders.clear()

        if final_probability >= long_threshold:
            order = self._build_order("BUY", final_probability, news_confidence)
            self._pending_orders.append(order)
        elif final_probability <= short_threshold:
            order = self._build_order("SELL", 1.0 - final_probability, news_confidence)
            self._pending_orders.append(order)

    # ------------------------------------------------------------------
    async def generate_orders(self) -> Sequence[Mapping[str, object]]:
        orders = tuple(self._pending_orders)
        self._pending_orders.clear()
        return orders

    # Internal helpers -------------------------------------------------
    def _on_model_update(self, state: PredictiveModelState) -> None:
        self._model_state = state

    def _extract_price(self, event: Mapping[str, object]) -> Optional[float]:
        for key in ("close", "price", "last_price"):
            value = event.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return None

    def _extract_timestamp(self, event: Mapping[str, object]) -> Optional[int]:
        value = event.get("timestamp")
        if isinstance(value, (int, float)):
            return int(value)
        return None

    def _resolve_news_probability(
        self, timestamp: Optional[int], base_probability: float
    ) -> tuple[float, float]:
        config = self._model_state.fusion_config
        if config.is_default():
            config = self._fusion_defaults

        if not config.enable_news_features or config.strategy is schemas.FusionStrategy.LATE:
            news_probability = base_probability
            news_confidence = 0.0
        else:
            news_probability, news_confidence = self._apply_early_mid_fusion(
                timestamp, base_probability, config
            )

        if config.enable_news_features and config.strategy is schemas.FusionStrategy.LATE:
            news_probability, news_confidence = self._apply_late_adjustment(
                timestamp, base_probability, config
            )

        return news_probability, news_confidence

    def _apply_early_mid_fusion(
        self,
        timestamp: Optional[int],
        base_probability: float,
        config: schemas.FusionConfig,
    ) -> tuple[float, float]:
        aggregated = self._aggregate_news(timestamp, config)
        if aggregated is None:
            return base_probability, 0.0

        news_probability = 0.5 * (aggregated["sentiment"] + 1.0)
        weight = config.resolved_news_weight()
        if config.strategy is schemas.FusionStrategy.MID:
            weight = min(1.0, weight * aggregated["confidence"])
        fused = base_probability * (1.0 - weight) + news_probability * weight
        fused = max(0.0, min(1.0, fused))
        return fused, aggregated["confidence"]

    def _apply_late_adjustment(
        self,
        timestamp: Optional[int],
        base_probability: float,
        config: schemas.FusionConfig,
    ) -> tuple[float, float]:
        aggregated = self._aggregate_news(timestamp, config)
        if aggregated is None:
            self._last_news_payload = None
            return base_probability, 0.0

        news_probability = 0.5 * (aggregated["sentiment"] + 1.0)
        weight = config.resolved_news_weight() * aggregated["confidence"]
        adjusted = base_probability * (1.0 - weight) + news_probability * weight
        adjusted = max(0.0, min(1.0, adjusted))
        self._last_news_payload = {
            "news_probability": news_probability,
            "news_confidence": aggregated["confidence"],
            "news_label": aggregated.get("label"),
        }
        return adjusted, aggregated["confidence"]

    def _aggregate_news(
        self, timestamp: Optional[int], config: schemas.FusionConfig
    ) -> Mapping[str, float] | None:
        provider = self._news_provider
        if provider is None or timestamp is None:
            return None
        try:
            signals = provider(self.symbol, self.timeframe, timestamp)
        except Exception:  # pragma: no cover - provider is user supplied
            return None
        filtered: list[Mapping[str, object]] = []
        threshold = config.confidence_threshold
        for payload in signals or []:
            if not isinstance(payload, Mapping):
                continue
            confidence = float(payload.get("confidence", 0.0))
            if confidence < threshold:
                continue
            sentiment = float(payload.get("sentiment", 0.0))
            sentiment = max(-1.0, min(1.0, sentiment))
            filtered.append({
                "sentiment": sentiment,
                "confidence": max(0.0, min(1.0, confidence)),
                "label": payload.get("label"),
            })
        if not filtered:
            return None
        total_conf = sum(item["confidence"] for item in filtered)
        if total_conf <= 0.0:
            return None
        sentiment = sum(item["sentiment"] * item["confidence"] for item in filtered) / total_conf
        dominant = max(filtered, key=lambda item: item["confidence"])
        averaged_confidence = min(1.0, total_conf / len(filtered))
        return {
            "sentiment": sentiment,
            "confidence": averaged_confidence,
            "label": dominant.get("label"),
        }

    def _policy_thresholds(self, state: PredictiveModelState) -> tuple[float, float]:
        bias = max(0.05, min(0.25, abs(state.policy_score) * 0.1))
        long_threshold = min(0.95, 0.5 + bias)
        short_threshold = max(0.05, 0.5 - bias)
        if state.policy_score < 0:
            long_threshold = min(0.98, long_threshold + 0.05)
            short_threshold = max(0.02, short_threshold - 0.05)
        return long_threshold, short_threshold

    def _build_order(
        self, side: str, confidence: float, news_confidence: float
    ) -> Mapping[str, object]:
        quantity = int(self.default_quantity)
        metadata = {
            "probability": round(confidence, 6),
            "model_version": self._model_state.version,
            "policy_score": self._model_state.policy_score,
            "fusion_strategy": self._model_state.fusion_config.strategy.value,
            "news_enabled": self._model_state.fusion_config.enable_news_features,
            "news_model_version": self._model_state.fusion_config.news_model_version,
            "news_confidence": round(news_confidence, 6),
            "default_quantity_raw": self._raw_default_quantity,
        }
        if self._last_news_payload:
            metadata.update(self._last_news_payload)
        signal = StrategySignal.from_probability(
            side,
            quantity,
            confidence,
            reason=f"predictive-{side.lower()}",
            metadata=metadata,
        )
        return signal.as_dict()

