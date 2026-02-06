"""Predictive strategy that consumes AI Model Ops trend probability outputs."""

from __future__ import annotations

import math
import inspect
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Awaitable, Callable, Mapping, MutableSequence, Optional, Sequence
from src.strategy.base import StrategyError

from .templates import StrategySignal, StrategyTemplate

__all__ = [
    "FusionStrategy",
    "FusionConfig",
    "PredictiveModelState",
    "PredictiveModelRepository",
    "PredictiveStrategy",
]


NewsSignalProvider = Callable[[str, str, Optional[int]], Sequence[Mapping[str, object]]]


class FusionStrategy(str, Enum):
    EARLY = "early"
    MID = "mid"
    LATE = "late"


@dataclass(slots=True)
class FusionConfig:
    enable_news_features: bool = False
    strategy: FusionStrategy | str = FusionStrategy.LATE
    confidence_threshold: float = 0.6
    news_weight: float = 0.5
    weights: Mapping[str, float] | None = None
    news_model_version: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.strategy, str):
            try:
                self.strategy = FusionStrategy(self.strategy)
            except ValueError:
                self.strategy = FusionStrategy.LATE
        if self.news_model_version:
            cleaned = self.news_model_version.strip()
            self.news_model_version = cleaned or None
        if not self.enable_news_features:
            self.strategy = FusionStrategy.LATE
            if self.news_model_version:
                self.news_model_version = None

    def resolved_news_weight(self) -> float:
        if self.weights and "news" in self.weights:
            try:
                value = float(self.weights["news"])
            except (TypeError, ValueError):
                value = self.news_weight
            else:
                if value < 0.0:
                    value = 0.0
                if value > 1.0:
                    value = 1.0
            return value
        return self.news_weight

    def is_default(self) -> bool:
        if self.enable_news_features:
            return False
        if self.strategy is not FusionStrategy.LATE:
            return False
        if self.confidence_threshold != 0.6:
            return False
        if self.news_weight != 0.5:
            return False
        if self.news_model_version is not None:
            return False
        if self.weights:
            return False
        return True


