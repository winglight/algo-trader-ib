"""Pair trading strategy focused on MNQ execution against a secondary symbol."""
from __future__ import annotations

import asyncio
from collections import deque
from statistics import mean
from typing import Any, Deque, Mapping

from src.common.market_data.aggregation import normalize_interval_token
from src.common.symbols import (
    build_symbol_aliases,
    normalize_symbol_token,
    symbols_match,
)
from src.common.orders import OrderSide
from src.strategies.templates import StrategySignal, StrategyTemplate


class PairTradingStrategy(StrategyTemplate):
    """Pair trading strategy using relative 1m bar return differences."""

    _PHASE_SUBSCRIPTION = "subscription"
    _PHASE_SIGNAL_GENERATION = "signal_generation"
    _PHASE_ORDER_EXECUTION = "order_execution"

    name: str = "Pair Trading Strategy"
    description: str = (
        "Compares 1m bar return spreads between MNQ and a secondary symbol, "
        "issuing MNQ-only mean reversion orders."
    )
    strategy_type = "pair_trading"
    data_feed_mode: str = "kline"
    is_kline_strategy: bool = True
    required_market_data_streams: tuple[str, ...] = ("bar",)
    data_layer_channel: str = "market.bar"

    symbol: str = "MNQ"
    symbol2: str = "NVDA"
    interval: str = "1m"
    interval2: str = "1m"
    intervals: list[str] = []
    intervals2: list[str] = []

    cooldown_seconds: float = 30.0
    signal_frequency_seconds: float = 60.0

    parameter_definitions = {
        "symbol": {
            "type": "str",
            "default": "MNQ",
            "description": "Primary symbol used for orders.",
        },
        "symbol2": {
            "type": "str",
            "default": "NVDA",
            "description": "Secondary symbol used for spread comparison.",
        },
        "interval": {
            "type": "str",
            "default": "1m",
            "description": "Primary bar interval for MNQ.",
        },
        "intervals": {
            "type": "list",
            "allow_null": True,
            "default": None,
            "description": "Optional list of intervals for the primary symbol.",
        },
        "interval2": {
            "type": "str",
            "default": "1m",
            "description": "Secondary bar interval for spread calculations.",
        },
        "intervals2": {
            "type": "list",
            "allow_null": True,
            "default": None,
            "description": "Optional list of intervals for the secondary symbol.",
        },
        "spread_threshold": {
            "type": "float",
            "default": 0.003,
            "min": 0.0,
            "description": "Deviation threshold for spread-based entries.",
        },
        "position_size": {
            "type": "int",
            "default": 1,
            "min": 1,
            "description": "Order quantity for MNQ entries.",
        },
        "lookback": {
            "type": "int",
            "default": 20,
            "min": 2,
            "description": "Rolling window for spread mean estimation.",
        },
    }
    default_parameters = {
        "symbol": "MNQ",
        "symbol2": "NVDA",
        "interval": "1m",
        "interval2": "1m",
        "spread_threshold": 0.003,
        "position_size": 1,
        "lookback": 20,
    }

    @staticmethod
    def _normalise_interval_list(value: Any, fallback: str) -> list[str]:
        if value is None:
            return [fallback]
        candidates: list[Any]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return [fallback]
            candidates = [part.strip() for part in text.split(",") if part.strip()]
        elif isinstance(value, (list, tuple, set)):
            candidates = list(value)
        else:
            candidates = [value]

        normalised: list[str] = []
        for candidate in candidates:
            token = normalize_interval_token(candidate)
            if token and token not in normalised:
                normalised.append(token)
        return normalised or [fallback]

    def __post_init__(self) -> None:
        super().__post_init__()
        self.symbol = normalize_symbol_token(self.symbol or "MNQ") or "MNQ"
        self.symbol2 = normalize_symbol_token(self.symbol2 or "NVDA") or "NVDA"
        self._primary_aliases = set(build_symbol_aliases(self.symbol))
        self._secondary_aliases = set(build_symbol_aliases(self.symbol2))
        if not getattr(self, "interval", None):
            self.interval = "1m"
        if not getattr(self, "interval2", None):
            self.interval2 = "1m"
        self.interval = normalize_interval_token(self.interval) or "1m"
        self.interval2 = normalize_interval_token(self.interval2) or "1m"
        self.intervals = self._normalise_interval_list(
            getattr(self, "intervals", None),
            self.interval,
        )
        self.intervals2 = self._normalise_interval_list(
            getattr(self, "intervals2", None),
            self.interval2,
        )

        lookback = int(getattr(self, "lookback", 20) or 20)
        if lookback < 2:
            lookback = 2
        self.lookback = lookback

        self._latest_bars: dict[str, dict[str, Any]] = {}
        self._spread_history: Deque[float] = deque(maxlen=self.lookback)
        self._position: float = 0.0
        self._pubsub: Any | None = getattr(self, "_pubsub", None)
        self._dispatch_event: Any | None = getattr(self, "_dispatch_event", None)
        self._listener_task: asyncio.Task[None] | None = None
        self._telemetry_set_phase_status(
            self._PHASE_SUBSCRIPTION,
            status="running",
            status_code="monitoring",
            status_reason="Awaiting market data subscription",
            status_details=self._telemetry_details(),
        )
        self._telemetry_set_phase_status(
            self._PHASE_SIGNAL_GENERATION,
            status="idle",
            status_code="ready",
            status_reason="Waiting for spread history",
            status_details=self._telemetry_details(),
        )
        self._telemetry_set_phase_status(
            self._PHASE_ORDER_EXECUTION,
            status="idle",
            status_code="ready",
            status_reason="No order signals yet",
            status_details=self._telemetry_details(),
        )

    # ------------------------------------------------------------------
    def _normalise_parameter_value(self, name: str, value: Any) -> Any:
        if name == "symbol":
            return normalize_symbol_token(value) or self.symbol or "MNQ"
        if name == "symbol2":
            return normalize_symbol_token(value) or self.symbol2 or "NVDA"
        if name == "interval":
            return normalize_interval_token(value) or self.interval or "1m"
        if name == "interval2":
            return normalize_interval_token(value) or self.interval2 or "1m"
        if name == "intervals":
            fallback = normalize_interval_token(getattr(self, "interval", None)) or "1m"
            return self._normalise_interval_list(value, fallback)
        if name == "intervals2":
            fallback = normalize_interval_token(getattr(self, "interval2", None)) or "1m"
            return self._normalise_interval_list(value, fallback)
        if name == "spread_threshold":
            return max(0.0, float(value))
        if name == "position_size":
            return max(1, int(value))
        if name == "lookback":
            return max(2, int(value))
        return super()._normalise_parameter_value(name, value)

    # ------------------------------------------------------------------
    def apply_parameter_updates(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        if not updates:
            return {}

        previous_interval = self.interval
        previous_interval2 = self.interval2
        previous_intervals = list(self.intervals or [self.interval])
        previous_intervals2 = list(self.intervals2 or [self.interval2])
        applied = super().apply_parameter_updates(updates)

        if "interval" in applied and "intervals" not in applied:
            if previous_intervals == [previous_interval]:
                self.intervals = [self.interval]
                applied["intervals"] = list(self.intervals)
        if "interval2" in applied and "intervals2" not in applied:
            if previous_intervals2 == [previous_interval2]:
                self.intervals2 = [self.interval2]
                applied["intervals2"] = list(self.intervals2)
        return applied

    def _telemetry(self) -> Any | None:
        return getattr(self, "runtime_telemetry", None)

    def set_dependencies(
        self,
        *,
        pubsub: Any | None = None,
        event_dispatcher: Any | None = None,
        runtime_telemetry: Any | None = None,
        **dependencies: Any,
    ) -> None:
        super().set_dependencies(**dependencies)
        if pubsub is not None:
            self._pubsub = pubsub
        if event_dispatcher is not None:
            async def _dispatch(payload: Mapping[str, Any]) -> None:
                result = event_dispatcher(self.name, payload)
                if asyncio.iscoroutine(result):
                    await result
            self._dispatch_event = _dispatch
        if runtime_telemetry is not None:
            self.runtime_telemetry = runtime_telemetry

    async def on_start(self) -> None:
        listener = self._listener_task
        if listener is not None and not listener.done():
            return
        if self._pubsub is None or self._dispatch_event is None:
            self._emit_pair_event(
                "pair.subscription.listener_unavailable",
                level="WARNING",
                tone="warning",
                details={
                    "has_pubsub": self._pubsub is not None,
                    "has_dispatcher": self._dispatch_event is not None,
                },
            )
            self._telemetry_set_phase_status(
                self._PHASE_SUBSCRIPTION,
                status="failed",
                status_code="listener_unavailable",
                status_reason="Pair bar listener dependencies unavailable",
            )
            return
        self._listener_task = asyncio.create_task(
            self._run_bar_listener(),
            name=f"{self.name}-pair-bar-listener",
        )

    async def on_stop(self) -> None:
        listener = self._listener_task
        self._listener_task = None
        if listener is None:
            return
        listener.cancel()
        try:
            await listener
        except asyncio.CancelledError:
            pass

    def _telemetry_strategy_id(self) -> Any:
        identifier = getattr(self, "identifier", None)
        if isinstance(identifier, str) and identifier.strip():
            return identifier
        return self.name

    def _resolve_channel(self, symbol: str, interval: str | None) -> str:
        base_channel = (self.data_layer_channel or "").strip() or "market.bar"
        interval_token = normalize_interval_token(interval or "1m") or "1m"
        return f"{base_channel}-{symbol}-{interval_token}"

    def _subscription_channels(self) -> list[str]:
        channels: list[str] = []
        primary_intervals = self.intervals or [self.interval]
        secondary_intervals = self.intervals2 or [self.interval2]
        for interval in primary_intervals:
            channels.append(self._resolve_channel(self.symbol, interval))
        for interval in secondary_intervals:
            channel = self._resolve_channel(self.symbol2, interval)
            if channel not in channels:
                channels.append(channel)
        return channels

    def _normalize_listener_payload(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        if not isinstance(payload, Mapping):
            return None

        raw_bar = payload.get("bar")
        if isinstance(raw_bar, Mapping):
            event = dict(payload)
            bar_payload = dict(raw_bar)
            symbol = bar_payload.get("symbol") or event.get("symbol")
            interval_value = (
                bar_payload.get("interval")
                or bar_payload.get("timeframe")
                or bar_payload.get("bar_timeframe")
                or event.get("interval")
                or event.get("timeframe")
                or event.get("bar_timeframe")
            )
            if symbol is not None:
                event["symbol"] = symbol
                bar_payload.setdefault("symbol", symbol)
            normalized_interval = normalize_interval_token(interval_value)
            if normalized_interval:
                event["interval"] = normalized_interval
                bar_payload.setdefault("interval", normalized_interval)
            event["bar"] = bar_payload
            event.setdefault("type", "bar")
            return event

        raw_bars = payload.get("bars")
        latest_bar: Mapping[str, Any] | None = None
        if isinstance(raw_bars, (list, tuple)):
            for candidate in reversed(raw_bars):
                if isinstance(candidate, Mapping):
                    latest_bar = candidate
                    break
        if latest_bar is not None:
            event = dict(payload)
            event.pop("bars", None)
            bar_payload = dict(latest_bar)
            symbol = bar_payload.get("symbol") or event.get("symbol")
            interval_value = (
                bar_payload.get("interval")
                or bar_payload.get("timeframe")
                or bar_payload.get("bar_timeframe")
                or event.get("interval")
                or event.get("timeframe")
                or event.get("bar_timeframe")
            )
            if symbol is not None:
                event["symbol"] = symbol
                bar_payload.setdefault("symbol", symbol)
            normalized_interval = normalize_interval_token(interval_value)
            if normalized_interval:
                event["interval"] = normalized_interval
                bar_payload.setdefault("interval", normalized_interval)
            event["bar"] = bar_payload
            event["is_snapshot"] = bool(payload.get("is_snapshot"))
            event["type"] = "bar"
            return event

        if {"open", "high", "low", "close"}.issubset(set(payload.keys())):
            event = dict(payload)
            interval_value = (
                event.get("interval")
                or event.get("timeframe")
                or event.get("bar_timeframe")
            )
            normalized_interval = normalize_interval_token(interval_value)
            if normalized_interval:
                event["interval"] = normalized_interval
            event.setdefault("type", "bar")
            return event

        return None

    async def _run_bar_listener(self) -> None:
        if self._pubsub is None or self._dispatch_event is None:
            return
        channels = self._subscription_channels()
        if not channels:
            return
        self._telemetry_set_phase_status(
            self._PHASE_SUBSCRIPTION,
            status="connected",
            status_code="listening",
            status_reason="Listening for pair bar events",
            status_details={"channels": channels},
        )
        self._telemetry_log_event(
            "Pair trading subscription configured",
            details={"channels": channels},
            phase=self._PHASE_SUBSCRIPTION,
        )
        backoff = 1.0
        try:
            while self.active:
                try:
                    stream = await self._pubsub.listen(channels)
                    async for payload in stream:
                        if not self.active:
                            break
                        if not isinstance(payload, Mapping):
                            continue
                        normalized_payload = self._normalize_listener_payload(payload)
                        if normalized_payload is None:
                            continue
                        await self._dispatch_event(normalized_payload)
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pragma: no cover - defensive runtime guard
                    self._emit_pair_event(
                        "pair.subscription.listener_failed",
                        level="WARNING",
                        tone="warning",
                        details={"error": str(exc), "channels": channels},
                    )
                    self._telemetry_set_phase_status(
                        self._PHASE_SUBSCRIPTION,
                        status="degraded",
                        status_code="listener_error",
                        status_reason="Pair bar listener crashed; retrying",
                        status_cause=str(exc),
                        status_cause_code="listener_error",
                        status_details={"channels": channels},
                    )
                if not self.active:
                    break
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)
        finally:
            if self._listener_task is not None and self._listener_task.done():
                self._listener_task = None

    def _telemetry_details(self, *, interval: str | None = None) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "symbol2": self.symbol2,
            "interval": interval or self.interval,
            "interval2": self.interval2,
        }

    def _telemetry_log_event(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        level: str = "INFO",
        tone: str = "neutral",
        phase: str | None = None,
    ) -> None:
        telemetry = self._telemetry()
        if telemetry is not None:
            try:
                telemetry.log_event(
                    self._telemetry_strategy_id(),
                    message,
                    level=level,
                    tone=tone,
                    details=details,
                    phase=phase,
                )
            except KeyError:
                return
            except Exception:  # pragma: no cover - defensive
                self.logger.exception("Failed to record telemetry event")
            return
        telemetry_log = getattr(self, "_telemetry_log", None)
        if callable(telemetry_log):
            try:
                telemetry_log(
                    message,
                    level=level,
                    tone=tone,
                    details=details,
                    deduplicate=False,
                )
            except Exception:  # pragma: no cover - defensive
                self.logger.exception("Failed to record telemetry event via compat log")

    def _telemetry_log_processing_step(
        self,
        *,
        step: str,
        metric: float | str | None = None,
        threshold: float | str | None = None,
        comparison: str | None = None,
        passed: bool | None = None,
        stage: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        telemetry = self._telemetry()
        if telemetry is not None:
            try:
                telemetry.log_processing_step(
                    self._telemetry_strategy_id(),
                    step=step,
                    metric=metric,
                    threshold=threshold,
                    comparison=comparison,
                    passed=passed,
                    stage=stage,
                    details=details,
                )
            except KeyError:
                return
            except Exception:  # pragma: no cover - defensive
                self.logger.exception("Failed to record telemetry processing step")
            return

    def _telemetry_set_phase_status(
        self,
        phase: str,
        *,
        status: str | None,
        status_code: str | None = None,
        status_reason: str | None = None,
        status_cause: str | None = None,
        status_cause_code: str | None = None,
        status_details: Mapping[str, Any] | None = None,
    ) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        try:
            telemetry.set_phase_status(
                self._telemetry_strategy_id(),
                phase,
                status=status,
                status_code=status_code,
                status_reason=status_reason,
                status_cause=status_cause,
                status_cause_code=status_cause_code,
                status_details=status_details,
            )
        except KeyError:
            return
        except Exception:  # pragma: no cover - defensive
            self.logger.exception("Failed to set telemetry phase status")

    def _emit_pair_event(
        self,
        event: str,
        *,
        level: str = "INFO",
        tone: str = "neutral",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        payload = dict(details or {})
        payload.setdefault("event", event)
        self._telemetry_log_event(event, details=payload, level=level, tone=tone)
        log_level = str(level or "INFO").strip().upper()
        logger_method = getattr(self.logger, log_level.lower(), self.logger.info)
        try:
            logger_method(
                event,
                extra={
                    "event": event,
                    "pair_symbol": self.symbol,
                    "pair_symbol_secondary": self.symbol2,
                    **payload,
                },
            )
        except Exception:  # pragma: no cover - defensive
            self.logger.exception("Failed to emit pair strategy event")

    def _resolve_event_symbol(self, raw_symbol: str) -> tuple[str | None, str | None]:
        normalized = normalize_symbol_token(raw_symbol)
        if not normalized:
            return None, None
        if normalized in self._primary_aliases or symbols_match(normalized, self.symbol):
            return self.symbol, "primary"
        if normalized in self._secondary_aliases or symbols_match(normalized, self.symbol2):
            return self.symbol2, "secondary"
        return normalized, None

    def _telemetry_update_phase_metrics(
        self,
        phase: str,
        updates: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        telemetry = self._telemetry()
        if telemetry is None:
            return
        combined: dict[str, Any] = {}
        if updates:
            combined.update(dict(updates))
        if kwargs:
            combined.update(kwargs)
        if not combined:
            return
        try:
            telemetry.update_phase_metrics(
                self._telemetry_strategy_id(), phase, combined
            )
        except KeyError:
            return
        except Exception:  # pragma: no cover - defensive
            self.logger.exception("Failed to update telemetry metrics")

    async def on_market_event(self, event: Mapping[str, Any]) -> None:
        bar, raw_symbol, interval = self._extract_bar_payload(event)
        if bar is None:
            self._emit_pair_event(
                "pair.market_event.ignored",
                level="WARNING",
                tone="warning",
                details={"reason": "unknown_event_type"},
            )
            return

        if raw_symbol is None:
            self._emit_pair_event(
                "pair.market_event.ignored",
                level="WARNING",
                tone="warning",
                details={"reason": "missing_symbol"},
            )
            return
        symbol, leg = self._resolve_event_symbol(raw_symbol)
        self._emit_pair_event(
            "pair.market_event.received",
            details={
                "raw_symbol": raw_symbol,
                "normalized_symbol": symbol,
                "matched_leg": leg,
                "interval": interval,
            },
        )
        if symbol is None or leg is None:
            self._emit_pair_event(
                "pair.market_event.ignored",
                level="WARNING",
                tone="warning",
                details={
                    "reason": "symbol_mismatch",
                    "raw_symbol": raw_symbol,
                    "normalized_symbol": symbol,
                    "interval": interval,
                },
            )
            return
        if not self._interval_allowed(symbol, interval):
            self._emit_pair_event(
                "pair.market_event.ignored",
                level="WARNING",
                tone="warning",
                details={
                    "reason": "interval_disallowed",
                    "symbol": symbol,
                    "interval": interval,
                    "allowed_primary": list(self.intervals or [self.interval]),
                    "allowed_secondary": list(self.intervals2 or [self.interval2]),
                },
            )
            return

        open_price, close_price = self._extract_bar_prices(bar)
        if open_price is None or close_price is None:
            missing_price_details = self._telemetry_details(interval=interval)
            missing_price_details.update(
                {
                    "event_symbol": symbol,
                    "event_interval": interval,
                    "open": bar.get("open"),
                    "close": bar.get("close"),
                }
            )
            self._telemetry_log_processing_step(
                step="bar_price_available",
                metric="missing",
                threshold="open/close",
                comparison="prices present",
                passed=False,
                stage=self._PHASE_SIGNAL_GENERATION,
                details=missing_price_details,
            )
            self._telemetry_log_event(
                "Pair trading bar skipped due to missing price",
                details=missing_price_details,
                level="WARNING",
                tone="warning",
                phase=self._PHASE_SIGNAL_GENERATION,
            )
            self._emit_pair_event(
                "pair.market_event.ignored",
                level="WARNING",
                tone="warning",
                details={
                    "reason": "missing_price",
                    "symbol": symbol,
                    "interval": interval,
                    "open": bar.get("open"),
                    "close": bar.get("close"),
                },
            )
            return

        phase_details = self._telemetry_details(interval=interval)
        phase_details.update({"event_symbol": symbol, "event_interval": interval})
        self._telemetry_set_phase_status(
            self._PHASE_SUBSCRIPTION,
            status="running",
            status_code="event_received",
            status_reason="Market bar received",
            status_details=phase_details,
        )
        self._telemetry_set_phase_status(
            self._PHASE_SIGNAL_GENERATION,
            status="processing",
            status_code="spread_update",
            status_reason="Updating spread signals",
            status_details=phase_details,
        )

        self._latest_bars[symbol] = {
            "open": open_price,
            "close": close_price,
            "timestamp": bar.get("end") or bar.get("close_time") or bar.get("timestamp"),
            "interval": interval or bar.get("interval"),
        }
        bar_details = self._telemetry_details(interval=interval)
        bar_details.update(
            {
                "event_symbol": symbol,
                "event_interval": interval,
                "open": open_price,
                "close": close_price,
            }
        )
        self._telemetry_log_event("Pair trading bar received", details=bar_details)

        if self.symbol not in self._latest_bars or self.symbol2 not in self._latest_bars:
            waiting_details = self._telemetry_details(interval=interval)
            waiting_details.update(
                {
                    "received_symbol": symbol,
                    "has_primary": self.symbol in self._latest_bars,
                    "has_secondary": self.symbol2 in self._latest_bars,
                    "primary_last_ts": self._latest_bars.get(self.symbol, {}).get("timestamp"),
                    "secondary_last_ts": self._latest_bars.get(self.symbol2, {}).get("timestamp"),
                }
            )
            self._telemetry_log_processing_step(
                step="peer_bar_available",
                metric=int(self.symbol2 in self._latest_bars) if symbol == self.symbol else int(self.symbol in self._latest_bars),
                threshold=1,
                comparison="peer leg ready",
                passed=False,
                stage=self._PHASE_SIGNAL_GENERATION,
                details=waiting_details,
            )
            self._telemetry_log_event(
                "Pair trading waiting for peer bar",
                details=waiting_details,
                tone="warning",
                phase=self._PHASE_SIGNAL_GENERATION,
            )
            self._emit_pair_event(
                "pair.market_event.waiting_for_peer",
                level="INFO",
                tone="warning",
                details={
                    "received_symbol": symbol,
                    "interval": interval,
                    "has_primary": self.symbol in self._latest_bars,
                    "has_secondary": self.symbol2 in self._latest_bars,
                    "primary_last_ts": self._latest_bars.get(self.symbol, {}).get("timestamp"),
                    "secondary_last_ts": self._latest_bars.get(self.symbol2, {}).get("timestamp"),
                },
            )
            return

        primary = self._latest_bars[self.symbol]
        secondary = self._latest_bars[self.symbol2]
        primary_return = self._return_rate(primary)
        secondary_return = self._return_rate(secondary)
        spread = primary_return - secondary_return
        self._spread_history.append(spread)
        spread_details = self._telemetry_details(interval=interval)
        spread_details.update(
            {
                "primary_return": primary_return,
                "secondary_return": secondary_return,
                "spread": spread,
                "history_size": len(self._spread_history),
                "lookback": self.lookback,
            }
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_SIGNAL_GENERATION,
            {
                "symbol": self.symbol,
                "interval": interval or self.interval,
                "spread": spread,
                "primary_return": primary_return,
                "secondary_return": secondary_return,
                "history_size": len(self._spread_history),
                "lookback": self.lookback,
            },
        )
        self._telemetry_log_event(
            "Pair trading spread computed",
            details=spread_details,
            phase=self._PHASE_SIGNAL_GENERATION,
        )
        self._emit_pair_event(
            "pair.spread.computed",
            details={
                "interval": interval,
                "spread": spread,
                "primary_return": primary_return,
                "secondary_return": secondary_return,
                "history_size": len(self._spread_history),
                "lookback": self.lookback,
            },
        )
        self._telemetry_log_processing_step(
            step="spread_history_ready",
            metric=len(self._spread_history),
            threshold=self.lookback,
            comparison=">= lookback",
            passed=len(self._spread_history) >= min(self.lookback, 2),
            stage=self._PHASE_SIGNAL_GENERATION,
            details=spread_details,
        )

        if len(self._spread_history) < min(self.lookback, 2):
            return

        average_spread = mean(self._spread_history)
        deviation = spread - average_spread
        threshold = float(getattr(self, "spread_threshold", 0.0) or 0.0)
        deviation_details = self._telemetry_details(interval=interval)
        deviation_details.update(
            {
                "spread": spread,
                "average_spread": average_spread,
                "deviation": deviation,
                "threshold": threshold,
            }
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_SIGNAL_GENERATION,
            {
                "symbol": self.symbol,
                "interval": interval or self.interval,
                "spread": spread,
                "average_spread": average_spread,
                "deviation": deviation,
                "threshold": threshold,
            },
        )
        self._telemetry_log_processing_step(
            step="spread_deviation_check",
            metric=abs(deviation),
            threshold=threshold,
            comparison=">= threshold",
            passed=abs(deviation) >= threshold if threshold > 0 else None,
            stage=self._PHASE_SIGNAL_GENERATION,
            details=deviation_details,
        )
        self._telemetry_log_event(
            "Pair trading spread deviation evaluated",
            details=deviation_details,
            phase=self._PHASE_SIGNAL_GENERATION,
        )
        if threshold <= 0.0:
            return

        current_position = await self.current_position()
        self._position = float(current_position or 0.0)

        exit_threshold = threshold * 0.5
        reduce_threshold = threshold

        position_details = dict(deviation_details)
        position_details.update({"position": self._position})

        if abs(self._position) > 1e-6:
            self._telemetry_log_processing_step(
                step="exit_threshold_check",
                metric=abs(deviation),
                threshold=exit_threshold,
                comparison="<= exit_threshold",
                passed=abs(deviation) <= exit_threshold,
                stage=self._PHASE_SIGNAL_GENERATION,
                details=position_details,
            )
            if abs(deviation) <= exit_threshold:
                self._enqueue_exit_signal(
                    deviation=deviation,
                    spread=spread,
                    average_spread=average_spread,
                )
                return
            self._telemetry_log_processing_step(
                step="reduce_threshold_check",
                metric=abs(deviation),
                threshold=reduce_threshold,
                comparison="<= reduce_threshold",
                passed=abs(deviation) <= reduce_threshold,
                stage=self._PHASE_SIGNAL_GENERATION,
                details=position_details,
            )
            if abs(deviation) <= reduce_threshold:
                reduced = self._enqueue_reduce_signal(
                    deviation=deviation,
                    spread=spread,
                    average_spread=average_spread,
                )
                if reduced:
                    return

        if abs(self._position) <= 1e-6 and abs(deviation) >= threshold:
            self._enqueue_entry_signal(
                deviation=deviation,
                spread=spread,
                average_spread=average_spread,
            )

    def _interval_allowed(self, symbol: str, interval: str | None) -> bool:
        if symbol == self.symbol:
            return self._interval_in_list(interval, self.intervals, self.interval)
        if symbol == self.symbol2:
            return self._interval_in_list(interval, self.intervals2, self.interval2)
        return False

    @staticmethod
    def _interval_in_list(
        interval: str | None, intervals: list[str], fallback: str
    ) -> bool:
        token = normalize_interval_token(interval or fallback)
        allowed = [normalize_interval_token(item) for item in intervals or [fallback]]
        return token in allowed

    @staticmethod
    def _extract_bar_payload(
        event: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any] | None, str | None, str | None]:
        bar = event.get("bar") if isinstance(event.get("bar"), Mapping) else event
        if not isinstance(bar, Mapping):
            return None, None, None
        symbol = bar.get("symbol") or event.get("symbol")
        interval = (
            bar.get("interval")
            or bar.get("timeframe")
            or bar.get("bar_timeframe")
            or event.get("interval")
            or event.get("timeframe")
        )
        if isinstance(symbol, str):
            symbol = symbol.strip() or None
        else:
            symbol = None
        interval_token = normalize_interval_token(interval) if interval else None
        return bar, symbol, interval_token

    @staticmethod
    def _extract_bar_prices(bar: Mapping[str, Any]) -> tuple[float | None, float | None]:
        try:
            open_price = float(bar.get("open"))
            close_price = float(bar.get("close"))
        except (TypeError, ValueError):
            return None, None
        if open_price == 0.0:
            return None, None
        return open_price, close_price

    @staticmethod
    def _return_rate(bar: Mapping[str, Any]) -> float:
        try:
            open_price = float(bar.get("open"))
            close_price = float(bar.get("close"))
        except (TypeError, ValueError):
            return 0.0
        if open_price == 0.0:
            return 0.0
        return (close_price - open_price) / open_price

    def _enqueue_entry_signal(
        self,
        *,
        deviation: float,
        spread: float,
        average_spread: float,
    ) -> None:
        quantity = int(getattr(self, "position_size", 1) or 0)
        if quantity <= 0:
            return
        side = OrderSide.SELL.value if deviation > 0 else OrderSide.BUY.value
        self._telemetry_set_phase_status(
            self._PHASE_ORDER_EXECUTION,
            status="processing",
            status_code="entry_signal",
            status_reason="Preparing entry order",
            status_details=self._telemetry_details(interval=self.interval),
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_ORDER_EXECUTION,
            {
                "symbol": self.symbol,
                "interval": self.interval,
                "spread": spread,
                "average_spread": average_spread,
                "deviation": deviation,
                "threshold": float(getattr(self, "spread_threshold", 0.0) or 0.0),
                "side": side,
                "quantity": quantity,
            },
        )
        signal = StrategySignal(
            side=side,
            quantity=quantity,
            reason="pair-spread-entry",
            metadata={
                "symbol": self.symbol,
                "symbol2": self.symbol2,
                "spread": spread,
                "average_spread": average_spread,
                "spread_deviation": deviation,
                "threshold": float(getattr(self, "spread_threshold", 0.0) or 0.0),
            },
        )
        if self.produce_signal(signal):
            self._emit_pair_event(
                "pair.signal.generated",
                level="INFO",
                tone="success",
                details={
                    "side": side,
                    "quantity": quantity,
                    "reason": "pair-spread-entry",
                    "spread": spread,
                    "average_spread": average_spread,
                    "deviation": deviation,
                    "threshold": float(getattr(self, "spread_threshold", 0.0) or 0.0),
                },
            )
            self.logger.info(
                "Pair trading entry signal",
                extra={
                    "symbol": self.symbol,
                    "side": side,
                    "quantity": quantity,
                    "spread": spread,
                    "average_spread": average_spread,
                    "spread_deviation": deviation,
                },
            )
            details = self._telemetry_details(interval=self.interval)
            details.update(
                {
                    "side": side,
                    "quantity": quantity,
                    "spread": spread,
                    "average_spread": average_spread,
                    "deviation": deviation,
                    "threshold": float(getattr(self, "spread_threshold", 0.0) or 0.0),
                }
            )
            self._telemetry_log_event(
                "Pair trading entry signal queued",
                details=details,
                phase=self._PHASE_ORDER_EXECUTION,
            )
            self._telemetry_set_phase_status(
                self._PHASE_ORDER_EXECUTION,
                status="queued",
                status_code="entry_signal",
                status_reason="Entry order signal queued",
                status_details=details,
            )
            self._emit_pair_event(
                "pair.order.submitted",
                level="INFO",
                tone="success",
                details={
                    "side": side,
                    "quantity": quantity,
                    "reason": "pair-spread-entry",
                },
            )
            return
        self._emit_pair_event(
            "pair.order.blocked_by_risk",
            level="WARNING",
            tone="warning",
            details={
                "side": side,
                "quantity": quantity,
                "reason": "pair-spread-entry",
                "blocked_by": "signal_guard",
            },
        )

    def _enqueue_exit_signal(
        self,
        *,
        deviation: float,
        spread: float,
        average_spread: float,
    ) -> None:
        side = (
            OrderSide.SELL.value if self._position > 0 else OrderSide.BUY.value
        )
        self._telemetry_set_phase_status(
            self._PHASE_ORDER_EXECUTION,
            status="processing",
            status_code="exit_signal",
            status_reason="Preparing exit order",
            status_details=self._telemetry_details(interval=self.interval),
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_ORDER_EXECUTION,
            {
                "symbol": self.symbol,
                "interval": self.interval,
                "spread": spread,
                "average_spread": average_spread,
                "deviation": deviation,
                "side": side,
                "quantity": max(1, int(abs(self._position))),
            },
        )
        signal = StrategySignal(
            side=side,
            quantity=max(1, int(abs(self._position))),
            reason="pair-spread-exit",
            metadata={
                "symbol": self.symbol,
                "symbol2": self.symbol2,
                "spread": spread,
                "average_spread": average_spread,
                "spread_deviation": deviation,
                "close_position": True,
            },
        )
        if self.produce_signal(signal, is_exit_like=True):
            self._emit_pair_event(
                "pair.signal.generated",
                level="INFO",
                tone="success",
                details={
                    "side": side,
                    "quantity": max(1, int(abs(self._position))),
                    "reason": "pair-spread-exit",
                    "spread": spread,
                    "average_spread": average_spread,
                    "deviation": deviation,
                },
            )
            self.logger.info(
                "Pair trading exit signal",
                extra={
                    "symbol": self.symbol,
                    "side": side,
                    "spread": spread,
                    "average_spread": average_spread,
                    "spread_deviation": deviation,
                },
            )
            details = self._telemetry_details(interval=self.interval)
            details.update(
                {
                    "side": side,
                    "quantity": max(1, int(abs(self._position))),
                    "spread": spread,
                    "average_spread": average_spread,
                    "deviation": deviation,
                }
            )
            self._telemetry_log_event(
                "Pair trading exit signal queued",
                details=details,
                phase=self._PHASE_ORDER_EXECUTION,
            )
            self._telemetry_set_phase_status(
                self._PHASE_ORDER_EXECUTION,
                status="queued",
                status_code="exit_signal",
                status_reason="Exit order signal queued",
                status_details=details,
            )
            self._emit_pair_event(
                "pair.order.submitted",
                level="INFO",
                tone="success",
                details={
                    "side": side,
                    "quantity": max(1, int(abs(self._position))),
                    "reason": "pair-spread-exit",
                },
            )
            return
        self._emit_pair_event(
            "pair.order.blocked_by_risk",
            level="WARNING",
            tone="warning",
            details={
                "side": side,
                "quantity": max(1, int(abs(self._position))),
                "reason": "pair-spread-exit",
                "blocked_by": "signal_guard",
            },
        )

    def _enqueue_reduce_signal(
        self,
        *,
        deviation: float,
        spread: float,
        average_spread: float,
    ) -> bool:
        reduce_quantity = max(1, int(abs(self._position) // 2))
        if reduce_quantity <= 0:
            return False
        side = OrderSide.SELL.value if self._position > 0 else OrderSide.BUY.value
        self._telemetry_set_phase_status(
            self._PHASE_ORDER_EXECUTION,
            status="processing",
            status_code="reduce_signal",
            status_reason="Preparing reduce order",
            status_details=self._telemetry_details(interval=self.interval),
        )
        self._telemetry_update_phase_metrics(
            self._PHASE_ORDER_EXECUTION,
            {
                "symbol": self.symbol,
                "interval": self.interval,
                "spread": spread,
                "average_spread": average_spread,
                "deviation": deviation,
                "side": side,
                "quantity": reduce_quantity,
            },
        )
        signal = StrategySignal(
            side=side,
            quantity=reduce_quantity,
            reason="pair-spread-reduce",
            metadata={
                "symbol": self.symbol,
                "symbol2": self.symbol2,
                "spread": spread,
                "average_spread": average_spread,
                "spread_deviation": deviation,
            },
        )
        if self.produce_signal(signal, is_exit_like=True):
            self._emit_pair_event(
                "pair.signal.generated",
                level="INFO",
                tone="success",
                details={
                    "side": side,
                    "quantity": reduce_quantity,
                    "reason": "pair-spread-reduce",
                    "spread": spread,
                    "average_spread": average_spread,
                    "deviation": deviation,
                },
            )
            self.logger.info(
                "Pair trading reduce signal",
                extra={
                    "symbol": self.symbol,
                    "side": side,
                    "quantity": reduce_quantity,
                    "spread": spread,
                    "average_spread": average_spread,
                    "spread_deviation": deviation,
                },
            )
            details = self._telemetry_details(interval=self.interval)
            details.update(
                {
                    "side": side,
                    "quantity": reduce_quantity,
                    "spread": spread,
                    "average_spread": average_spread,
                    "deviation": deviation,
                }
            )
            self._telemetry_log_event(
                "Pair trading reduce signal queued",
                details=details,
                phase=self._PHASE_ORDER_EXECUTION,
            )
            self._telemetry_set_phase_status(
                self._PHASE_ORDER_EXECUTION,
                status="queued",
                status_code="reduce_signal",
                status_reason="Reduce order signal queued",
                status_details=details,
            )
            self._emit_pair_event(
                "pair.order.submitted",
                level="INFO",
                tone="success",
                details={
                    "side": side,
                    "quantity": reduce_quantity,
                    "reason": "pair-spread-reduce",
                },
            )
            return True
        self._emit_pair_event(
            "pair.order.blocked_by_risk",
            level="WARNING",
            tone="warning",
            details={
                "side": side,
                "quantity": reduce_quantity,
                "reason": "pair-spread-reduce",
                "blocked_by": "signal_guard",
            },
        )
        return False
