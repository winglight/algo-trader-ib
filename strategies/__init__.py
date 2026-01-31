"""Strategy implementations and lazy import helpers."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "BRRStrategy",
    "BuyTheDipStrategy",
    "CandleSubscriptionStrategy",
    "DOMSubscriptionStrategy",
    "DynamicORBStrategy",
    "FiveMinuteMomentumStrategy",
    "MeanReversionStrategy",
    "OpeningRangeBreakoutStrategy",
    "PredictiveStrategy",
    "ScreenerStrategy",
]



_MODULE_MAP = {
    "BRRStrategy": "src.strategies.brr_strategy",
    "BuyTheDipStrategy": "src.strategies.buy_the_dip",
    "CandleSubscriptionStrategy": "src.strategies.candle",
    "DOMSubscriptionStrategy": "src.strategies.dom",
    "DynamicORBStrategy": "src.strategies.orb_dynamic",
    "FiveMinuteMomentumStrategy": "src.strategies.five_minute_momentum",
    "MeanReversionStrategy": "src.strategies.mean_reversion_strategy",
    "OpeningRangeBreakoutStrategy": "src.strategies.orb_fvg",
    "PredictiveStrategy": "src.strategies.predictive_strategy",
    "ScreenerStrategy": "src.strategies.screener",
}



def __getattr__(name: str) -> Any:  # pragma: no cover - exercised indirectly
    if name == "base":
        return import_module("src.strategy.base")
    if name not in _MODULE_MAP:
        raise AttributeError(f"module 'src.strategies' has no attribute {name!r}")
    module = import_module(_MODULE_MAP[name])
    try:
        return getattr(module, name)
    except AttributeError as exc:  # pragma: no cover - defensive guard
        raise AttributeError(f"{name!r} not exported by {_MODULE_MAP[name]}") from exc


def __dir__() -> list[str]:  # pragma: no cover - introspection helper
    return sorted(__all__)
