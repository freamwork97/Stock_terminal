"""Log privacy, bounded rotation and failure/recovery integration."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from stock_terminal.diagnostics import (configure_logging, close_logging, record,
    HealthReporter, LOGGER)
from stock_terminal.api.exceptions import TossAPIError


@pytest.fixture
def logs(tmp_path):
    assert configure_logging(tmp_path) is None
    yield tmp_path
    close_logging()


def entries(path):
    file = path / "stock-terminal.log"
    return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]


def test_sensitive_error_details_and_causes_never_enter_log(logs):
    sentinel = "SECRET_client_id_client_secret_Bearer_token_account_123456789"
    request = httpx.Request("GET", "https://example.com/?token=" + sentinel,
                            headers={"Authorization": "Bearer " + sentinel})
    response = httpx.Response(503, request=request, text=sentinel)
    cause = httpx.HTTPStatusError(sentinel, request=request, response=response)
    error = TossAPIError(sentinel)
    error.__cause__ = cause
    record("operation_failed", "stock_prices", error=error)
    record(sentinel, "stock_prices", error=error)
    record("operation_failed", sentinel, error=error)
    text = (logs / "stock-terminal.log").read_text(encoding="utf-8")
    assert sentinel not in text
    assert "Authorization" not in text
    assert "https://" not in text
    assert "Traceback" not in text
    entry, = entries(logs)
    assert entry["error_kind"] == "http"
    assert entry["http_status"] == 503
    assert set(entry) == {"time_utc", "event", "operation", "error_kind", "http_status"}


@pytest.mark.parametrize("error,kind,status", [
    (httpx.ReadTimeout("secret"), "timeout", None),
    (httpx.ConnectError("secret"), "connection", None),
    (TossAPIError("secret", status_code=500), "http", 500),
    (ValueError("secret"), "invalid_response", None),
])
def test_error_categories(logs, error, kind, status):
    record("operation_failed", "chart", error=error)
    entry, = entries(logs)
    assert entry["error_kind"] == kind
    assert entry.get("http_status") == status
    assert "secret" not in json.dumps(entry)


def test_repeated_failure_rate_limit_and_recovery(logs, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("stock_terminal.diagnostics.time", SimpleNamespace(monotonic=lambda: clock[0]))
    health = HealthReporter()
    for _ in range(10):
        health.report("stock_prices", error=httpx.ConnectError("secret"))
    assert len(entries(logs)) == 1
    clock[0] = 61
    health.report("stock_prices", error=httpx.ConnectError("secret"))
    health.report("stock_prices")
    health.report("stock_prices")
    assert [entry["event"] for entry in entries(logs)] == ["operation_failed", "operation_failed", "operation_recovered"]
    assert not health._failed


def test_rotation_is_bounded(tmp_path):
    try:
        configure_logging(tmp_path, max_bytes=300, backup_count=2)
        for _ in range(100):
            record("operation_failed", "stock_prices", error=TossAPIError("secret"))
        files = list(tmp_path.glob("stock-terminal.log*"))
        assert len(files) == 3
        assert all(file.stat().st_size <= 300 for file in files)
    finally:
        close_logging()


def test_log_file_failure_does_not_crash_or_print_into_tui(logs, monkeypatch, capsys):
    handler = next(handler for handler in LOGGER.handlers if hasattr(handler, "shouldRollover"))
    def fail(*args):
        raise OSError("disk full with secret")
    monkeypatch.setattr(handler, "shouldRollover", fail)
    record("operation_failed", "chart", error=TossAPIError("secret"))
    assert capsys.readouterr().err == ""


def test_log_startup_failure_is_nonfatal(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("occupied", encoding="utf-8")
    try:
        assert configure_logging(blocked) is not None
        record("app_started", "runtime")
    finally:
        close_logging()


async def test_price_failure_and_recovery_are_logged_safely(logs, monkeypatch):
    from stock_terminal.config import Settings
    from stock_terminal.api.models import Price
    from decimal import Decimal
    from stock_terminal.storage.watchlist import WatchlistItem
    from stock_terminal.tui.app import StockTerminalApp
    from stock_terminal.tui.widgets.watchlist_table import WatchlistTable
    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", logs / "watchlist.json")
    app = StockTerminalApp(Settings("PRIVATE_ID", "PRIVATE_SECRET", 999, 999))
    monkeypatch.setattr(app, "poll_prices", lambda: None)
    monkeypatch.setattr(app, "poll_baselines", lambda: None)
    monkeypatch.setattr(app, "load_chart", lambda: None)
    monkeypatch.setattr(app.client, "get_prices", AsyncMock(side_effect=[TossAPIError("PRIVATE_BODY"), [], [Price("AAPL", Decimal("100"), "USD", None)]]))
    app.watchlist.add(WatchlistItem("AAPL"))
    try:
        async with app.run_test():
            table = app.query_one(WatchlistTable)
            await app._refresh_prices(table)
            await app._refresh_prices(table)
            assert not any(entry["event"] == "operation_recovered" for entry in entries(logs))
            await app._refresh_prices(table)
    finally:
        await app.client.aclose()
    data = entries(logs)
    assert any(entry["event"] == "operation_failed" for entry in data)
    assert any(entry["event"] == "operation_recovered" for entry in data)
    text = (logs / "stock-terminal.log").read_text(encoding="utf-8")
    assert "PRIVATE" not in text


def test_crash_lifecycle_is_logged_without_exception_message(tmp_path, monkeypatch):
    from stock_terminal import main
    from stock_terminal.config import Settings
    monkeypatch.setattr("stock_terminal.config.DATA_DIR", tmp_path)
    monkeypatch.setattr(Settings, "load", lambda: Settings("PRIVATE_ID", "PRIVATE_SECRET", 999, 999))
    monkeypatch.setattr("stock_terminal.tui.app.StockTerminalApp", Mock(return_value=Mock(run=Mock(side_effect=RuntimeError("PRIVATE_CRASH")))))
    with pytest.raises(RuntimeError):
        main()
    data = entries(tmp_path / "logs")
    assert [entry["event"] for entry in data] == ["app_started", "app_crashed", "app_stopped"]
    assert "PRIVATE" not in (tmp_path / "logs" / "stock-terminal.log").read_text(encoding="utf-8")
