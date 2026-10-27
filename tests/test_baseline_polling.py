"""Concurrency and lifecycle checks for independent baseline refresh."""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from stock_terminal.api.exceptions import TossAPIError
from stock_terminal.api.models import CloseReference, Price, StockInfo
from stock_terminal.config import Settings
from stock_terminal.storage.watchlist import WatchlistItem
from stock_terminal.tui.app import StockTerminalApp
from stock_terminal.tui.widgets.watchlist_table import WatchlistTable


@pytest.fixture
async def app(tmp_path, monkeypatch):
    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    result = StockTerminalApp(Settings("test", "test", 0.01, 999))
    monkeypatch.setattr(result, "load_chart", lambda: None)
    yield result
    await result.client.aclose()


def add(app, symbol, currency="USD"):
    item = WatchlistItem(symbol, currency=currency)
    app.watchlist.add(item)
    return item


def reference():
    return CloseReference(Decimal("100"), "2026-10-23", "minute_estimate")


async def test_price_polls_continue_while_baseline_is_blocked(app, monkeypatch):
    add(app, "AAPL")
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    twice = asyncio.Event()
    calls = 0

    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def prices(symbols):
        nonlocal calls
        calls += 1
        if calls >= 2:
            twice.set()
        return [Price("AAPL", Decimal("123"), "USD", None)]

    monkeypatch.setattr(app.client, "get_close_reference", blocked)
    monkeypatch.setattr(app.client, "get_prices", prices)
    async with app.run_test() as pilot:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(twice.wait(), 2)
        await pilot.pause()
        assert "$123.00" in str(app.query_one(WatchlistTable).get_cell("AAPL", "price"))
        assert not cancelled.is_set()
    assert cancelled.is_set()


async def test_concurrency_is_bounded_and_one_failure_does_not_block_others(app, monkeypatch):
    for i in range(8):
        add(app, str(i))
    gate = asyncio.Event()
    saturated = asyncio.Event()
    active = peak = 0
    calls = []

    async def fetch(symbol, *args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(symbol)
        if active == 3:
            saturated.set()
        try:
            await gate.wait()
            if symbol == "0":
                raise TossAPIError("offline")
            return reference()
        finally:
            active -= 1

    monkeypatch.setattr(app.client, "get_close_reference", fetch)
    task = asyncio.create_task(app._refresh_stale_prev_closes(app.watchlist.symbols))
    try:
        await asyncio.wait_for(saturated.wait(), 2)
        assert len(calls) == 3
        gate.set()
        await asyncio.wait_for(task, 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert peak == 3
    assert len(calls) == 8
    assert app.watchlist.items["0"].prev_close is None
    assert all(app.watchlist.items[str(i)].prev_close == Decimal("100") for i in range(1, 8))


async def test_metadata_failure_backoff_and_success_are_per_symbol(app, monkeypatch):
    add(app, "OLD", currency="")
    add(app, "GOOD")
    clock = [100.0]
    monkeypatch.setattr("stock_terminal.tui.app.time.monotonic", lambda: clock[0])
    metadata = AsyncMock(side_effect=[TossAPIError("offline"), [],
        [StockInfo("OLD", "Old", "Old", "US", "USD", "stock")]])
    fetch = AsyncMock(return_value=reference())
    monkeypatch.setattr(app.client, "get_stocks", metadata)
    monkeypatch.setattr(app.client, "get_close_reference", fetch)
    await app._refresh_stale_prev_closes(app.watchlist.symbols)
    assert app._baseline_retry["OLD"][1] == 160
    assert app.watchlist.items["GOOD"].prev_close == Decimal("100")
    clock[0] = 161
    await app._refresh_stale_prev_closes(app.watchlist.symbols)
    assert app._baseline_retry["OLD"][1] == 281
    clock[0] = 280
    await app._refresh_stale_prev_closes(app.watchlist.symbols)
    assert metadata.await_count == 2
    clock[0] = 282
    await app._refresh_stale_prev_closes(app.watchlist.symbols)
    assert metadata.await_count == 3
    assert app.watchlist.items["OLD"].prev_close == Decimal("100")
    assert "OLD" not in app._baseline_failures
    assert fetch.await_count == 2


async def test_cancel_stops_active_and_queued_lookups(app, monkeypatch):
    for i in range(9):
        add(app, str(i))
    saturated = asyncio.Event()
    active = 0
    calls = []

    async def blocked(symbol, *args, **kwargs):
        nonlocal active
        calls.append(symbol)
        active += 1
        if active == 3:
            saturated.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    monkeypatch.setattr(app.client, "get_close_reference", blocked)
    task = asyncio.create_task(app._refresh_stale_prev_closes(app.watchlist.symbols))
    await asyncio.wait_for(saturated.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0
    assert len(calls) == 3
    assert not app._baseline_retry


async def test_deleted_and_readded_symbol_ignores_old_result(app, monkeypatch):
    original = add(app, "AAPL")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return reference()

    monkeypatch.setattr(app.client, "get_close_reference", blocked)
    task = asyncio.create_task(app._refresh_stale_prev_closes(["AAPL"]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        app.watchlist.remove("AAPL")
        replacement = add(app, "AAPL")
        release.set()
        await asyncio.wait_for(task, 2)
        assert replacement is not original
        assert replacement.prev_close is None
        assert "AAPL" not in app._baseline_retry
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_backoff_is_capped_and_market_rollover_bypasses_wait(app, monkeypatch):
    add(app, "AAPL")
    clock = [0.0]
    day = ["2026-10-23"]
    monkeypatch.setattr("stock_terminal.tui.app.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("stock_terminal.tui.app._trading_day_key", lambda _: day[0])
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(app.client, "get_close_reference", fetch)
    for delay in [60, 120, 240, 480, 900, 900]:
        await app._refresh_stale_prev_closes(["AAPL"])
        assert app._baseline_retry["AAPL"][1] - clock[0] == delay
        clock[0] += delay
    await app._refresh_stale_prev_closes(["AAPL"])
    before = fetch.await_count
    day[0] = "2026-10-24"
    await app._refresh_stale_prev_closes(["AAPL"])
    assert fetch.await_count == before + 1
    assert app._baseline_retry["AAPL"][1] - clock[0] == 60


async def test_retry_delay_starts_after_slow_request_finishes(app, monkeypatch):
    add(app, "AAPL")
    clock = [100.0]
    monkeypatch.setattr("stock_terminal.tui.app.time.monotonic", lambda: clock[0])

    async def slow(*args, **kwargs):
        clock[0] = 150.0
        raise TossAPIError("timeout")

    monkeypatch.setattr(app.client, "get_close_reference", slow)
    await app._refresh_stale_prev_closes(["AAPL"])
    assert app._baseline_retry["AAPL"][1] == 210.0


async def test_deleted_queued_symbol_is_not_fetched(app, monkeypatch):
    for i in range(5):
        add(app, str(i))
    gate = asyncio.Event()
    saturated = asyncio.Event()
    calls = []

    async def fetch(symbol, *args, **kwargs):
        calls.append(symbol)
        if len(calls) == 3:
            saturated.set()
        await gate.wait()
        return reference()

    monkeypatch.setattr(app.client, "get_close_reference", fetch)
    task = asyncio.create_task(app._refresh_stale_prev_closes(app.watchlist.symbols))
    try:
        await asyncio.wait_for(saturated.wait(), 2)
        app.watchlist.remove("3")
        gate.set()
        await asyncio.wait_for(task, 2)
        assert "3" not in calls
        assert "4" in calls
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
