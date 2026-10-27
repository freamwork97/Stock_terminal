from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from textual.widgets import Input, Button

from stock_terminal.config import Settings
from stock_terminal.storage.watchlist import WatchlistItem
from stock_terminal.tui.app import StockTerminalApp
from stock_terminal.tui.widgets.watchlist_table import WatchlistTable
from stock_terminal.tui.widgets.chart_panel import ChartPanel
from stock_terminal.tui.screens.add_symbol import AddSymbolScreen, AddSymbolResult
from stock_terminal.api.models import StockInfo


@pytest.fixture
async def app(tmp_path, monkeypatch):
    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    result = StockTerminalApp(Settings("test", "test", 999, 999))
    monkeypatch.setattr(result, "poll_prices", lambda: None)
    monkeypatch.setattr(result, "poll_baselines", lambda: None)
    monkeypatch.setattr(result.client, "get_candles", AsyncMock(return_value=[]))
    yield result
    await result.client.aclose()


async def test_filter_keyboard_and_responsive_layout(app):
    app.watchlist.add(WatchlistItem("AAPL", name="Apple"))
    app.watchlist.add(WatchlistItem("MSFT", name="Microsoft"))
    async with app.run_test(size=(100, 35)) as pilot:
        table = app.query_one(WatchlistTable)
        assert app.query_one("#panels").has_class("narrow")
        await pilot.press("slash", "a", "a", "p", "l", "enter")
        assert table.row_count == 1
        assert table.selected_symbol == "AAPL"
        assert table.has_focus
        await pilot.press("escape")
        assert table.row_count == 2
        await pilot.resize_terminal(180, 40)
        await pilot.pause()
        assert not app.query_one("#panels").has_class("narrow")
        assert table.region.right <= app.query_one(ChartPanel).region.x


async def test_duplicate_add_keeps_alert_settings(app):
    original = WatchlistItem("AAPL", alert_upper=Decimal("210"))
    app.watchlist.add(original)
    async with app.run_test():
        app._on_symbol_added(AddSymbolResult("AAPL", "Apple", None, "USD"))
        assert app.watchlist.items["AAPL"] is original
        assert original.alert_upper == Decimal("210")


async def test_editing_verified_symbol_disables_add(app):
    app.client.get_stocks = AsyncMock(return_value=[StockInfo("AAPL", "Apple", "Apple", "US", "USD", "stock")])
    app.client.get_close_reference = AsyncMock(return_value=None)
    async with app.run_test() as pilot:
        app.push_screen(AddSymbolScreen(app.client))
        await pilot.pause()
        screen = app.screen
        screen.query_one(Input).value = "AAPL"
        await pilot.pause()
        await screen._lookup()
        assert not screen.query_one("#confirm", Button).disabled
        screen.query_one(Input).value = "MSFT"
        await pilot.pause()
        assert screen.query_one("#confirm", Button).disabled
        assert screen._verified is None


async def test_delete_last_symbol_clears_chart(app):
    app.watchlist.add(WatchlistItem("AAPL"))
    async with app.run_test() as pilot:
        app.action_delete_symbol()
        await pilot.pause()
        assert app.query_one(WatchlistTable).row_count == 0
        assert app.query_one(ChartPanel).symbol is None


async def test_manual_refresh_requests_prices_and_chart(app, monkeypatch):
    refresh = Mock()
    monkeypatch.setattr(app, "poll_prices", refresh)
    prices = AsyncMock(return_value=[])
    monkeypatch.setattr(app.client, "get_prices", prices)
    app.watchlist.add(WatchlistItem("AAPL"))
    async with app.run_test() as pilot:
        await pilot.press("r")
        await pilot.pause()
        assert refresh.call_count >= 2  # Startup and manual refresh.
        assert app.client.get_candles.await_count >= 1


async def test_delete_undo_restores_alert_configuration(app, monkeypatch):
    original = WatchlistItem("AAPL", alert_upper=Decimal("210"))
    app.watchlist.add(original)
    monkeypatch.setattr(app.client, "get_prices", AsyncMock(return_value=[]))
    async with app.run_test() as pilot:
        await pilot.press("d")
        assert not app.watchlist.items
        await pilot.press("u")
        assert app.watchlist.items["AAPL"].alert_upper == Decimal("210")
        assert app.query_one(WatchlistTable).row_count == 1


async def test_chart_error_survives_successful_price_refresh(app, monkeypatch):
    from stock_terminal.api.exceptions import TossAPIError
    app.watchlist.add(WatchlistItem("AAPL"))
    monkeypatch.setattr(app.client, "get_candles", AsyncMock(side_effect=TossAPIError("chart offline")))
    monkeypatch.setattr(app.client, "get_prices", AsyncMock(return_value=[]))
    async with app.run_test() as pilot:
        await app.load_chart().wait()
        chart = app.query_one(ChartPanel)
        assert str(chart.border_subtitle) == "chart offline"
        await app._refresh_prices(app.query_one(WatchlistTable))
        assert str(chart.border_subtitle) == "chart offline"
