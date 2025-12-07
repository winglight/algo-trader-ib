"""Mean reversion strategy based on candle z-scores."""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from statistics import mean
from typing import Any, Deque, Dict, Mapping

from src.common.market_data.aggregation import floor_timestamp as _floor_timestamp
from src.data_layer import DataSubscriptionRequest, get_data_source_manager
from src.market_data.history_chunks import load_history_with_backoff
from src.strategies.candle import CandleSubscriptionStrategy
from src.strategies.templates import StrategySignal, StrategyTemplate


class MeanReversionStrategy(CandleSubscriptionStrategy, StrategyTemplate):
    """Simple mean reversion strategy using rolling z-score."""

    strategy_type = "mean_reversion"
    interval: str = "5m"
    parameter_definitions = {
        "symbol": {
            "type": "str",
            "allow_null": True,
            "default": "ES",
            "description": "Symbol to subscribe for candle updates.",
        },
        "lookback": {
            "type": "int",
            "default": 20,
            "min": 3,
            "description": "Rolling window for mean and std calculations.",
        },
        "entry_zscore": {
            "type": "float",
            "default": 1.5,
            "min": 0.5,
            "description": "Z-score threshold for trade entries.",
        },
        "exit_zscore": {
            "type": "float",
            "default": 0.5,
            "min": 0.0,
            "description": "Z-score threshold for trade exits.",
        },
        "order_quantity": {
            "type": "int",
            "default": 1,
            "min": 1,
            "step": 1,
            "description": "Order quantity when entering/exiting positions.",
        },
    }
    default_parameters = {
        "symbol": "ES",
        "lookback": 20,
        "entry_zscore": 1.5,
        "exit_zscore": 0.5,
        "order_quantity": 1,
    }

    def __post_init__(self) -> None:
        self.interval = "5m"
        super().__post_init__()
        self._history: Deque[float] = deque(maxlen=int(getattr(self, "lookback", 20)))
        self._position: int = 0
        self._last_closed_candle_id: Any | None = None
        self._initial_backfill_requested: bool = False

    def _log_skip_reason(
        self,
        message: str,
        *,
        tone: str = "neutral",
        level: str = "INFO",
        details: Mapping[str, Any] | None = None,
        dedupe_interval: float = 5.0,
    ) -> None:
        now = time.monotonic()
        key = message
        last_msg = getattr(self, "_last_skip_message", None)
        last_at = getattr(self, "_last_skip_logged_at", 0.0)
        if key == last_msg and now - last_at < dedupe_interval:
            return
        self._last_skip_message = key
        self._last_skip_logged_at = now
        payload = dict(details or {})
        if payload:
            self.logger.info(message, extra={"details": payload})
        else:
            self.logger.info(message)
        self._telemetry_log(
            message,
            level=level,
            tone=tone,
            deduplicate=False,
            details=payload,
        )

    def _telemetry_record_signal(self, side: str) -> None:
        telemetry = getattr(self, "runtime_telemetry", None)
        if telemetry is None:
            return
        try:
            telemetry.record_signal(self._telemetry_strategy_id(), side)
        except KeyError:
            return
        except Exception:  # pragma: no cover - defensive
            self.logger.exception("Failed to record signal telemetry")

    def _process_candle_event(self, candle: Mapping[str, Any]) -> None:
        if candle.get("type") not in {None, "candle"}:
            return

        if not bool(candle.get("is_closed", False)):
            self._log_skip_reason(
                "Ignoring intrabar candle update for mean reversion",
                details={
                    "interval": candle.get("interval") or getattr(self, "interval", ""),
                    "symbol": candle.get("symbol") or getattr(self, "symbol", ""),
                },
            )
            return

        expected_interval = getattr(self, "interval", "")
        interval = candle.get("interval")
        if interval and expected_interval and interval != expected_interval:
            self._log_skip_reason(
                "Skipping candle with unexpected interval for mean reversion",
                details={
                    "interval": interval,
                    "expected_interval": expected_interval,
                    "symbol": candle.get("symbol") or getattr(self, "symbol", ""),
                },
            )
            return

        price = candle.get("close")
        if price is None:
            price = candle.get("price")
        if price is None:
            return

        def _canonical_candle_id(payload: Mapping[str, Any]) -> str | None:
            def _coerce_datetime(value: Any) -> datetime | None:
                if isinstance(value, datetime):
                    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
                if isinstance(value, str):
                    text = value.strip()
                    if text:
                        if text.endswith("Z"):
                            text = text[:-1] + "+00:00"
                        try:
                            parsed = datetime.fromisoformat(text)
                        except ValueError:
                            parsed = None
                        return (
                            parsed if parsed is None or parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                        )
                    return None
                if isinstance(value, (int, float)):
                    try:
                        return datetime.fromtimestamp(float(value), tz=timezone.utc)
                    except (TypeError, ValueError, OSError):
                        return None
                return None

            end_value = payload.get("end")
            seq_value = payload.get("sequence")
            end_dt = _coerce_datetime(end_value)
            if end_dt is not None:
                return end_dt.astimezone(timezone.utc).isoformat()
            if seq_value is not None:
                try:
                    return str(seq_value)
                except Exception:
                    return None

            interval_delta = getattr(self, "_interval_delta", None)
            if interval_delta is None:
                return None

            start_value = (
                payload.get("start")
                or payload.get("open_time")
                or payload.get("timestamp")
                or payload.get("time")
                or payload.get("ts")
            )
            start_dt = _coerce_datetime(start_value)
            if start_dt is None:
                return None

            bucket_start = _floor_timestamp(start_dt, interval_delta)
            bucket_end = bucket_start + interval_delta
            return f"{bucket_start.isoformat()}_{bucket_end.isoformat()}"

        candle_id = _canonical_candle_id(candle)
        if candle_id is not None:
            if self._last_closed_candle_id == candle_id:
                return
            self._last_closed_candle_id = candle_id

        price_value = float(price)

        lookback = int(getattr(self, "lookback", 20))
        if self._history.maxlen != lookback:
            self._history = deque(self._history, maxlen=lookback)

        is_history_replay = bool(getattr(self, "_history_replay_in_progress", False))
        self._history.append(price_value)
        if is_history_replay:
            return
        if len(self._history) < self._history.maxlen:
            if self._use_unified_data and not self._initial_backfill_requested:
                manager = None
                try:
                    manager = self._data_layer_manager or get_data_source_manager()
                except Exception:
                    manager = None
                if manager is not None:
                    try:
                        have = len(self._history)
                        missing = max(self._history.maxlen - have, 1)
                        delta = self._interval_delta
                        now = datetime.now(timezone.utc)
                        start = _floor_timestamp(now - (delta * missing), delta)
                        request = DataSubscriptionRequest(
                            channel=self._resolve_bar_channel(),
                            symbol=self.symbol,
                            interval=self.interval,
                            options={
                                "interval": self.interval,
                                "start": start,
                                "end": now,
                            },
                        )
                        config = self._history_replay_config()
                        records = self._run_coro(
                            load_history_with_backoff(
                                manager,
                                request,
                                start=start,
                                end=now,
                                interval=delta,
                                config=config,
                                logger=self.logger,
                            )
                        )
                        aggregated: list[Mapping[str, Any]] = []
                        self._history_replay_in_progress = True
                        try:
                            self._reset_unified_bucket()
                            for item in list(records or [])[-self.history_limit :]:
                                if not item:
                                    continue
                                closed = self._ingest_bar_payload(item)
                                if closed is not None:
                                    aggregated.append(closed)
                            leftovers = self._flush_unified_bucket(close_partial=True)
                            if leftovers:
                                aggregated.extend(leftovers)
                            for snapshot in aggregated:
                                normalised = self._normalise_candle(snapshot, is_closed=True)
                                if normalised is not None:
                                    self._invoke_candle_handlers(normalised)
                        finally:
                            self._history_replay_in_progress = False
                        self._initial_backfill_requested = True
                    except Exception:
                        self.logger.debug("Mean reversion unified backfill attempt failed", exc_info=True)
            elif not self._use_unified_data and not self._initial_backfill_requested:
                loader = getattr(self, "_history_loader", None)
                if callable(loader):
                    try:
                        have = len(self._history)
                        missing = max(self._history.maxlen - have, 1)
                        minutes_needed = int(max(1, (self._interval_delta.total_seconds() / 60))) * missing
                        records = self._run_coro(loader(self.symbol, self.interval, minutes_needed))
                        self._history_replay_in_progress = True
                        try:
                            for item in list(records or [])[-minutes_needed:]:
                                normalised = self._normalise_candle(item, is_closed=True)
                                if normalised is not None:
                                    self._invoke_candle_handlers(normalised)
                        finally:
                            self._history_replay_in_progress = False
                        self._initial_backfill_requested = True
                    except Exception:
                        self.logger.debug("Mean reversion legacy backfill attempt failed", exc_info=True)
            self._telemetry_log_signal_waiting(
                step="mean_reversion_history",
                reason="等待足够的历史K线以计算z-score",
                metric=float(len(self._history)),
                threshold=float(self._history.maxlen),
                comparison="bars",
                details={
                    "symbol": getattr(self, "symbol", "") or "",
                    "have_bars": len(self._history),
                    "need_bars": self._history.maxlen,
                },
            )
            return

        avg_price = mean(self._history)
        squared = [pow(item - avg_price, 2) for item in self._history]
        variance = sum(squared) / max(len(self._history) - 1, 1)
        std_dev = variance ** 0.5 if variance > 0 else 0.0
        zscore = (price_value - avg_price) / std_dev if std_dev else 0.0

        entry_threshold = float(getattr(self, "entry_zscore", 1.5))
        exit_threshold = float(getattr(self, "exit_zscore", 0.5))
        raw_quantity = getattr(self, "order_quantity", 0)
        quantity, fractional_quantity = self._resolve_order_quantity(raw_quantity)
        quantity = int(quantity)

        if quantity <= 0:
            return

        raw_quantity_float: float | None
        try:
            raw_quantity_float = float(raw_quantity)
        except (TypeError, ValueError):
            raw_quantity_float = None

        def build_metadata() -> Dict[str, Any]:
            metadata = {
                "zscore": zscore,
                "price": price_value,
                "symbol": getattr(self, "symbol", "") or "",
            }
            if raw_quantity_float is not None and fractional_quantity > 0.0:
                metadata["quantity_requested_raw"] = raw_quantity_float
            if fractional_quantity > 0.0:
                metadata["quantity_fractional_discarded"] = fractional_quantity
            return metadata

        def enqueue_signal(
            signal: StrategySignal,
            *,
            event: str,
            tone: str,
            new_position: int,
        ) -> None:
            if event == "entry":
                now_monotonic = self._monotonic_now()
                if self.breaker_tripped:
                    self._log_skip_reason(
                        "Mean reversion entry suppressed by breaker",
                        details={
                            "symbol": getattr(self, "symbol", "") or "",
                        },
                    )
                    return
                cooldown = max(0.0, float(self.cooldown_seconds))
                if cooldown > 0.0 and now_monotonic < self._cooldown_until:
                    remaining = max(0.0, self._cooldown_until - now_monotonic)
                    self._log_skip_reason(
                        "Mean reversion entry suppressed by cooldown",
                        details={"remaining_seconds": round(remaining, 3)},
                    )
                    return
                frequency = max(0.0, float(self.signal_frequency_seconds))
                last_wall = self._last_signal_wall
                if frequency > 0.0 and last_wall is not None:
                    now_wall = self._wall_clock_now()
                    elapsed = now_wall - last_wall
                    if elapsed < frequency:
                        remaining = max(0.0, frequency - elapsed)
                        self._log_skip_reason(
                            "Mean reversion entry suppressed by frequency",
                            details={"remaining_seconds": round(remaining, 3)},
                        )
                        return
            if not self.queue_order(signal.as_dict()):
                self.logger.warning(
                    "Failed to queue mean reversion %s signal for execution", event
                )
                return

            self._position = new_position
            # Track signal-generation phase telemetry for kline UI
            try:
                if not hasattr(self, "_signals_generated"):
                    self._signals_generated = 0  # type: ignore[attr-defined]
                self._signals_generated += 1  # type: ignore[attr-defined]
            except Exception:
                # Defensive: ensure counter does not break runtime
                pass
            self._telemetry_set_phase_status(
                self._PHASE_SIGNALS,
                status="ready",
                status_code="signal_generated",
            )
            self._telemetry_update_phase_metrics(
                self._PHASE_SIGNALS,
                signals_generated=getattr(self, "_signals_generated", 1),
                last_signal_side=signal.side,
                last_signal_quantity=signal.quantity,
            )
            details = dict(signal.metadata)
            details.update(
                {
                    "side": signal.side,
                    "quantity": signal.quantity,
                    "reason": signal.reason,
                    "position": self._position,
                }
            )
            self._telemetry_log(
                f"Mean reversion {event} signal",
                level="INFO",
                tone=tone,
                phase=self._PHASE_SIGNALS,
                details=details,
                deduplicate=False,
            )

        if self._position == 0:
            if zscore >= entry_threshold:
                enqueue_signal(
                    StrategySignal(
                        side="SELL",
                        quantity=quantity,
                        reason="mean-reversion-entry",
                        metadata=build_metadata(),
                    ),
                    event="entry",
                    tone="positive",
                    new_position=-1,
                )
                return
            elif zscore <= -entry_threshold:
                enqueue_signal(
                    StrategySignal(
                        side="BUY",
                        quantity=quantity,
                        reason="mean-reversion-entry",
                        metadata=build_metadata(),
                    ),
                    event="entry",
                    tone="positive",
                    new_position=1,
                )
                return
        elif self._position > 0 and zscore >= exit_threshold:
            enqueue_signal(
                StrategySignal(
                    side="SELL",
                    quantity=quantity,
                    reason="mean-reversion-exit",
                    metadata=build_metadata(),
                ),
                event="exit",
                tone="neutral",
                new_position=0,
            )
            return
        elif self._position < 0 and zscore <= -exit_threshold:
            enqueue_signal(
                StrategySignal(
                    side="BUY",
                    quantity=quantity,
                    reason="mean-reversion-exit",
                    metadata=build_metadata(),
                ),
                event="exit",
                tone="neutral",
                new_position=0,
            )
            return
        else:
            evaluations: list[dict[str, Any]] = []
            if self._position == 0:
                evaluations.append(
                    {
                        "condition": f"zscore>={entry_threshold}",
                        "current": zscore,
                        "passed": bool(zscore >= entry_threshold),
                    }
                )
                evaluations.append(
                    {
                        "condition": f"zscore<={-entry_threshold}",
                        "current": zscore,
                        "passed": bool(zscore <= -entry_threshold),
                    }
                )
            elif self._position > 0:
                evaluations.append(
                    {
                        "condition": f"zscore>={exit_threshold}",
                        "current": zscore,
                        "passed": bool(zscore >= exit_threshold),
                    }
                )
            else:
                evaluations.append(
                    {
                        "condition": f"zscore<={-exit_threshold}",
                        "current": zscore,
                        "passed": bool(zscore <= -exit_threshold),
                    }
                )
            self._telemetry_set_phase_status(
                self._PHASE_SIGNALS,
                status="evaluated",
                status_code="conditions_checked",
            )
            self._telemetry_log(
                "Mean reversion conditions evaluated",
                level="INFO",
                tone="neutral",
                phase=self._PHASE_SIGNALS,
                details={
                    "symbol": getattr(self, "symbol", "") or "",
                    "zscore": zscore,
                    "entry_threshold": entry_threshold,
                    "exit_threshold": exit_threshold,
                    "position": self._position,
                    "evaluations": evaluations,
                },
                deduplicate=False,
            )

    def on_candle(self, candle: Mapping[str, Any]) -> None:  # noqa: D401 - event hook
        self._process_candle_event(candle)

    async def on_market_event(self, event: Mapping[str, Any]) -> None:
        if getattr(self, "_use_unified_data", False) and event.get("type") == "candle":
            if not bool(event.get("is_closed", False)):
                return
        self._process_candle_event(event)
