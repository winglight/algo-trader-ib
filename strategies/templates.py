"""Built-in strategy templates leveraging streaming base classes."""

from __future__ import annotations

import asyncio
import inspect
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping

from src.strategy.base import BaseStrategy

__all__ = [
    "StrategySignal",
    "StrategyTemplate",
    "DomStructureStrategy",
    "DomMomentumStrategy",
    "MeanReversionStrategy",
]


_CONTRACT_METADATA_KEYS: tuple[str, ...] = (
    "subscription_id",
    "symbol",
    "sec_type",
    "exchange",
    "currency",
    "local_symbol",
    "trading_class",
    "primary_exchange",
)

_DEFAULT_REGIME_CONDITION_OVERRIDES: Mapping[str, Mapping[str, float | int]] = {
    "calm": {
        "required_hits": 2,
        "cooldown_seconds": 30.0,
        "default_quantity": 0.5,
    },
    "normal": {
        "required_hits": 3,
        "cooldown_seconds": 15.0,
        "default_quantity": 1.0,
    },
    "volatile": {
        "required_hits": 4,
        "cooldown_seconds": 45.0,
        "default_quantity": 0.75,
    },
}


def _extract_contract_metadata(*sources: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Merge contract metadata fields from *sources* into a flat mapping."""

    merged: Dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        stack: list[Mapping[str, Any]] = [source]
        contract = source.get("contract")
        if isinstance(contract, Mapping):
            stack.append(contract)
        for candidate in stack:
            for key in _CONTRACT_METADATA_KEYS:
                if key in merged:
                    continue
                value = candidate.get(key)
                if value is None:
                    continue
                if isinstance(value, str):
                    value = value.strip()
                    if not value:
                        continue
                merged[key] = value
    return merged


@dataclass(slots=True)
class StrategySignal:
    """Simple representation of an order signal produced by a strategy."""

    side: str
    quantity: int
    reason: str
    metadata: Dict[str, Any]

    def __post_init__(self) -> None:
        raw_quantity = self.quantity
        fractional = 0.0
        integer_quantity = 0
        try:
            numeric = float(raw_quantity)
        except (TypeError, ValueError):
            numeric = 0.0
        if not math.isfinite(numeric):
            numeric = 0.0
        if numeric >= 0.0:
            integer_quantity = int(math.floor(numeric))
            fractional = numeric - integer_quantity
        else:
            integer_quantity = int(math.ceil(numeric))
            fractional = integer_quantity - numeric
        if fractional < 0.0:
            fractional = 0.0
        if 0.0 < fractional < 1e-09:
            fractional = 0.0

        try:
            metadata_source = self.metadata or {}
        except Exception:  # pragma: no cover - defensive
            metadata_source = {}
        try:
            metadata: Dict[str, Any] = dict(metadata_source)
        except Exception:  # pragma: no cover - defensive
            metadata = {}

        if fractional > 0.0:
            metadata.setdefault("quantity_fractional_discarded", fractional)

        object.__setattr__(self, "quantity", int(integer_quantity))
        object.__setattr__(self, "metadata", metadata)

    def as_dict(self) -> Dict[str, Any]:
        metadata = dict(self.metadata)
        payload = dict(metadata)
        payload.update({"side": self.side, "quantity": self.quantity, "reason": self.reason})
        payload["metadata"] = metadata
        return payload

    @classmethod
    def from_probability(
        cls,
        side: str,
        quantity: float,
        probability: float,
        *,
        reason: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> "StrategySignal":
        payload = dict(metadata or {})
        payload.setdefault("probability", float(probability))
        return cls(side=side, quantity=quantity, reason=reason, metadata=payload)


class StrategyTemplate(BaseStrategy):
    """Helper base class that wires default parameters for templates."""

    strategy_type: str = ""
    default_parameters: Mapping[str, Any] = {}
    parameter_definitions: Mapping[str, Mapping[str, Any]] = {}

    def __post_init__(self) -> None:
        super().__post_init__()
        if not hasattr(self, "_position_provider"):
            self._position_provider: Callable[[str], float] | None = None
        self.set_parameter_definitions(self.parameter_definitions)
        for name, default in self.default_parameters.items():
            if not hasattr(self, name):
                setattr(self, name, default)
                continue
            current = getattr(self, name)
            if isinstance(default, str) and not str(current or "").strip():
                setattr(self, name, default)
            elif current is None:
                setattr(self, name, default)
        if hasattr(self, "order_quantity"):
            normalised_quantity = self._normalise_order_quantity(
                getattr(self, "order_quantity")
            )
            setattr(self, "order_quantity", normalised_quantity)

    def describe(self) -> Dict[str, Any]:  # type: ignore[override]
        base = super().describe()
        base["strategy_type"] = self.strategy_type or self.__class__.__name__
        base["parameters"] = self.describe_parameters()
        return base

    def set_dependencies(
        self,
        *,
        position_provider: Callable[[str], float] | None = None,
        **dependencies: Any,
    ) -> None:
        super().set_dependencies(**dependencies)
        if position_provider is not None:
            try:
                self._dependencies["position_provider"] = position_provider
            except Exception:  # pragma: no cover - defensive
                pass
            self._position_provider = position_provider

    def _current_position(self) -> float:
        provider = getattr(self, "_position_provider", None)
        if provider is None:
            return 0.0
        try:
            result = provider(self.symbol)
            if inspect.isawaitable(result):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    return float(asyncio.run(result))
                else:
                    asyncio.create_task(result)
                    return 0.0
            return float(result)
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    def _normalise_parameter_value(self, name: str, value: Any) -> Any:
        if name == "symbol":
            if value is None:
                return ""
            if isinstance(value, str):
                return value.strip().upper()
            return str(value).upper()
        if name == "order_quantity":
            return self._normalise_order_quantity(value)
        return super()._normalise_parameter_value(name, value)

    @staticmethod
    def _coerce_order_quantity_components(value: Any) -> tuple[int, float]:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0, 0.0
        if not math.isfinite(numeric):
            return 0, 0.0
        quantity = int(numeric)
        if quantity < 0:
            return 0, 0.0
        discarded = numeric - quantity
        if discarded < 0.0:
            discarded = 0.0
        if 0.0 < discarded < 1e-09:
            discarded = 0.0
        return quantity, discarded

    def _normalise_order_quantity(self, value: Any) -> int:
        quantity, _ = self._coerce_order_quantity_components(value)
        return quantity

    def _resolve_order_quantity(self, value: Any | None = None) -> tuple[int, float]:
        source = value if value is not None else getattr(self, "order_quantity", 0)
        quantity, discarded = self._coerce_order_quantity_components(source)
        return quantity, discarded

# Re-export built-in strategy classes after definitions to avoid circular imports
from .dom_structure_strategy import DomStructureStrategy  # noqa: F401
from .dom_momentum_strategy import DomMomentumStrategy  # noqa: F401
from .mean_reversion_strategy import MeanReversionStrategy  # noqa: F401

