from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque

__all__ = [
    "EmaTracker",
    "RsiTracker",
    "parse_timestamp",
    "coerce_float",
]


def parse_timestamp(value: Any) -> datetime:
    """Return a timezone-aware UTC timestamp derived from *value*."""

    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, (int, float)):
        ts = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return datetime.now(timezone.utc)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            ts = datetime.fromisoformat(text)
        except ValueError:
            try:
                ts = datetime.fromtimestamp(float(text), tz=timezone.utc)
            except Exception:
                return datetime.now(timezone.utc)
    else:
        return datetime.now(timezone.utc)

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def coerce_float(value: Any, default: float = 0.0) -> float:
    """Attempt to coerce *value* to a finite float."""

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(numeric) or math.isinf(numeric):
        return default
    return numeric


@dataclass
class EmaTracker:
    """Incrementally compute an exponential moving average."""

    period: int
    value: float | None = None
    ready: bool = False
    _seed: Deque[float] = field(default_factory=deque, init=False, repr=False)

    def reset(self) -> None:
        self.value = None
        self.ready = False
        self._seed.clear()

    def update(self, price: float | None) -> float | None:
        if price is None or not math.isfinite(price):
            return self.value
        if self.period <= 0:
            self.value = price
            self.ready = True
            return self.value
        if not self.ready:
            self._seed.append(price)
            if len(self._seed) > self.period:
                self._seed.popleft()
            if len(self._seed) < self.period:
                return None
            self.value = sum(self._seed) / len(self._seed)
            self.ready = True
            return self.value
        if self.value is None:
            self.value = price
            self.ready = True
            return self.value
        alpha = 2.0 / (self.period + 1.0)
        self.value = self.value + alpha * (price - self.value)
        return self.value


@dataclass
class RsiTracker:
    """Incrementally compute a Relative Strength Index (RSI)."""

    period: int
    avg_gain: float | None = None
    avg_loss: float | None = None
    prev_close: float | None = None
    value: float | None = None
    _warm_gains: Deque[float] = field(default_factory=deque, init=False, repr=False)
    _warm_losses: Deque[float] = field(default_factory=deque, init=False, repr=False)

    def reset(self) -> None:
        self.avg_gain = None
        self.avg_loss = None
        self.prev_close = None
        self.value = None
        self._warm_gains.clear()
        self._warm_losses.clear()

    def update(self, close: float | None) -> float | None:
        if close is None or not math.isfinite(close):
            return self.value
        if self.period <= 0:
            self.value = 50.0
            self.prev_close = close
            return self.value
        if self.prev_close is None:
            self.prev_close = close
            return None
        delta = close - self.prev_close
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        if self.avg_gain is None or self.avg_loss is None:
            self._warm_gains.append(gain)
            self._warm_losses.append(loss)
            if len(self._warm_gains) > self.period:
                self._warm_gains.popleft()
                self._warm_losses.popleft()
            if len(self._warm_gains) < self.period:
                self.prev_close = close
                return None
            self.avg_gain = sum(self._warm_gains) / self.period
            self.avg_loss = sum(self._warm_losses) / self.period
        else:
            self.avg_gain = ((self.avg_gain * (self.period - 1)) + gain) / self.period
            self.avg_loss = ((self.avg_loss * (self.period - 1)) + loss) / self.period
        self.prev_close = close
        if self.avg_loss is None or self.avg_loss == 0.0:
            self.value = 100.0 if (self.avg_gain or 0.0) > 0.0 else 50.0
        else:
            rs = (self.avg_gain or 0.0) / self.avg_loss
            self.value = 100.0 - (100.0 / (1.0 + rs))
        self.value = max(0.0, min(100.0, self.value))
        return self.value
