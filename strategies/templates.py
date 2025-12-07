"""Minimal StrategyTemplate for public runtime.

Enough to satisfy imports and parameter description calls.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping

from src.strategy.base import BaseStrategy


@dataclass
class StrategySignal:
    side: str
    quantity: int
    reason: str
    metadata: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        payload = dict(self.metadata or {})
        payload.update({"side": self.side, "quantity": int(self.quantity), "reason": self.reason})
        return payload


class StrategyTemplate(BaseStrategy):
    strategy_type: str = ""
    parameter_definitions: Mapping[str, Mapping[str, Any]] = {
        "symbol": {
            "type": "str",
            "label": "Symbol",
            "description": "Primary symbol",
        },
        "order_quantity": {
            "type": "int",
            "default": 1,
            "min": 0,
            "label": "Order Quantity",
        },
    }

    def __post_init__(self) -> None:
        super().__post_init__()
        self.set_parameter_definitions(self.parameter_definitions)

    def _normalise_parameter_value(self, name: str, value: Any) -> Any:
        if name == "symbol":
            if value is None:
                return ""
            return str(value).strip().upper()
        if name == "order_quantity":
            try:
                v = float(value)
            except (TypeError, ValueError):
                v = 1.0
            if not math.isfinite(v) or v < 0:
                v = 0.0
            return int(v)
        return super()._normalise_parameter_value(name, value)

    def set_dependencies(self, **dependencies: Any) -> None:
        super().set_dependencies(**dependencies)

