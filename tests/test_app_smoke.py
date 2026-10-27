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
