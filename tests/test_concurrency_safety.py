"""Cross-process storage safety, authentication races and independent fetches."""
import asyncio
from pathlib import Path
import subprocess
import sys

import httpx
import pytest
import respx

from stock_terminal.api.client import TossInvestClient
from stock_terminal.api.exceptions import TossAPIError
from stock_terminal.api.models import Price
from stock_terminal.config import Settings
from stock_terminal.storage.watchlist import Watchlist, WatchlistItem, WatchlistError
from stock_terminal.tui.app import StockTerminalApp
from decimal import Decimal

BASE = "https://openapi.tossinvest.com"


def test_stale_instance_cannot_overwrite_newer_watchlist(tmp_path):
    path = tmp_path / "watchlist.json"
    first, second = Watchlist.load(path), Watchlist.load(path)
    first.add(WatchlistItem("AAPL"))
    original, backup = path.read_bytes(), first.backup_path.read_bytes()
    with pytest.raises(WatchlistError, match="다른 실행"):
        second.add(WatchlistItem("MSFT"))
    assert not second.items
    assert path.read_bytes() == original
    assert first.backup_path.read_bytes() == backup
    second = Watchlist.load(path)
    second.add(WatchlistItem("MSFT"))
    assert Watchlist.load(path).symbols == ["AAPL", "MSFT"]
    with pytest.raises(WatchlistError):
        first.set_alerts("AAPL", Decimal("100"), None)
    assert first.items["AAPL"].alert_upper is None


def test_os_lock_blocks_other_process_and_is_released_on_exit(tmp_path):
    path = tmp_path / "watchlist.json"
    Watchlist(path).add(WatchlistItem("AAPL"))
    code = "from pathlib import Path; import sys; from stock_terminal.storage.watchlist import _file_lock; lock=_file_lock(Path(sys.argv[1])); lock.__enter__(); print('locked',flush=True); sys.stdin.readline()"
    child = subprocess.Popen([sys.executable, "-c", code, str(path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(WatchlistError, match="사용 중"):
            Watchlist.load(path)
        child.communicate("exit\n", timeout=10)
        assert child.returncode == 0
        assert Watchlist.load(path).symbols == ["AAPL"]
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()


@pytest.mark.parametrize("token", [None, "", " ", "bad token", "bad\r\nheader", 123, True])
@respx.mock
async def test_invalid_token_never_reaches_quote_request(token):
    respx.post(BASE + "/oauth2/token").respond(200, json={"access_token":token, "expires_in":3600})
    quote = respx.get(BASE + "/api/v1/prices").respond(200, json={"result":[]})
    async with TossInvestClient("id", "secret", BASE) as client:
        with pytest.raises(TossAPIError):
            await client.get_prices(["AAPL"])
        assert client._access_token is None
        assert not quote.called


@pytest.mark.parametrize("expiry", ["NaN", "Infinity", "-Infinity", 0, -1, True, None, "bad"])
@respx.mock
async def test_invalid_token_expiry_is_rejected_without_cache_mutation(expiry):
    respx.post(BASE + "/oauth2/token").respond(200, json={"access_token":"test", "expires_in":expiry})
    async with TossInvestClient("id", "secret", BASE) as client:
        with pytest.raises(TossAPIError):
            await client.get_prices(["AAPL"])
        assert client._access_token is None
        assert client._token_expires_at == 0


@respx.mock
async def test_invalid_token_response_can_recover_and_short_ttl_is_cached():
    route = respx.post(BASE + "/oauth2/token").mock(side_effect=[
        httpx.Response(200, json={"access_token":None, "expires_in":3600}),
        httpx.Response(200, json={"access_token":"good", "expires_in":30})])
    respx.get(BASE + "/api/v1/prices").respond(200, json={"result":[]})
    async with TossInvestClient("id", "secret", BASE) as client:
        with pytest.raises(TossAPIError):
            await client.get_prices(["AAPL"])
        await client.get_prices(["AAPL"])
        await client.get_prices(["AAPL"])
    assert route.call_count == 2


@respx.mock
async def test_simultaneous_401_refreshes_token_once():
    issued, old_requests = 0, 0
    gate = asyncio.Event()
    def token(request):
        nonlocal issued
        issued += 1
        return httpx.Response(200, json={"access_token":f"test-{issued}", "expires_in":3600})
    async def quote(request):
        nonlocal old_requests
        if request.headers["Authorization"] == "Bearer test-1":
            old_requests += 1
            if old_requests == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), 2)
            return httpx.Response(401)
        return httpx.Response(200, json={"result":[]})
    respx.post(BASE + "/oauth2/token").mock(side_effect=token)
    respx.get(BASE + "/api/v1/prices").mock(side_effect=quote)
    async with TossInvestClient("id", "secret", BASE) as client:
        await client._ensure_token()
        await asyncio.gather(client.get_prices(["AAPL"]), client.get_prices(["MSFT"]))
    assert issued == 2  # Initial issuance plus one shared renewal.


@pytest.mark.parametrize("slow_index", [False, True])
async def test_one_slow_market_does_not_block_other_market_update(tmp_path, monkeypatch, slow_index):
    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    app = StockTerminalApp(Settings("id", "secret", 3, 30))
    app.watchlist.items = {"AAPL":WatchlistItem("AAPL"), "KOSPI":WatchlistItem("KOSPI", is_index=True)}
    monkeypatch.setattr(app, "_update_summary", lambda:None)
    monkeypatch.setattr(app, "_update_poll_status", lambda:None)
    release, received = asyncio.Event(), asyncio.Event()
    async def stock(symbols):
        if not slow_index:
            await release.wait()
        return [Price("AAPL", Decimal("100"), "USD", None)]
    async def index(symbols):
        if slow_index:
            await release.wait()
        return [Price("KOSPI", Decimal("2500"), "PT", None)]
    monkeypatch.setattr(app.client, "get_prices", stock)
    monkeypatch.setattr(app.client, "get_market_indicator_prices", index)
    class Table:
        def apply_prices(self, prices, watchlist):
            if ("AAPL" if slow_index else "KOSPI") in prices:
                received.set()
        def mark_missing(self, *args): pass
        def mark_failed(self, *args): pass
    task = asyncio.create_task(app._refresh_prices(Table()))
    try:
        await asyncio.wait_for(received.wait(), 2)
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, 2)
        assert app._poll_health.success == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await app.client.aclose()


async def test_cancelling_poll_stops_both_market_requests(tmp_path, monkeypatch):
    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    app = StockTerminalApp(Settings("id", "secret", 3, 30))
    app.watchlist.items = {"AAPL":WatchlistItem("AAPL"), "KOSPI":WatchlistItem("KOSPI", is_index=True)}
    monkeypatch.setattr(app, "_update_poll_status", lambda:None)
    both_started = asyncio.Event()
    active = 0
    async def blocked(symbols):
        nonlocal active
        active += 1
        if active == 2:
            both_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
    monkeypatch.setattr(app.client, "get_prices", blocked)
    monkeypatch.setattr(app.client, "get_market_indicator_prices", blocked)
    task = asyncio.create_task(app._refresh_prices(object()))
    try:
        await asyncio.wait_for(both_started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert active == 0
        assert app._poll_health.cancelled == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await app.client.aclose()
