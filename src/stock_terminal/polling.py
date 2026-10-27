"""Bounded session counters and monotonic polling-health measurements."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic


@dataclass(slots=True)
class PollingHealth:
    created_at: float = field(default_factory=lambda: monotonic())
    started_at: float | None = None
    completed_at: float | None = None
    last_received: datetime | None = None
    received_at: float | None = None
    last_duration: float = 0.0
    polls: int = 0
    success: int = 0
    failure: int = 0
    missing: int = 0
    cancelled: int = 0
    cycle_failure: int = 0
    cycle_missing: int = 0

    def begin(self) -> None:
        self.started_at = monotonic()
        self.cycle_failure = self.cycle_missing = 0

    def receive(self, success: int, failure: int = 0, missing: int = 0) -> None:
        self.success += success
        self.failure += failure
        self.missing += missing
        self.cycle_failure += failure
        self.cycle_missing += missing
        if success:
            self.last_received = datetime.now(timezone.utc)
            self.received_at = monotonic()

    def finish(self, *, cancelled: bool = False) -> None:
        now = monotonic()
        if self.started_at is not None:
            self.last_duration = max(0.0, now - self.started_at)
        self.started_at = None
        if cancelled:
            self.cancelled += 1
        else:
            self.polls += 1
            self.completed_at = now

    def snapshot(self, interval: float, has_symbols: bool) -> dict:
        now = monotonic()
        duration = max(0.0, now - self.started_at) if self.started_at is not None else self.last_duration
        threshold = max(30.0, interval * 3)
        age = max(0.0, now - self.received_at) if self.received_at is not None else None
        if not has_symbols:
            state = "idle"
        elif self.started_at is not None:
            state = "slow" if duration > interval else "fetching"
        elif now - (self.completed_at if self.completed_at is not None else self.created_at) > threshold:
            state = "stalled"
        elif duration > interval:
            state = "slow"
        elif self.cycle_failure or self.cycle_missing:
            state = "failed"
        elif age is None or age > threshold:
            state = "no_data"
        else:
            state = "normal"
        return {"state": state, "poll_in_progress": self.started_at is not None,
                "duration_seconds": round(duration, 3), "receive_age_seconds": round(age, 3) if age is not None else None,
                "poll_count": self.polls, "success_count": self.success, "failure_count": self.failure,
                "missing_count": self.missing, "cancelled_count": self.cancelled,
                "last_received_utc": self.last_received}
