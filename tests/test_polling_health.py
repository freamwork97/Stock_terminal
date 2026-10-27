"""Independent heartbeat, timing and counters, including stalled requests."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Static

from stock_terminal.polling import PollingHealth
from stock_terminal.diagnostics import configure_logging, close_logging, record
from stock_terminal.api.exceptions import TossAPIError
from stock_terminal.api.models import Price
from stock_terminal.config import Settings
from stock_terminal.storage.watchlist import WatchlistItem
from stock_terminal.tui.app import StockTerminalApp
from stock_terminal.tui.widgets.watchlist_table import WatchlistTable
from decimal import Decimal


@pytest.fixture
def clock(monkeypatch):
    value = [0.0]
    monkeypatch.setattr("stock_terminal.polling.monotonic", lambda: value[0])
    return value


def test_polling_duration_delay_recovery_and_counters(clock):
    health = PollingHealth()
    assert health.snapshot(3, False)["state"] == "idle"
    health.begin()
    assert health.snapshot(3, True)["state"] == "fetching"
    clock[0] = 4
    assert health.snapshot(3, True)["state"] == "slow"
    health.receive(2, failure=1, missing=1)
    health.finish()
    snapshot = health.snapshot(3, True)
    assert snapshot["state"] == "slow"
    assert snapshot["duration_seconds"] == 4
    assert snapshot["success_count"] == 2
    assert snapshot["failure_count"] == 1
    assert snapshot["missing_count"] == 1
    health.begin()
    clock[0] += 1
    health.receive(3)
    health.finish()
    assert health.snapshot(3, True)["state"] == "normal"
    clock[0] += 31
    assert health.snapshot(3, True)["state"] == "stalled"


def test_failures_do_not_advance_receive_time_and_cancellation_is_distinct(clock):
    health = PollingHealth()
    health.begin()
    health.receive(1)
    health.finish()
    received = health.last_received
    clock[0] = 10
    health.begin()
    health.receive(0, failure=2)
    health.finish(cancelled=True)
    assert health.last_received == received
    snapshot = health.snapshot(3, True)
    assert snapshot["cancelled_count"] == 1
    assert snapshot["poll_count"] == 1
    assert snapshot["failure_count"] == 2
    assert not snapshot["poll_in_progress"]


def test_heartbeat_fields_are_allowlisted(tmp_path, clock):
    try:
        configure_logging(tmp_path)
        health = PollingHealth()
        health.begin()
        health.receive(1)
        health.finish()
        metrics = health.snapshot(3, True)
        metrics["Authorization"] = "SECRET"
        metrics["response_body"] = "SECRET"
        record("poll_status", "runtime", metrics=metrics)
        text = (tmp_path / "stock-terminal.log").read_text(encoding="utf-8")
        entry = json.loads(text)
        assert entry["success_count"] == 1
        assert entry["last_received_utc"]
        assert "SECRET" not in text
        assert "Authorization" not in text
    finally:
        close_logging()


async def test_heartbeat_and_display_continue_during_blocked_poll(tmp_path, monkeypatch, clock):
    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    app = StockTerminalApp(Settings("id", "secret", 3, 999))
    monkeypatch.setattr(app, "poll_baselines", lambda: None)
    monkeypatch.setattr(app, "load_chart", lambda: None)
    app.watchlist.add(WatchlistItem("AAPL"))
    entered = asyncio.Event()
    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(app.client, "get_prices", blocked)
    original_interval = app.set_interval
    def quick_status_timer(interval, callback, *args, **kwargs):
        if callback.__name__ in ("_update_poll_status", "_log_poll_status"):
            interval = 0.01
        return original_interval(interval, callback, *args, **kwargs)
    monkeypatch.setattr(app, "set_interval", quick_status_timer)
    try:
        configure_logging(tmp_path / "logs")
        async with app.run_test() as pilot:
            await asyncio.wait_for(entered.wait(), 2)
            clock[0] += 4
            await pilot.pause(0.1)
            assert "폴링 지연" in str(app.query_one("#poll-status", Static).render())
            entry = json.loads((tmp_path / "logs" / "stock-terminal.log").read_text(encoding="utf-8").splitlines()[-1])
            assert entry["event"] == "poll_status"
            assert entry["poll_in_progress"] is True
            assert entry["state"] == "slow"
    finally:
        await app.client.aclose()
        close_logging()


async def test_missing_failure_and_success_counts(tmp_path, monkeypatch):
    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    app = StockTerminalApp(Settings("id", "secret", 3, 999))
    monkeypatch.setattr(app, "poll_prices", lambda: None)
    monkeypatch.setattr(app, "poll_baselines", lambda: None)
    monkeypatch.setattr(app, "load_chart", lambda: None)
    app.watchlist.add(WatchlistItem("AAPL"))
    mock = AsyncMock(side_effect=[[], TossAPIError("offline"), [Price("AAPL", Decimal("100"), "USD", None)]])
    monkeypatch.setattr(app.client, "get_prices", mock)
    try:
        async with app.run_test():
            table = app.query_one(WatchlistTable)
            for i in range(3):
                await app._refresh_prices(table)
                if i < 2:
                    assert app._poll_health.snapshot(3, True)["state"] == "failed"
            snapshot = app._poll_health.snapshot(3, True)
            assert snapshot["poll_count"] == 3
            assert snapshot["success_count"] == 1
            assert snapshot["failure_count"] == 1
            assert snapshot["missing_count"] == 1
            assert snapshot["last_received_utc"] is not None
    finally:
        await app.client.aclose()


def test_no_worker_progress_becomes_stalled_without_false_receive_time(clock):
    health = PollingHealth()
    clock[0] = 31
    snapshot = health.snapshot(3, True)
    assert snapshot["state"] == "stalled"
    assert snapshot["last_received_utc"] is None
    assert snapshot["success_count"] == 0
    assert health.snapshot(3, False)["state"] == "idle"
