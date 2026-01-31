"""Built-in strategy templates leveraging streaming base classes."""

from __future__ import annotations

import asyncio
import inspect
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Sequence
from collections import deque
import time
from src.orders.models import OrderSide

from src.strategy.base import BaseStrategy

__all__ = [
    "StrategySignal",
    "StrategyTemplate",
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
        if not hasattr(self, "_signals"):
            self._signals = deque()
        if not hasattr(self, "_last_signal_monotonic"):
            self._last_signal_monotonic = 0.0
        if not hasattr(self, "_last_signal_wall"):
            self._last_signal_wall = None
        if not hasattr(self, "_cooldown_until"):
            self._cooldown_until = 0.0

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
                    # Cannot await in sync method within running loop
                    return 0.0
            return float(result)
        except Exception:
            return 0.0

    async def current_position(self) -> float:
        """Asynchronously retrieve the current position."""
        provider = getattr(self, "_position_provider", None)
        if provider is None:
            return 0.0
        try:
            result = provider(self.symbol)
            if inspect.isawaitable(result):
                return float(await result)
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

    def _monotonic_now(self) -> float:
        try:
            return float(time.monotonic())
        except Exception:
            return float(time.time())

    def _wall_clock_now(self) -> float:
        return float(time.time())

    def _risk_guard_accept_signal(self, side: str, *, is_exit_like: bool) -> bool:
        now_monotonic = self._monotonic_now()
        side_token = str(side).strip().upper()
        try:
            current_position = float(getattr(self, "_position", 0.0))
        except Exception:
            current_position = 0.0
        if current_position > 0 and side_token == OrderSide.SELL.value:
            is_exit_like = True
        elif current_position < 0 and side_token == OrderSide.BUY.value:
            is_exit_like = True
        if getattr(self, "breaker_tripped", False) and not is_exit_like:
            return False
        cooldown = max(0.0, float(getattr(self, "cooldown_seconds", 0.0)))
        if cooldown > 0.0 and now_monotonic < getattr(self, "_cooldown_until", 0.0) and not is_exit_like:
            return False
        frequency = max(0.0, float(getattr(self, "signal_frequency_seconds", 0.0)))
        last_wall = getattr(self, "_last_signal_wall", None)
        if frequency > 0.0 and last_wall is not None and not is_exit_like:
            now_wall = self._wall_clock_now()
            elapsed = now_wall - float(last_wall)
            if elapsed < frequency:
                return False
        return True

    def produce_signal(self, signal: StrategySignal, *, is_exit_like: bool = False) -> bool:
        accepted = self._risk_guard_accept_signal(signal.side, is_exit_like=is_exit_like)
        if not accepted:
            return False
        self._signals.append(signal)
        now_monotonic = self._monotonic_now()
        self._last_signal_monotonic = now_monotonic
        self._last_signal_wall = self._wall_clock_now()
        cooldown = max(0.0, float(getattr(self, "cooldown_seconds", 0.0)))
        self._cooldown_until = max(now_monotonic, getattr(self, "_cooldown_until", 0.0)) + cooldown
        return True

    async def generate_orders(self) -> Sequence[Mapping[str, Any]]:
        if not getattr(self, "_signals", None):
            return []
        
        orders: list[Mapping[str, Any]] = []
        current_position = await self.current_position()
        
        # Virtual position tracker to prevent duplicate orders in the same batch
        virtual_position = float(current_position)
        has_opened_in_batch = False

        while self._signals:
            signal = self._signals.popleft()
            payload = signal.as_dict()
            metadata = payload.get("metadata")
            
            # 1. Handle Close Position Signals
            if isinstance(metadata, Mapping) and metadata.get("close_position"):
                if abs(virtual_position) <= 1e-9:
                    # Try to resolve fallback if strictly zero, just in case
                    fallback_position, _ = self._resolve_position_state(
                        use_strategy_position=False
                    )
                    virtual_position = fallback_position

                if abs(virtual_position) <= 1e-9:
                    # Still zero? Check if the signal carried a quantity hint to clear
                    fallback_quantity = self._coerce_float(payload.get("quantity") or 0.0)
                    side_value = str(payload.get("side", "")).strip().upper()
                    if fallback_quantity and side_value in {
                        OrderSide.BUY.value,
                        OrderSide.SELL.value,
                    }:
                        # Assume the intention was to close this specific quantity
                        # effectively setting virtual pos to allow the close
                        virtual_position = (
                            fallback_quantity
                            if side_value == OrderSide.BUY.value
                            else -fallback_quantity
                        )

                if abs(virtual_position) <= 1e-9:
                    # Already closed or no position to close
                    continue

                # Generate closing order
                payload["side"] = (
                    OrderSide.SELL.value if virtual_position > 0 else OrderSide.BUY.value
                )
                payload["quantity"] = abs(float(virtual_position))
                orders.append(payload)
                
                # Update state
                virtual_position = 0.0
                continue

            # 2. Handle Normal Entry/Exit Signals
            raw_quantity = float(payload.get("quantity", 0))
            if raw_quantity <= 0:
                continue

            side_value = str(payload.get("side", "")).strip().upper()
            is_buy = side_value == OrderSide.BUY.value
            signed_change = raw_quantity if is_buy else -raw_quantity
            
            # Determine if this signal increases the position exposure (Open/Add)
            # or reduces it (Close/Reduce).
            # Note: Crossing 0 (flipping) counts as increasing after the flip.
            is_increasing = False
            
            if virtual_position == 0:
                is_increasing = True
            elif virtual_position > 0 and is_buy:
                is_increasing = True
            elif virtual_position < 0 and not is_buy:
                is_increasing = True
            
            # Guard against duplicate entries in the same batch
            if is_increasing:
                if has_opened_in_batch:
                    # Determine if we should allow multiple entries. 
                    # Default to False for safety unless explicitly configured.
                    # For now, we log and skip to prevent the "4 duplicate orders" bug.
                    # self.logger.warning("Skipping duplicate entry signal in batch: %s", payload)
                    continue
                has_opened_in_batch = True

            # Calculate Exit Targets (Stop Loss / Take Profit)
            # Use the virtual position + signal to estimate the post-trade state
            # for exit calculation purposes, or just the signal quantity.
            exit_targets = self.evaluate_exit_signal(
                position=signed_change,
                entry_price=self._coerce_float(
                    payload["metadata"].get("entry_price_hint")
                ),
                account_equity=getattr(self, "account_equity", None),
                is_dom=getattr(self, "_is_dom_strategy", False),
            )
            
            if exit_targets is not None:
                payload["metadata"]["exit_mode"] = exit_targets.mode.value
                if exit_targets.stop_loss is not None:
                    payload["metadata"]["evaluated_stop_loss"] = float(
                        exit_targets.stop_loss
                    )
                if exit_targets.take_profit is not None:
                    payload["metadata"]["evaluated_take_profit"] = float(
                        exit_targets.take_profit
                    )
            
            orders.append(payload)
            virtual_position += signed_change

        return orders

def __getattr__(name: str) -> Any:
    if name == "DomStructureStrategy":
        from .dom_structure_strategy import DomStructureStrategy as cls
        return cls
    if name == "DomMomentumStrategy":
        from .dom_momentum_strategy import DomMomentumStrategy as cls
        return cls
    if name == "MeanReversionStrategy":
        from .mean_reversion_strategy import MeanReversionStrategy as cls
        return cls
    raise AttributeError(f"module 'src.strategies.templates' has no attribute {name!r}")
