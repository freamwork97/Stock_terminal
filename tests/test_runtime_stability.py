"""Accelerated lifecycle/soak checks; these do not replace an overnight live run."""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from stock_terminal.api.models import Price
from stock_terminal.config import Settings
from stock_terminal.storage.watchlist import WatchlistItem
from stock_terminal.tui.app import StockTerminalApp
from stock_terminal.tui.widgets.watchlist_table import WatchlistTable


@pytest.fixture
async def app(tmp_path, monkeypatch):
    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    result = StockTerminalApp(Settings("test", "test", 999, 999))
    monkeypatch.setattr(result, "poll_prices", lambda: None)
    monkeypatch.setattr(result, "poll_baselines", lambda: None)
    monkeypatch.setattr(result, "load_chart", lambda: None)
    yield result
    await result.client.aclose()


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("failure", [False, True])
async def test_old_price_response_cannot_update_readded_symbol(app, monkeypatch, single, failure):
    app.watchlist.add(WatchlistItem("AAPL"))
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(symbols):
        entered.set()
        await release.wait()
        if failure:
            from stock_terminal.api.exceptions import TossAPIError
            raise TossAPIError("old request failed")
        return [Price("AAPL", Decimal("999"), "USD", None)]
    monkeypatch.setattr(app.client, "get_prices", blocked)
    async with app.run_test() as pilot:
        table = app.query_one(WatchlistTable)
        if single:
            worker = app._refresh_one("AAPL")
            task = asyncio.create_task(worker.wait())
        else:
            task = asyncio.create_task(app._refresh_prices(table))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            app.watchlist.remove("AAPL")
            table.sync_rows(app.watchlist)
            app.watchlist.add(WatchlistItem("AAPL"))
            table.sync_rows(app.watchlist)
            release.set()
            await asyncio.wait_for(task, 2)
            assert str(table.get_cell("AAPL", "price")) == "-"
            assert str(table.get_cell("AAPL", "status")) == "대기"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_accelerated_1000_refreshes_recover_and_keep_state_bounded(app, monkeypatch):
    from stock_terminal.api.exceptions import TossAPIError
    from stock_terminal.api.client import trading_day_key
    clock = [0.0]
    monkeypatch.setattr("stock_terminal.alerts.monotonic", lambda: clock[0])
    for symbol in ("AAPL", "MSFT", "AMD"):
        app.watchlist.add(WatchlistItem(symbol, currency="USD", prev_close=Decimal("100"),
            prev_close_source="minute_estimate", prev_close_date=trading_day_key("USD"),
            alert_upper=Decimal("110")))
    cycle = [0]
    async def prices(symbols):
        i = cycle[0]
        if i % 13 == 0:
            raise TossAPIError("simulated connection loss")
        if i % 17 == 0:
            return []
        return [Price(symbol, Decimal("120" if i % 2 else "100"), "USD", None) for symbol in symbols]
    monkeypatch.setattr(app.client, "get_prices", prices)
    monkeypatch.setattr(app, "notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "bell", lambda: None)
    async with app.run_test() as pilot:
        table = app.query_one(WatchlistTable)
        table.sort_mode = "등락률순"
        for i in range(1000):
            cycle[0] = i
            clock[0] = i * 61
            if i % 25 == 0:
                table.filter_text = "AAPL" if i % 50 == 0 else ""
                table.sync_rows(app.watchlist)
            await app._refresh_prices(table)
            if i % 50 == 0:
                await asyncio.sleep(0)
        await pilot.pause()
        assert len(table.alerts.history) == 100
        assert len(table._cached_rows) == 3
        assert len(table._received_at) == 3
        assert len(table.alerts._states) == 3
        assert len(app.workers) <= 2
        assert all(table._states[symbol] == "체결시각 불명" for symbol in app.watchlist.symbols)
        assert "$120.00" in str(table._cached_rows["AAPL"]["price"])


@pytest.mark.parametrize("currency,zone,before,after", [
    ("KRW", "Asia/Seoul", "2026-10-27T15:29:59", "2026-10-27T15:30:00"),
    ("USD", "America/New_York", "2026-10-27T15:59:59", "2026-10-27T16:00:00"),
    ("USD", "America/New_York", "2026-11-03T15:59:59", "2026-11-03T16:00:00"),
])
def test_market_close_rollover_across_dst(monkeypatch, currency, zone, before, after):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from stock_terminal.api.client import trading_day_key
    current = [datetime.fromisoformat(before).replace(tzinfo=ZoneInfo(zone))]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz)
    monkeypatch.setattr("stock_terminal.api.client.datetime", Clock)
    assert trading_day_key(currency) == (current[0].date() - timedelta(days=1)).isoformat()
    current[0] = datetime.fromisoformat(after).replace(tzinfo=ZoneInfo(zone))
    assert trading_day_key(currency) == current[0].date().isoformat()


