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


async def test_freshness_failure_missing_and_recovery(watchlist, monkeypatch):
    app = _HarnessApp()
    async with app.run_test():
        table = app.query_one(WatchlistTable)
        table.sync_rows(watchlist)
        assert str(table.get_cell("AAPL", "status")) == "대기"
        price = Price("AAPL", Decimal("205"), "USD", datetime.now(timezone.utc))
        table.apply_prices({"AAPL": price}, watchlist)
        assert str(table.get_cell("AAPL", "status")) == "정상"
        received = table.get_cell("AAPL", "received")
        assert received != "-"
        table.mark_missing(["AAPL"], set())
        assert str(table.get_cell("AAPL", "status")) == "응답 누락"
        table.mark_failed(["AAPL"])
        assert str(table.get_cell("AAPL", "status")) == "조회 실패"
        assert table.get_cell("AAPL", "received") == received
        assert "$205.00" in str(table.get_cell("AAPL", "price"))
        table.apply_prices({"AAPL": price}, watchlist)
        assert str(table.get_cell("AAPL", "status")) == "정상"
        table._received_at["AAPL"] -= 31
        table.refresh_freshness()
        assert str(table.get_cell("AAPL", "status")) == "수신 지연"
        table.mark_failed(["AAPL"])
        table.refresh_freshness()
        assert str(table.get_cell("AAPL", "status")) == "조회 실패"
        del watchlist.items["AAPL"]
        table.sync_rows(watchlist)
        assert "AAPL" not in table._received_at
        table.refresh_freshness()


@pytest.mark.parametrize("age, status", [(60, "체결 오래됨"), (-60, "체결시각 이상"), (None, "체결시각 불명")])
async def test_trade_age_is_separate_from_receive_age(watchlist, age, status):
    from datetime import timedelta
    app = _HarnessApp()
    async with app.run_test():
        table = app.query_one(WatchlistTable)
        table.sync_rows(watchlist)
        stamp = datetime.now(timezone.utc) - timedelta(seconds=age) if age is not None else None
        table.apply_prices({"AAPL": Price("AAPL", Decimal("205"), "USD", stamp)}, watchlist)
        assert str(table.get_cell("AAPL", "status")) == status
        assert table.get_cell("AAPL", "received") != "-"
        table.refresh_freshness()
        assert str(table.get_cell("AAPL", "status")) == status
