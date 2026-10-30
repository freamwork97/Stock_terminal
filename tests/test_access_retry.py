"""Persistent authentication/access failures back off across all API users."""
import asyncio

import httpx
import pytest
import respx

from stock_terminal.access_retry import AccessRetry
from stock_terminal.api.client import TossInvestClient
from stock_terminal.api.exceptions import TossAPIError, TossAuthError, TossForbiddenError
from stock_terminal.config import Settings
from stock_terminal.storage.watchlist import WatchlistItem
from stock_terminal.tui.app import StockTerminalApp

BASE = "https://openapi.tossinvest.com"


@pytest.fixture
def clock(monkeypatch):
    value = [100.0]
    monkeypatch.setattr("stock_terminal.access_retry.monotonic", lambda: value[0])
    return value


def test_backoff_cap_manual_retry_and_recovery(clock):
    retry = AccessRetry()
    for expected in (60, 120, 240, 480, 900, 900):
        retry.fail(TossForbiddenError(), version=retry.version)
        assert retry.wait == expected
        with pytest.raises(TossForbiddenError):
            retry.check()
        clock[0] += expected
    retry.retry_now()
    assert retry.wait == 0
    retry.success(retry.version)
    assert retry.error is None
    assert retry.failures == 0


def test_old_inflight_failures_and_successes_do_not_change_new_state(clock):
    retry = AccessRetry()
    original = retry.version
    retry.fail(TossAuthError("bad"), version=original)
    retry.success(original)
    assert retry.wait == 60
    retry.retry_now()
    current = retry.version
    retry.fail(TossForbiddenError(), version=original)
    assert retry.wait == 0
    retry.success(current)
    assert retry.error is None


@pytest.mark.parametrize("stage,status", [("token",401), ("token",403), ("quote",401), ("quote",403)])
@respx.mock
async def test_repeated_denial_does_not_repeat_http_during_cooldown(clock, stage, status):
    token = respx.post(BASE + "/oauth2/token").respond(status if stage == "token" else 200,
        json={"access_token":"test", "expires_in":3600})
    quote = respx.get(BASE + "/api/v1/prices").respond(status)
    async with TossInvestClient("fake", "fake", BASE) as client:
        with pytest.raises(TossAPIError):
            await client.get_prices(["AAPL"])
        initial = token.call_count + quote.call_count
        assert client.access_retry.wait == 60
        for _ in range(3):
            with pytest.raises(TossAPIError):
                await client.get_candles("AAPL")
        assert token.call_count + quote.call_count == initial
        clock[0] += 60
        with pytest.raises(TossAPIError):
            await client.get_prices(["AAPL"])
        assert client.access_retry.wait == 120
        assert token.call_count + quote.call_count > initial


@respx.mock
async def test_token_failure_shared_by_simultaneous_requests(clock):
    token = respx.post(BASE + "/oauth2/token").respond(401)
    async with TossInvestClient("fake", "fake", BASE) as client:
        results = await asyncio.gather(client.get_prices(["AAPL"]), client.get_stocks(["MSFT"]), return_exceptions=True)
        assert all(isinstance(error, TossAuthError) for error in results)
        assert token.call_count == 1
        assert client.access_retry.wait == 60


@pytest.mark.parametrize("network", [False, True])
@respx.mock
async def test_transient_failures_do_not_trigger_access_cooldown(clock, network):
    respx.post(BASE + "/oauth2/token").respond(200, json={"access_token":"test", "expires_in":3600})
    route = respx.get(BASE + "/api/v1/prices")
    if network:
        route.mock(side_effect=httpx.ConnectError("offline"))
    else:
        route.respond(503)
    async with TossInvestClient("fake", "fake", BASE) as client:
        for _ in range(2):
            with pytest.raises(TossAPIError):
                await client.get_prices(["AAPL"])
        assert route.call_count == 2
        assert client.access_retry.error is None
        assert client.access_retry.wait == 0


@respx.mock
async def test_manual_refresh_bypasses_wait_and_recovers_ui(tmp_path, monkeypatch):
    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    app = StockTerminalApp(Settings("fake", "fake", 0.05, 999))
    monkeypatch.setattr(app, "poll_baselines", lambda:None)
    monkeypatch.setattr(app, "load_chart", lambda:None)
    app.watchlist.add(WatchlistItem("AAPL", currency="USD"))
    denied, recovered = asyncio.Event(), asyncio.Event()
    calls = 0
    respx.post(BASE + "/oauth2/token").respond(200, json={"access_token":"test", "expires_in":3600})
    def quote(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            denied.set()
            return httpx.Response(403)
        recovered.set()
        return httpx.Response(200, json={"result":[{"symbol":"AAPL", "lastPrice":"100", "currency":"USD", "timestamp":None}]})
    route = respx.get(BASE + "/api/v1/prices").mock(side_effect=quote)
    try:
        async with app.run_test() as pilot:
            await asyncio.wait_for(denied.wait(), 2)
            await pilot.pause(0.15)
            assert route.call_count == 1
            assert "허용 IP" in app._monitoring_details()
            assert "다음 자동 재시도" in app._monitoring_details()
            assert app._poll_snapshot()["state"] == "access_wait"
            app.action_refresh_now()
            await asyncio.wait_for(recovered.wait(), 2)
            await pilot.pause()
            assert app.client.access_retry.error is None
            assert "허용 IP" not in app._monitoring_details()
    finally:
        await app.client.aclose()
