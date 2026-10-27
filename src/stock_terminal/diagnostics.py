"""Bounded diagnostics built only from allowlisted, non-sensitive fields."""
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import time

import httpx

LOGGER = logging.getLogger("stock_terminal.diagnostics")
LOGGER.propagate = False
LOGGER.addHandler(logging.NullHandler())

EVENTS = {"app_started", "app_stopped", "app_crashed", "startup_failed", "operation_failed",
          "operation_recovered", "baseline_updated", "baseline_retry", "watchlist_recovered",
          "preferences_invalid", "worker_failed"}
OPERATIONS = {"runtime", "configuration", "watchlist", "preferences", "stock_prices",
              "index_prices", "chart", "baseline", "single_price", "search", "worker"}


class _QuietRotatingHandler(RotatingFileHandler):
    def handleError(self, record):
        # Disk-full/permission errors must never crash polling or print over the TUI.
        pass


def configure_logging(directory: Path, *, max_bytes: int = 2 * 1024 * 1024,
                      backup_count: int = 4) -> str | None:
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
    LOGGER.addHandler(logging.NullHandler())
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = _QuietRotatingHandler(directory / "stock-terminal.log", maxBytes=max_bytes,
                                       backupCount=backup_count, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)
    except OSError:
        return "진단 로그를 시작할 수 없습니다. 로그 없이 계속 실행합니다."
    return None


def close_logging() -> None:
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
    LOGGER.addHandler(logging.NullHandler())


def error_details(error: BaseException) -> tuple[str, int | None]:
    from stock_terminal.api.exceptions import TossAuthError, TossForbiddenError, TossRateLimitError
    from stock_terminal.storage.watchlist import WatchlistError
    from stock_terminal.storage.preferences import PreferencesError
    current = error
    seen = set()
    status = getattr(error, "status_code", None)
    while id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.TimeoutException):
            return "timeout", None
        if isinstance(current, httpx.RequestError):
            return "connection", None
        if isinstance(current, httpx.HTTPStatusError):
            return "http", current.response.status_code
        if isinstance(current, TossAuthError):
            return "authentication", 401
        if isinstance(current, TossForbiddenError):
            return "forbidden", 403
        if isinstance(current, TossRateLimitError):
            return "rate_limit", 429
        if isinstance(current, (WatchlistError, PreferencesError, OSError)):
            return "storage", None
        if isinstance(current, (ValueError, KeyError, TypeError, ArithmeticError)):
            return "invalid_response", None
        cause = current.__cause__
        if cause is None:
            break
        current = cause
    return ("http" if isinstance(status, int) else "api"), status if isinstance(status, int) else None


def record(event: str, operation: str, *, error: BaseException | None = None,
           count: int | None = None, delay: float | None = None) -> None:
    if event not in EVENTS or operation not in OPERATIONS:
        return
    payload = {"time_utc": datetime.now(timezone.utc).isoformat(), "event": event, "operation": operation}
    if error is not None:
        kind, status = error_details(error)
        payload["error_kind"] = kind
        if status is not None and 100 <= status <= 599:
            payload["http_status"] = status
    if type(count) is int and count >= 0:
        payload["count"] = count
    if type(delay) in (int, float) and math.isfinite(delay) and delay >= 0:
        payload["retry_seconds"] = delay
    # No exception messages, tracebacks, URLs, headers, bodies or symbol input.
    LOGGER.log(logging.WARNING if error is not None or event.endswith(("failed", "retry")) else logging.INFO,
               json.dumps(payload, ensure_ascii=False))


class HealthReporter:
    """Log state changes and at most one identical failure reminder per minute."""
    def __init__(self):
        self._failed: dict[str, tuple[str, float]] = {}

    def report(self, operation: str, *, error: BaseException | None = None, missing: int = 0):
        if operation not in OPERATIONS:
            return
        now = time.monotonic()
        if error is None and missing == 0:
            if self._failed.pop(operation, None) is not None:
                record("operation_recovered", operation)
            return
        kind = repr(error_details(error)) if error else "missing"
        previous = self._failed.get(operation)
        if previous is None or previous[0] != kind or now - previous[1] >= 60:
            record("operation_failed", operation, error=error, count=missing or None)
            self._failed[operation] = (kind, now)
