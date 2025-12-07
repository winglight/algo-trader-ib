"""Minimal DOMSubscriptionStrategy to satisfy imports at runtime."""

from __future__ import annotations

from dataclasses import dataclass

from src.strategy.base import BaseStrategy


@dataclass
class DOMSubscriptionStrategy(BaseStrategy):
    strategy_type: str = "DOMSubscriptionStrategy"
    data_feed_mode: str = "dom"
    symbol: str = ""
    depth_levels: int = 5

    parameter_definitions = {
        "symbol": {"type": "str", "label": "Symbol"},
        "depth_levels": {"type": "int", "default": 5, "min": 1, "label": "Depth Levels"},
    }

    def __post_init__(self) -> None:
        super().__post_init__()
        self.set_parameter_definitions(self.parameter_definitions)

