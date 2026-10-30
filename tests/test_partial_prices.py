"""A corrupt quote must not freeze other symbols in the same response."""
from decimal import Decimal

import httpx
import pytest
import respx

from stock_terminal.api.client import TossInvestClient
from stock_terminal.api.exceptions import TossPartialPricesError
from stock_terminal.api.models import Price
from stock_terminal.config import Settings
from stock_terminal.storage.watchlist import WatchlistItem
from stock_terminal.tui.app import StockTerminalApp
from stock_terminal.tui.widgets.watchlist_table import WatchlistTable

BASE = "https://openapi.tossinvest.com"


@pytest.mark.parametrize("index", [False, True])
@pytest.mark.parametrize("bad_first", [False, True])
@pytest.mark.parametrize("missing_price", [False, True])
@respx.mock
async def test_partial_quote_updates_good_symbol_and_counts_only_bad(tmp_path, monkeypatch, index, bad_first, missing_price):
    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    app = StockTerminalApp(Settings("fake", "fake", 999, 999))
    monkeypatch.setattr(app, "poll_prices", lambda:None)
    monkeypatch.setattr(app, "poll_baselines", lambda:None)
    monkeypatch.setattr(app, "load_chart", lambda:None)
    good, bad = ("KOSPI", "KOSDAQ") if index else ("AAPL", "MSFT")
    currency = "PT" if index else "USD"
    for symbol in (good, bad):
        app.watchlist.add(WatchlistItem(symbol, currency=currency, is_index=index, alert_upper=Decimal("110")))
    good_row = {"symbol":good, "lastPrice":"120", "currency":currency, "timestamp":None}
    bad_row = {"symbol":bad, "lastPrice":"NaN", "currency":currency, "timestamp":None}
    if missing_price: bad_row.pop("lastPrice")
    route = "/api/v1/market-indicators/prices" if index else "/api/v1/prices"
    respx.post(BASE + "/oauth2/token").respond(200, json={"access_token":"test", "expires_in":3600})
    request = respx.get(BASE + route).mock(side_effect=[
        httpx.Response(200, json={"result":[bad_row, good_row] if bad_first else [good_row, bad_row]}),
        httpx.Response(200, json={"result":[good_row, dict(bad_row, lastPrice="105")]})])
    try:
        async with app.run_test() as pilot:
            table = app.query_one(WatchlistTable)
            table.apply_prices({s:Price(s, Decimal("100"), currency, None) for s in (good,bad)}, app.watchlist)
            previous_receive = table._received_at[bad]
            await app._refresh_prices(table)
            await pilot.pause()
            assert "120.00" in str(table.get_cell(good, "price"))
            assert "100.00" in str(table.get_cell(bad, "price"))
            assert str(table.get_cell(good, "status")) == "체결시각 불명"
            assert str(table.get_cell(bad, "status")) == "조회 실패"
            assert table._received_at[bad] == previous_receive
            assert app._poll_health.success == 1
            assert app._poll_health.failure == 1
            assert app._poll_health.missing == 0
            assert [event.symbol for event in table.alerts.history] == [good]
            await app._refresh_prices(table)
            assert "105.00" in str(table.get_cell(bad, "price"))
            assert str(table.get_cell(bad, "status")) == "체결시각 불명"
            assert app._poll_health.success == 3
            assert app._poll_health.failure == 1
            assert request.call_count == 2
    finally:
        await app.client.aclose()


@pytest.mark.parametrize("rows,failed", [
    ([{"symbol":"AAPL", "lastPrice":"100", "timestamp":None, "currency":"USD"}, {}], {"MSFT"}),
    ([{"symbol":"AAPL", "lastPrice":"100", "timestamp":None, "currency":"USD"},
      {"symbol":"MSFT", "lastPrice":"101", "timestamp":None, "currency":"USD"},
      {"symbol":"MSFT", "lastPrice":"102", "timestamp":None, "currency":"USD"}], {"MSFT"}),
])
@respx.mock
async def test_unknown_and_duplicate_rows_preserve_identifiable_good_quotes(rows, failed):
    respx.post(BASE + "/oauth2/token").respond(200, json={"access_token":"test", "expires_in":3600})
    respx.get(BASE + "/api/v1/prices").respond(200, json={"result":rows})
    async with TossInvestClient("fake", "fake", BASE) as client:
        with pytest.raises(TossPartialPricesError) as caught:
            await client.get_prices(["AAPL", "MSFT"])
        assert [price.symbol for price in caught.value.prices] == ["AAPL"]
        assert caught.value.failed_symbols == failed


@respx.mock
async def test_successful_batch_is_preserved_when_following_batch_fails():
    respx.post(BASE + "/oauth2/token").respond(200, json={"access_token":"test", "expires_in":3600})
    symbols = [f"S{i}" for i in range(201)]
    rows = [{"symbol":s, "lastPrice":"100", "currency":"USD", "timestamp":None} for s in symbols[:200]]
    respx.get(BASE + "/api/v1/prices").mock(side_effect=[
        httpx.Response(200, json={"result":rows}), httpx.Response(503)])
    async with TossInvestClient("fake", "fake", BASE) as client:
        with pytest.raises(TossPartialPricesError) as caught:
            await client.get_prices(symbols)
        assert len(caught.value.prices) == 200
        assert caught.value.failed_symbols == {"S200"}


@respx.mock
async def test_invalid_and_missing_quotes_have_separate_counts_and_safe_log(tmp_path, monkeypatch):
    import json
    from stock_terminal.diagnostics import configure_logging, close_logging
    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    app = StockTerminalApp(Settings("fake", "fake", 999, 999))
    app.watchlist.items = {symbol:WatchlistItem(symbol) for symbol in ("AAPL", "MSFT", "AMD")}
    monkeypatch.setattr(app, "_update_summary", lambda:None)
    monkeypatch.setattr(app, "_update_poll_status", lambda:None)
    respx.post(BASE + "/oauth2/token").respond(200, json={"access_token":"PRIVATE_TOKEN", "expires_in":3600})
    respx.get(BASE + "/api/v1/prices").respond(200, json={"result":[
        {"symbol":"AAPL", "lastPrice":"100", "currency":"USD", "timestamp":None},
        {"symbol":"MSFT", "lastPrice":"PRIVATE_BAD_VALUE", "currency":"USD", "timestamp":None}]})
    class Table:
        def apply_prices(self, prices, watchlist): self.good = set(prices)
        def mark_failed(self, symbols): self.bad = set(symbols)
        def mark_missing(self, symbols, received): self.missing = set(symbols) - received
    table = Table()
    try:
        configure_logging(tmp_path / "logs")
        await app._refresh_prices(table)
        assert table.good == {"AAPL"}
        assert table.bad == {"MSFT"}
        assert table.missing == {"AMD"}
        assert (app._poll_health.success, app._poll_health.failure, app._poll_health.missing) == (1,1,1)
        text = (tmp_path / "logs" / "stock-terminal.log").read_text(encoding="utf-8")
        event = json.loads(text.splitlines()[-1])
        assert event["event"] == "operation_failed"
        assert event["count"] == 2
        assert event["failure_count"] == 1
        assert event["missing_count"] == 1
        assert event["error_kind"] == "invalid_response"
        assert "PRIVATE" not in text
    finally:
        await app.client.aclose()
        close_logging()