async def test_expired_token_is_renewed_after_simulated_sleep(monkeypatch):
    from types import SimpleNamespace
    import httpx
    import respx
    from stock_terminal.api.client import TossInvestClient
    clock = [100.0]
    monkeypatch.setattr("stock_terminal.api.client.time", SimpleNamespace(monotonic=lambda: clock[0]))
    base = "https://openapi.tossinvest.com"
    with respx.mock:
        token = respx.post(base + "/oauth2/token").mock(side_effect=[
            httpx.Response(200, json={"access_token": "first", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "renewed", "expires_in": 3600})])
        quotes = respx.get(base + "/api/v1/prices").respond(200, json={"result": []})
        async with TossInvestClient("id", "secret", base) as client:
            await client.get_prices(["AAPL"])
            clock[0] += 86400
            await client.get_prices(["AAPL"])
        assert token.call_count == 2
        assert quotes.calls[-1].request.headers["Authorization"] == "Bearer renewed"


async def test_cached_baseline_refreshes_after_day_jump(app, monkeypatch):
    from types import SimpleNamespace
    from stock_terminal.api.models import CloseReference
    day = ["2026-10-26"]
    clock = [100.0]
    monkeypatch.setattr("stock_terminal.tui.app.time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr("stock_terminal.tui.app._trading_day_key", lambda _: day[0])
    app.watchlist.add(WatchlistItem("AAPL", currency="USD", prev_close=Decimal("100"),
        prev_close_date=day[0], prev_close_session="2026-10-23", prev_close_source="minute_estimate"))
    fetch = AsyncMock(return_value=CloseReference(Decimal("110"), "2026-10-26", "minute_estimate"))
    monkeypatch.setattr(app.client, "get_close_reference", fetch)
    await app._refresh_stale_prev_closes(["AAPL"])
    fetch.assert_not_awaited()
    clock[0] += 86400
    day[0] = "2026-10-27"
    await app._refresh_stale_prev_closes(["AAPL"])
    fetch.assert_awaited_once()
    assert app.watchlist.items["AAPL"].prev_close == Decimal("110")
    assert app.watchlist.items["AAPL"].prev_close_date == "2026-10-27"


async def test_old_chart_response_cannot_update_readded_symbol(app, monkeypatch):
    from datetime import datetime, timezone
    from stock_terminal.api.models import Candle
    from stock_terminal.tui.widgets.chart_panel import ChartPanel
    app.watchlist.add(WatchlistItem("AAPL"))
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return [Candle(datetime.now(timezone.utc), *(Decimal("100"),)*5)]
    monkeypatch.setattr(app.client, "get_candles", blocked)
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "load_chart", StockTerminalApp.load_chart.__get__(app))
        worker = app.load_chart()
        await asyncio.wait_for(entered.wait(), 2)
        app.watchlist.remove("AAPL")
        app.watchlist.add(WatchlistItem("AAPL"))
        release.set()
        await worker.wait()
        chart = app.query_one(ChartPanel)
        assert chart.symbol is None
        assert not chart._candles
