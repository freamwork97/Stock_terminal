"""Headless smoke test: the app mounts, shows widgets, and quits cleanly.

Uses an empty watchlist so the polling workers take their early-return path
(no symbols -> no network calls), keeping this test fully offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stock_terminal.config import Settings
from stock_terminal.tui.app import StockTerminalApp
from stock_terminal.tui.widgets.chart_panel import ChartPanel
from stock_terminal.tui.widgets.watchlist_table import WatchlistTable


@pytest.fixture(autouse=True)
def disable_background_baselines(monkeypatch):
    # These tests exercise other workers or invoke baseline refresh explicitly.
    monkeypatch.setattr(StockTerminalApp, "poll_baselines", lambda self: None)


@pytest.fixture
def fake_settings() -> Settings:
    return Settings(
        client_id="fake-id",
        client_secret="fake-secret",
        poll_interval_seconds=999,
        chart_refresh_seconds=999,
    )


async def test_app_mounts_and_quits(
    fake_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json"
    )

    app = StockTerminalApp(fake_settings)
    try:
        async with app.run_test() as pilot:
            assert app.query_one(WatchlistTable) is not None
            assert app.query_one(ChartPanel) is not None
            await pilot.press("q")
    finally:
        await app.client.aclose()


@pytest.mark.parametrize("failure", ["timeout", "connection", "malformed", "server"])
async def test_polling_recovers_after_failure(fake_settings, tmp_path, monkeypatch, failure):
    import asyncio
    from dataclasses import replace
    from decimal import Decimal
    import httpx
    import respx
    from stock_terminal.api.client import trading_day_key
    from stock_terminal.storage.watchlist import WatchlistItem

    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    app = StockTerminalApp(replace(fake_settings, poll_interval_seconds=0.02))
    app.watchlist.add(WatchlistItem(symbol="AAPL", currency="USD",
        prev_close=Decimal("100"), prev_close_date=trading_day_key("USD")))
    recovered = asyncio.Event()
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("offline", request=request)
            if failure == "connection":
                raise httpx.ConnectError("offline", request=request)
            if failure == "server":
                return httpx.Response(503)
            return httpx.Response(200, json={"result": [{}]})
        recovered.set()
        return httpx.Response(200, json={"result": [{"symbol": "AAPL",
            "lastPrice": "123", "currency": "USD", "timestamp": None}]})

    try:
        with respx.mock:
            respx.post("https://openapi.tossinvest.com/oauth2/token").respond(
                200, json={"access_token": "test", "expires_in": 3600})
            respx.get("https://openapi.tossinvest.com/api/v1/prices").mock(side_effect=respond)
            respx.get("https://openapi.tossinvest.com/api/v1/candles").respond(
                200, json={"result": {"candles": []}})
            async with app.run_test() as pilot:
                await asyncio.wait_for(recovered.wait(), timeout=3)
                await pilot.pause()
                cell = app.query_one(WatchlistTable).get_cell("AAPL", "price")
                assert "$123.00" in str(cell)
                assert calls >= 2
                assert app.sub_title == ""
    finally:
        await app.client.aclose()


async def test_partial_market_failure_and_missing_response(fake_settings, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from datetime import datetime, timezone
    from decimal import Decimal
    from stock_terminal.api.exceptions import TossAPIError
    from stock_terminal.api.models import Price
    from stock_terminal.storage.watchlist import WatchlistItem

    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    app = StockTerminalApp(fake_settings)
    monkeypatch.setattr(app, "poll_prices", lambda: None)
    monkeypatch.setattr(app, "load_chart", lambda: None)
    monkeypatch.setattr(app, "_refresh_stale_prev_closes", AsyncMock())
    for symbol in ["AAPL", "KOSPI"]:
        app.watchlist.add(WatchlistItem(symbol=symbol, is_index=symbol == "KOSPI"))
    monkeypatch.setattr(app.client, "get_prices", AsyncMock(side_effect=TossAPIError("offline")))
    monkeypatch.setattr(app.client, "get_market_indicator_prices", AsyncMock(return_value=[
        Price("KOSPI", Decimal("2500"), "PT", datetime.now(timezone.utc))]))
    try:
        async with app.run_test():
            table = app.query_one(WatchlistTable)
            await app._refresh_prices(table)
            assert str(table.get_cell("AAPL", "status")) == "조회 실패"
            assert str(table.get_cell("KOSPI", "status")) == "정상"
            monkeypatch.setattr(app.client, "get_prices", AsyncMock(return_value=[]))
            await app._refresh_prices(table)
            assert str(table.get_cell("AAPL", "status")) == "응답 누락"
            monkeypatch.setattr(app.client, "get_prices", AsyncMock(return_value=[
                Price("AAPL", Decimal("123"), "USD", datetime.now(timezone.utc))]))
            await app._refresh_prices(table)
            assert str(table.get_cell("AAPL", "status")) == "정상"
    finally:
        await app.client.aclose()


@pytest.mark.parametrize("first", ["none", "error", "fallback"])
async def test_baseline_retries_without_marking_failure_complete(fake_settings, tmp_path, monkeypatch, first):
    from unittest.mock import AsyncMock
    from decimal import Decimal
    from stock_terminal.api.models import CloseReference
    from stock_terminal.api.exceptions import TossAPIError
    from stock_terminal.api.client import trading_day_key
    from stock_terminal.storage.watchlist import WatchlistItem

    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    clock = [100.0]
    monkeypatch.setattr("stock_terminal.tui.app.time.monotonic", lambda: clock[0])
    app = StockTerminalApp(fake_settings)
    app.watchlist.add(WatchlistItem("AAPL", currency="USD", prev_close=Decimal("90"),
        prev_close_date="2000-01-01"))
    initial = {"none": None, "error": TossAPIError("offline"),
        "fallback": CloseReference(Decimal("100"), "2026-10-23", "daily_fallback")}[first]
    fetch = AsyncMock(side_effect=[initial, CloseReference(Decimal("101"), "2026-10-23", "minute_estimate")])
    monkeypatch.setattr(app.client, "get_close_reference", fetch)
    try:
        await app._refresh_stale_prev_closes(["AAPL"])
        if first != "fallback":
            assert app.watchlist.items["AAPL"].prev_close_date == "2000-01-01"
            assert app.watchlist.items["AAPL"].prev_close == Decimal("90")
        await app._refresh_stale_prev_closes(["AAPL"])
        assert fetch.await_count == 1
        clock[0] += 61
        await app._refresh_stale_prev_closes(["AAPL"])
        assert fetch.await_count == 2
        item = app.watchlist.items["AAPL"]
        assert item.prev_close == Decimal("101")
        assert item.prev_close_date == trading_day_key("USD")
        assert item.prev_close_source == "minute_estimate"
        await app._refresh_stale_prev_closes(["AAPL"])
        assert fetch.await_count == 2
    finally:
        await app.client.aclose()


async def test_alert_notifications_history_and_settings(fake_settings, tmp_path, monkeypatch):
    from decimal import Decimal
    from unittest.mock import Mock
    from stock_terminal.api.models import Price
    from stock_terminal.storage.watchlist import WatchlistItem
    from stock_terminal.tui.screens.alert_history import AlertHistoryScreen
    from stock_terminal.tui.screens.set_alert import AlertResult
    from textual.widgets import DataTable

    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    app = StockTerminalApp(fake_settings)
    monkeypatch.setattr(app, "poll_prices", lambda: None)
    monkeypatch.setattr(app, "load_chart", lambda: None)
    bell = Mock()
    monkeypatch.setattr(app, "bell", bell)
    app.watchlist.add(WatchlistItem("AAPL", alert_upper=Decimal("210")))
    try:
        async with app.run_test() as pilot:
            table = app.query_one(WatchlistTable)
            prices = {"AAPL": Price("AAPL", Decimal("215"), "USD", None)}
            table.apply_prices(prices, app.watchlist)
            table.apply_prices(prices, app.watchlist)
            await pilot.pause()
            assert bell.call_count == 1
            app._on_alert_set("AAPL", AlertResult(Decimal("210"), None))
            table.apply_prices(prices, app.watchlist)
            await pilot.pause()
            assert bell.call_count == 1
            app._on_alert_set("AAPL", AlertResult(Decimal("211"), None))
            table.apply_prices(prices, app.watchlist)
            await pilot.pause()
            assert bell.call_count == 2
            await pilot.press("h")
            assert isinstance(app.screen, AlertHistoryScreen)
            assert app.screen.query_one(DataTable).row_count == 2
            await pilot.press("escape")
            assert not isinstance(app.screen, AlertHistoryScreen)
    finally:
        await app.client.aclose()


async def test_initial_refresh_alert_is_delivered_once(fake_settings, tmp_path, monkeypatch):
    from decimal import Decimal
    from unittest.mock import AsyncMock, Mock
    from stock_terminal.api.models import Price
    from stock_terminal.storage.watchlist import WatchlistItem

    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    app = StockTerminalApp(fake_settings)
    monkeypatch.setattr(app, "poll_prices", lambda: None)
    monkeypatch.setattr(app, "load_chart", lambda: None)
    monkeypatch.setattr(app.client, "get_prices", AsyncMock(return_value=[Price("AAPL", Decimal("215"), "USD", None)]))
    bell = Mock()
    monkeypatch.setattr(app, "bell", bell)
    app.watchlist.add(WatchlistItem("AAPL", alert_upper=Decimal("210")))
    try:
        async with app.run_test() as pilot:
            await app._refresh_one("AAPL").wait()
            await pilot.pause()
            assert bell.call_count == 1
            await app._refresh_one("AAPL").wait()
            await pilot.pause()
            assert bell.call_count == 1
    finally:
        await app.client.aclose()


@pytest.mark.parametrize("invalid", ["NaN", "sNaN", "Infinity", "-1", "0"])
async def test_alert_dialog_rejects_invalid_prices(fake_settings, tmp_path, monkeypatch, invalid):
    from textual.widgets import Input, Label
    from stock_terminal.tui.screens.set_alert import SetAlertScreen

    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    app = StockTerminalApp(fake_settings)
    try:
        async with app.run_test() as pilot:
            app.push_screen(SetAlertScreen("AAPL", None, None))
            await pilot.pause()
            app.screen.query_one("#upper-input", Input).value = invalid
            await pilot.click("#confirm")
            assert isinstance(app.screen, SetAlertScreen)
            assert "유한한 양수" in str(app.screen.query_one("#status", Label).render())
    finally:
        await app.client.aclose()


async def test_empty_alert_history_opens_and_closes(fake_settings, tmp_path, monkeypatch):
    from stock_terminal.tui.screens.alert_history import AlertHistoryScreen
    from textual.widgets import DataTable

    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    app = StockTerminalApp(fake_settings)
    try:
        async with app.run_test() as pilot:
            await pilot.press("h")
            assert isinstance(app.screen, AlertHistoryScreen)
            assert app.screen.query_one(DataTable).row_count == 0
            await pilot.click("#close-history")
            assert not isinstance(app.screen, AlertHistoryScreen)
    finally:
        await app.client.aclose()