@dataclass(frozen=True)
class PredictiveModelState:
    """Holds parameters required to project probabilities at runtime."""

    version: str
    posterior_mean: float
    posterior_std: float
    policy_score: float
    fusion_config: FusionConfig
    metrics: Mapping[str, float] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "PredictiveModelState":
        return cls(
            version="uninitialised",
            posterior_mean=0.0,
            posterior_std=1.0,
            policy_score=0.0,
            fusion_config=FusionConfig(),
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

    def get(self, version: str) -> PredictiveModelState | None:
        return self._states.get(version)

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
        self._fusion_defaults = FusionConfig()
        self._news_provider: NewsSignalProvider = lambda *args, **kwargs: ()
        self._inference_client: Callable[..., Awaitable[Mapping[str, object]] | Mapping[str, object]] | None = None
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
        if isinstance(defaults, FusionConfig):
            self._fusion_defaults = defaults
        provider = dependencies.get("news_signal_provider")
        if callable(provider):
            self._news_provider = provider  # type: ignore[assignment]
        inference_client = (
            dependencies.get("predictive_inference_client")
            or dependencies.get("predictive_model_inference")
        )
        if callable(inference_client):
            self._inference_client = inference_client  # type: ignore[assignment]

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

        inference_probability = await self._infer_probability(event, return_rate)
        base_probability = (
            inference_probability
            if inference_probability is not None
            else self._model_state.project_probability(return_rate)
        )
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

    async def _infer_probability(
        self, event: Mapping[str, object], return_rate: float
    ) -> Optional[float]:
        client = self._inference_client
        if client is None:
            return None

        record = self._build_inference_record(event, return_rate)
        request_payload = {
            "records": [record],
            "model_version": self._model_state.version,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
        }

        response: Mapping[str, object] | None = None
        try:
            response = await self._invoke_inference_client(client, request_payload)
        except Exception:  # pragma: no cover - inference is optional
            return None
        if not isinstance(response, Mapping):
            return None

        probability = self._extract_inference_probability(response)
        if probability is None:
            return None
        self._apply_inference_update(response)
        return probability

    async def _invoke_inference_client(
        self,
        client: Callable[..., Awaitable[Mapping[str, object]] | Mapping[str, object]],
        payload: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        infer_method = getattr(client, "infer", None)
        if callable(infer_method):
            result = infer_method(
                model_version=payload.get("model_version"),
                records=payload.get("records"),
                symbol=payload.get("symbol"),
                timeframe=payload.get("timeframe"),
            )
            if inspect.isawaitable(result):
                awaited = await result
                return awaited if isinstance(awaited, Mapping) else None
            return result if isinstance(result, Mapping) else None
        try:
            result = client(**payload)  # type: ignore[arg-type]
        except TypeError:
            result = client(payload)
        if isinstance(result, Mapping):
            return result
        if inspect.isawaitable(result):
            awaited = await result
            return awaited if isinstance(awaited, Mapping) else None
        return None

    def _build_inference_record(
        self, event: Mapping[str, object], return_rate: float
    ) -> Mapping[str, float]:
        record: dict[str, float] = {"return_rate": return_rate}
        for key in ("open", "high", "low", "close", "volume"):
            value = event.get(key)
            if isinstance(value, (int, float)):
                record[key] = float(value)
        if "close" not in record and self._last_price is not None:
            record["close"] = float(self._last_price)
        return record

    def _extract_inference_probability(
        self, payload: Mapping[str, object]
    ) -> Optional[float]:
        direct = payload.get("probability")
        if isinstance(direct, (int, float)):
            return float(direct)
        probabilities = payload.get("probabilities")
        if isinstance(probabilities, Sequence) and probabilities:
            first = probabilities[0]
            if isinstance(first, (int, float)):
                return float(first)
        return None

    def _apply_inference_update(self, payload: Mapping[str, object]) -> None:
        version = payload.get("model_version")
        if isinstance(version, str) and version:
            current_version = version
        else:
            current_version = self._model_state.version

        posterior_mean = self._coerce_float(payload.get("posterior_mean"))
        posterior_std = self._coerce_float(payload.get("posterior_std"))
        if posterior_mean is None and isinstance(payload.get("summary"), Mapping):
            posterior_mean = self._coerce_float(payload["summary"].get("mean"))
        if posterior_std is None and isinstance(payload.get("summary"), Mapping):
            variance = self._coerce_float(payload["summary"].get("variance"))
            if variance is not None:
                posterior_std = math.sqrt(max(variance, 0.0))
        policy_score = self._coerce_float(payload.get("policy_score"))
        metrics = payload.get("metrics")
        if not isinstance(metrics, Mapping):
            metrics = payload.get("summary")
        if not isinstance(metrics, Mapping):
            metrics = self._model_state.metrics

        state = self._model_state
        state = replace(
            state,
            version=current_version,
            posterior_mean=posterior_mean if posterior_mean is not None else state.posterior_mean,
            posterior_std=posterior_std if posterior_std is not None else state.posterior_std,
            policy_score=policy_score if policy_score is not None else state.policy_score,
            metrics=metrics if isinstance(metrics, Mapping) else state.metrics,
        )
        self._model_state = state

    @staticmethod
    def _coerce_float(value: object) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

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

        if not config.enable_news_features or config.strategy is FusionStrategy.LATE:
            news_probability = base_probability
            news_confidence = 0.0
        else:
            news_probability, news_confidence = self._apply_early_mid_fusion(
                timestamp, base_probability, config
            )

        if config.enable_news_features and config.strategy is FusionStrategy.LATE:
            news_probability, news_confidence = self._apply_late_adjustment(
                timestamp, base_probability, config
            )

        return news_probability, news_confidence

    def _apply_early_mid_fusion(
        self,
        timestamp: Optional[int],
        base_probability: float,
        config: FusionConfig,
    ) -> tuple[float, float]:
        aggregated = self._aggregate_news(timestamp, config)
        if aggregated is None:
            return base_probability, 0.0

        news_probability = 0.5 * (aggregated["sentiment"] + 1.0)
        weight = config.resolved_news_weight()
        if config.strategy is FusionStrategy.MID:
            weight = min(1.0, weight * aggregated["confidence"])
        fused = base_probability * (1.0 - weight) + news_probability * weight
        fused = max(0.0, min(1.0, fused))
        return fused, aggregated["confidence"]

    def _apply_late_adjustment(
        self,
        timestamp: Optional[int],
        base_probability: float,
        config: FusionConfig,
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
        self, timestamp: Optional[int], config: FusionConfig
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
