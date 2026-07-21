"""Widget-level tests for WatchlistTable, run inside a minimal headless App."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from textual.app import App, ComposeResult

from stock_terminal.api.models import Price
from stock_terminal.storage.watchlist import Watchlist, WatchlistItem
from stock_terminal.tui.widgets.watchlist_table import WatchlistTable


class _HarnessApp(App):
    def compose(self) -> ComposeResult:
        yield WatchlistTable()


@pytest.fixture
def watchlist() -> Watchlist:
    wl = Watchlist(path=None)  # type: ignore[arg-type]
    wl.items["005930"] = WatchlistItem(
        symbol="005930", name="삼성전자", prev_close=Decimal("70000")
    )
    wl.items["AAPL"] = WatchlistItem(
        symbol="AAPL",
        name="Apple",
        prev_close=Decimal("200"),
        alert_upper=Decimal("210"),
    )
    return wl


async def test_sync_rows_and_selected_symbol(watchlist: Watchlist) -> None:
    app = _HarnessApp()
    async with app.run_test() as pilot:
        table = app.query_one(WatchlistTable)
        table.sync_rows(watchlist)

        assert table.row_count == 2
        first = table.selected_symbol
        assert first in {"005930", "AAPL"}

        await pilot.press("down")
        second = table.selected_symbol
        assert {first, second} == {"005930", "AAPL"}


async def test_apply_prices_updates_cells_and_detects_alert(
    watchlist: Watchlist,
) -> None:
    app = _HarnessApp()
    async with app.run_test():
        table = app.query_one(WatchlistTable)
        table.sync_rows(watchlist)

        prices = {
            "005930": Price(
                symbol="005930",
                last_price=Decimal("71000"),
                currency="KRW",
                timestamp=datetime.now(timezone.utc),
            ),
            "AAPL": Price(
                symbol="AAPL",
                last_price=Decimal("215"),
                currency="USD",
                timestamp=datetime.now(timezone.utc),
            ),
        }

        triggered = table.apply_prices(prices, watchlist)

        assert triggered == ["AAPL"]  # crossed alert_upper=210

        price_cell = table.get_cell("005930", "price")
        assert "71,000" in price_cell.plain


async def test_sync_rows_removes_deleted_symbol(watchlist: Watchlist) -> None:
    app = _HarnessApp()
    async with app.run_test():
        table = app.query_one(WatchlistTable)
        table.sync_rows(watchlist)
        assert table.row_count == 2

        del watchlist.items["AAPL"]
        table.sync_rows(watchlist)

        assert table.row_count == 1
        assert "005930" in table.rows
        assert "AAPL" not in table.rows
