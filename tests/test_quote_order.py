"""Late-response rejection for initial and recurring quote requests."""
import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from stock_terminal.api.models import Price
from stock_terminal.api.exceptions import TossAPIError
from stock_terminal.config import Settings
from stock_terminal.quote_order import QuoteOrder
from stock_terminal.storage.watchlist import WatchlistItem
from stock_terminal.tui.app import StockTerminalApp
from stock_terminal.tui.widgets.watchlist_table import WatchlistTable


@pytest.mark.parametrize("old_single", [True, False])
@pytest.mark.parametrize("stamp_kind", ["older", "equal", "missing", "naive"])
async def test_late_response_cannot_overwrite_price_time_or_alerts(tmp_path, monkeypatch, old_single, stamp_kind):
    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    app = StockTerminalApp(Settings("fake", "fake", 999, 999))
    monkeypatch.setattr(app, "poll_prices", lambda:None)
    monkeypatch.setattr(app, "poll_baselines", lambda:None)
    monkeypatch.setattr(app, "load_chart", lambda:None)
    app.watchlist.add(WatchlistItem("TEST", currency="USD", alert_upper=Decimal("110")))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    now = datetime.now(timezone.utc)
    old_stamp = {"older":now-timedelta(minutes=1), "equal":now,
                 "missing":None, "naive":now.replace(tzinfo=None)}[stamp_kind]
    async def quotes(symbols):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return [Price("TEST", Decimal("120"), "USD", old_stamp)]
        return [Price("TEST", Decimal("100"), "USD", now)]
    monkeypatch.setattr(app.client, "get_prices", quotes)
    try:
        async with app.run_test() as pilot:
            table = app.query_one(WatchlistTable)
            task = asyncio.create_task(app._refresh_one("TEST").wait() if old_single else app._refresh_prices(table))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                if old_single:
                    await app._refresh_prices(table)
                else:
                    await app._refresh_one("TEST").wait()
                received, updated = table.get_cell("TEST", "received"), table.get_cell("TEST", "updated")
                receive_clock = table._received_at["TEST"]
                release.set()
                await asyncio.wait_for(task, 2)
                await pilot.pause()
                assert str(table.get_cell("TEST", "price")) == "$100.00"
                assert table.get_cell("TEST", "received") == received
                assert table.get_cell("TEST", "updated") == updated
                assert table._received_at["TEST"] == receive_clock
                assert not table.alerts.history
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    finally:
        await app.client.aclose()


def test_newer_request_with_older_trade_is_rejected_and_missing_keeps_watermark():
    order = QuoteOrder()
    item = WatchlistItem("TEST")
    items = {"TEST":item}
    now = datetime.now(timezone.utc)
    def price(stamp): return Price("TEST", Decimal("100"), "USD", stamp)
    assert order.accept(price(now), item, order.begin(items))
    assert not order.accept(price(now-timedelta(minutes=1)), item, order.begin(items))
    assert order.accept(price(None), item, order.begin(items))
    assert not order.accept(price(now-timedelta(seconds=1)), item, order.begin(items))
    assert order.accept(price(now), item, order.begin(items))
    assert order.accept(price(now+timedelta(seconds=1)), item, order.begin(items))


def test_request_order_used_without_trade_time_and_removed_state_is_pruned():
    order = QuoteOrder()
    item = WatchlistItem("TEST")
    items = {"TEST":item}
    first, second = order.begin(items), order.begin(items)
    price = Price("TEST", Decimal("100"), "USD", None)
    assert order.accept(price, item, second)
    assert not order.accept(price, item, first)
    replacement = WatchlistItem("TEST")
    request = order.begin({"TEST":replacement})
    assert not order._accepted
    assert order.accept(price, replacement, request)
    order.begin({})
    assert not order._accepted


@pytest.mark.parametrize("missing", [False, True])
async def test_old_error_or_missing_response_cannot_change_new_success(tmp_path, monkeypatch, missing):
    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    app = StockTerminalApp(Settings("fake", "fake", 999, 999))
    monkeypatch.setattr(app, "poll_prices", lambda:None)
    monkeypatch.setattr(app, "poll_baselines", lambda:None)
    monkeypatch.setattr(app, "load_chart", lambda:None)
    app.watchlist.add(WatchlistItem("TEST", currency="USD"))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def quotes(symbols):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            if missing: return []
            raise TossAPIError("old failure")
        return [Price("TEST", Decimal("100"), "USD", datetime.now(timezone.utc))]
    monkeypatch.setattr(app.client, "get_prices", quotes)
    try:
        async with app.run_test():
            table = app.query_one(WatchlistTable)
            worker = app._refresh_one("TEST")
            await asyncio.wait_for(entered.wait(), 2)
            await app._refresh_prices(table)
            release.set()
            await worker.wait()
            assert str(table.get_cell("TEST", "status")) == "정상"
    finally:
        await app.client.aclose()
