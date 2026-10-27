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
        assert not app.query_one("#filter", Input).display
        await pilot.press("slash")
        assert app.query_one("#filter", Input).display
        await pilot.press("a", "a", "p", "l", "enter")
        assert table.row_count == 1
        assert table.selected_symbol == "AAPL"
        assert table.has_focus
        await pilot.press("escape")
        assert table.row_count == 2
        assert not app.query_one("#filter", Input).display
        await pilot.press("slash")
        assert app.query_one("#filter", Input).display
        assert app.query_one("#filter", Input).value == ""
        await pilot.press("escape")
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


async def test_unified_search_looks_up_and_adds_missing_symbol(app, monkeypatch):
    stocks = AsyncMock(return_value=[StockInfo("MSFT", "Microsoft", "Microsoft", "US", "USD", "stock")])
    monkeypatch.setattr(app.client, "get_stocks", stocks)
    monkeypatch.setattr(app.client, "get_prices", AsyncMock(return_value=[]))
    async with app.run_test() as pilot:
        await pilot.press("a")
        assert app.query_one("#filter", Input).display
        await pilot.press("m", "s", "f", "t", "enter")
        await pilot.pause()
        stocks.assert_awaited_once_with(["MSFT"])
        assert not app.query_one("#search-add", Button).disabled
        assert "MSFT" not in app.watchlist.items
        await pilot.click("#search-add")
        assert "MSFT" in app.watchlist.items
        assert not app.query_one("#filter", Input).display
        assert app.query_one(WatchlistTable).selected_symbol == "MSFT"


async def test_unified_search_edit_invalidates_verified_result(app, monkeypatch):
    monkeypatch.setattr(app.client, "get_stocks", AsyncMock(return_value=[StockInfo("MSFT", "Microsoft", "Microsoft", "US", "USD", "stock")]))
    async with app.run_test() as pilot:
        await pilot.press("slash", "m", "s", "f", "t", "enter")
        await pilot.pause()
        assert not app.query_one("#search-add", Button).disabled
        app.query_one("#filter", Input).value = "AAPL"
        await pilot.pause()
        assert app.query_one("#search-add", Button).disabled
        assert app._search_result is None
        assert not app.watchlist.items


async def test_unified_search_existing_match_does_not_call_api(app, monkeypatch):
    app.watchlist.add(WatchlistItem("AAPL", name="Apple"))
    stocks = AsyncMock()
    monkeypatch.setattr(app.client, "get_stocks", stocks)
    async with app.run_test() as pilot:
        await pilot.press("a", "a", "p", "p", "l", "e", "enter")
        assert app.query_one(WatchlistTable).has_focus
        assert app.query_one(WatchlistTable).selected_symbol == "AAPL"
        stocks.assert_not_awaited()


async def test_reordering_same_symbol_does_not_reload_chart(app, monkeypatch):
    from stock_terminal.api.models import Price
    from stock_terminal.api.client import trading_day_key
    for symbol in ("AAPL", "MSFT"):
        app.watchlist.add(WatchlistItem(symbol, currency="USD", prev_close=Decimal("100"),
            prev_close_source="minute_estimate", prev_close_date=trading_day_key("USD")))
    async with app.run_test() as pilot:
        await pilot.pause()
        count = app.client.get_candles.await_count
        table = app.query_one(WatchlistTable)
        table.sort_mode = "등락률순"
        table.apply_prices({"MSFT": Price("MSFT", Decimal("120"), "USD", None)}, app.watchlist)
        await pilot.pause()
        assert table.selected_symbol == "AAPL"
        assert app.client.get_candles.await_count == count


async def test_chart_refresh_retains_data_during_wait_and_failure(app, monkeypatch):
    import asyncio
    from datetime import datetime, timezone
    from stock_terminal.api.models import Candle
    from stock_terminal.api.exceptions import TossAPIError
    app.watchlist.add(WatchlistItem("AAPL"))
    async with app.run_test() as pilot:
        await pilot.pause()
        chart = app.query_one(ChartPanel)
        candles = [Candle(datetime.now(timezone.utc), *(Decimal("100"),)*5)]
        chart.show_candles("AAPL", candles)
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await release.wait()
            raise TossAPIError("offline")
        monkeypatch.setattr(app.client, "get_candles", blocked)
        worker = app.load_chart()
        await asyncio.wait_for(entered.wait(), 2)
        assert chart._candles == candles
        assert chart.symbol == "AAPL"
        release.set()
        await worker.wait()
        assert chart._candles == candles
        assert str(chart.border_subtitle) == "offline"


async def test_keyboard_only_lookup_confirm_add(app, monkeypatch):
    monkeypatch.setattr(app.client, "get_stocks", AsyncMock(return_value=[StockInfo("MSFT", "Microsoft", "Microsoft", "US", "USD", "stock")]))
    monkeypatch.setattr(app.client, "get_prices", AsyncMock(return_value=[]))
    async with app.run_test() as pilot:
        await pilot.press("slash", "m", "s", "f", "t", "enter")
        await pilot.pause()
        assert app.query_one("#search-add", Button).has_focus
        assert not app.watchlist.items
        await pilot.press("enter")
        assert "MSFT" in app.watchlist.items
        assert app.query_one(WatchlistTable).has_focus
        assert not app.query_one("#filter", Input).display


async def test_ctrl_enter_looks_up_even_when_partial_local_match_exists(app, monkeypatch):
    app.watchlist.add(WatchlistItem("AAPL"))
    stocks = AsyncMock(return_value=[StockInfo("AA", "Alcoa", "Alcoa", "US", "USD", "stock")])
    monkeypatch.setattr(app.client, "get_stocks", stocks)
    async with app.run_test() as pilot:
        await pilot.press("slash", "a", "a", "ctrl+enter")
        await pilot.pause()
        stocks.assert_awaited_once_with(["AA"])
        assert app.query_one("#search-add", Button).has_focus


async def test_keyboard_lookup_failure_retry_and_escape(app, monkeypatch):
    from stock_terminal.api.exceptions import TossAPIError
    stocks = AsyncMock(side_effect=[TossAPIError("offline"),
        [StockInfo("AA", "Alcoa", "Alcoa", "US", "USD", "stock")]])
    monkeypatch.setattr(app.client, "get_stocks", stocks)
    async with app.run_test() as pilot:
        await pilot.press("slash", "a", "a", "enter")
        await pilot.pause()
        assert app.query_one("#filter", Input).has_focus
        assert app.query_one("#search-add", Button).disabled
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#search-add", Button).has_focus
        await pilot.press("escape")
        assert not app.query_one("#filter", Input).display
        assert not app.watchlist.items
