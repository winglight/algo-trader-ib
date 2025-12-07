"""Minimal CandleSubscriptionStrategy to satisfy imports at runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from src.strategy.base import BaseStrategy


@dataclass
class CandleSubscriptionStrategy(BaseStrategy):
    strategy_type: str = "CandleSubscriptionStrategy"
    is_kline_strategy: bool = True
    data_feed_mode: str = "kline"
    symbol: str = ""
    interval: str = "1m"

    parameter_definitions = {
        "symbol": {"type": "str", "label": "Symbol"},
        "interval": {"type": "str", "default": "1m", "label": "Interval"},
    }

    def __post_init__(self) -> None:
        super().__post_init__()
        self.set_parameter_definitions(self.parameter_definitions)

