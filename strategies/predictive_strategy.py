"""Predictive strategy helpers required by runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, MutableSequence, Optional


@dataclass(frozen=True)
class PredictiveModelState:
    version: str
    posterior_mean: float
    posterior_std: float
    policy_score: float
    fusion_config: object
    metrics: Mapping[str, float] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "PredictiveModelState":
        return cls(
            version="uninitialised",
            posterior_mean=0.0,
            posterior_std=1.0,
            policy_score=0.0,
            fusion_config=object(),
            metrics={},
        )


class PredictiveModelRepository:
    def __init__(self) -> None:
        self._states: dict[str, PredictiveModelState] = {}
        self._active_version: Optional[str] = None
        self._listeners: MutableSequence[Callable[[PredictiveModelState], None]] = []

    def upsert(self, state: PredictiveModelState) -> None:
        self._states[state.version] = state
        if self._active_version is None:
            self._active_version = state.version
            self._notify(state)

    def activate(self, version: str) -> None:
        if version not in self._states:
            raise KeyError(f"model version {version!r} unknown")
        self._active_version = version
        self._notify(self._states[version])

    def get_active(self) -> PredictiveModelState:
        if not self._active_version:
            raise LookupError("no predictive model version has been activated")
        return self._states[self._active_version]

    def subscribe(self, listener: Callable[[PredictiveModelState], None]) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[PredictiveModelState], None]) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            return

    def _notify(self, state: PredictiveModelState) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(state)
            except Exception:
                continue

