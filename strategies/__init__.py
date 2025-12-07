"""Public strategies shim package.

Provides minimal classes required by backend and strategy services
when mounting `./strategies` into `/app/src/strategies`.
"""

from __future__ import annotations

try:
    from .templates import StrategyTemplate  # type: ignore[F401]
except Exception:
    # Fallback minimal base to keep imports working
    from src.strategy.base import BaseStrategy as StrategyTemplate  # type: ignore[type-var]

# Export CandleSubscriptionStrategy (minimal stub if full module missing)
try:
    from .candle import CandleSubscriptionStrategy  # type: ignore[F401]
except Exception:
    from dataclasses import dataclass
    from src.strategy.base import BaseStrategy

    @dataclass
    class CandleSubscriptionStrategy(BaseStrategy):  # type: ignore[misc]
        strategy_type: str = "CandleSubscriptionStrategy"
        is_kline_strategy: bool = True
        data_feed_mode: str = "kline"
        symbol: str = ""
        interval: str = "1m"

# Export DOMSubscriptionStrategy (minimal stub if full module missing)
try:
    from .dom import DOMSubscriptionStrategy  # type: ignore[F401]
except Exception:
    from dataclasses import dataclass
    from src.strategy.base import BaseStrategy

    @dataclass
    class DOMSubscriptionStrategy(BaseStrategy):  # type: ignore[misc]
        strategy_type: str = "DOMSubscriptionStrategy"
        data_feed_mode: str = "dom"
        symbol: str = ""
        depth_levels: int = 5

# Export predictive repository/state used by live_strategy_runner
try:
    from .predictive_strategy import (  # type: ignore[F401]
        PredictiveModelRepository,
        PredictiveModelState,
    )
except Exception:
    # Leave undefined; runtime will not rely on these if strategy not used
    pass

