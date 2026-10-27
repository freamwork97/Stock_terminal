"""Session-local threshold alerts with edge detection and bounded history."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from time import monotonic


@dataclass(frozen=True, slots=True)
class AlertEvent:
    symbol: str
    direction: str
    price: Decimal
    threshold: Decimal
    currency: str
    occurred_at: datetime


@dataclass(slots=True)
class _State:
    thresholds: tuple[Decimal | None, Decimal | None]
    region: str | None = None
    upper_sent: float | None = None
    lower_sent: float | None = None


class AlertTracker:
    def __init__(self, cooldown_seconds: float = 60.0) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._states: dict[str, _State] = {}
        self.history: deque[AlertEvent] = deque(maxlen=100)

    def reset(self, symbol: str) -> None:
        self._states.pop(symbol, None)

    def evaluate(self, symbol: str, price: Decimal, currency: str,
                 upper: Decimal | None, lower: Decimal | None, *, enabled: bool = True) -> AlertEvent | None:
        if not enabled:
            self.reset(symbol)
            return None
        if not price.is_finite() or price <= 0:
            return None
        thresholds = (upper, lower)
        if any(v is not None and (not v.is_finite() or v <= 0) for v in thresholds):
            return None
        if upper is not None and lower is not None and upper <= lower:
            return None
        state = self._states.get(symbol)
        if state is None or state.thresholds != thresholds:
            state = self._states[symbol] = _State(thresholds)
        region = ("upper" if upper is not None and price >= upper else
                  "lower" if lower is not None and price <= lower else None)
        previous = state.region
        state.region = region
        if region is None or region == previous:
            return None
        now = monotonic()
        last_sent = state.upper_sent if region == "upper" else state.lower_sent
        # A crossing suppressed by cooldown is consumed, never queued for later.
        if last_sent is not None and now - last_sent < self.cooldown_seconds:
            return None
        if region == "upper":
            state.upper_sent = now
        else:
            state.lower_sent = now
        threshold = upper if region == "upper" else lower
        event = AlertEvent(symbol, region, price, threshold, currency, datetime.now(timezone.utc))
        self.history.appendleft(event)
        return event
