"""Tests for TossInvestClient auth/retry/rate-limit handling, fully mocked via respx."""

from __future__ import annotations

import json
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from stock_terminal.api.client import TossInvestClient, trading_day_key
from stock_terminal.api.exceptions import (
    TossAuthError,
    TossForbiddenError,
    TossRateLimitError,
)

BASE_URL = "https://openapi.tossinvest.com"


def _token_route(expires_in: int = 3600):
    return respx.post(f"{BASE_URL}/oauth2/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "tok-1", "expires_in": expires_in}
        )
    )


@pytest.fixture
async def client():
    c = TossInvestClient("id", "secret", BASE_URL)
    yield c
    await c.aclose()


@respx.mock
async def test_get_prices_caches_token_across_calls(client: TossInvestClient) -> None:
    token_route = _token_route()
    prices_route = respx.get(f"{BASE_URL}/api/v1/prices").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": [
                    {
                        "symbol": "005930",
                        "timestamp": "2026-07-21T09:30:00+09:00",
                        "lastPrice": "72000",
                        "currency": "KRW",
                    }
                ]
            },
        )
    )

    prices = await client.get_prices(["005930"])
    await client.get_prices(["005930"])

    assert token_route.call_count == 1
    assert prices_route.call_count == 2
    assert prices[0].symbol == "005930"
    assert str(prices[0].last_price) == "72000"


@respx.mock
async def test_get_prices_handles_null_timestamp(client: TossInvestClient) -> None:
    _token_route()
    respx.get(f"{BASE_URL}/api/v1/prices").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": [
                    {
                        "symbol": "114800",
                        "timestamp": None,
                        "lastPrice": "1111",
                        "currency": "KRW",
                    }
                ]
            },
        )
    )

    prices = await client.get_prices(["114800"])

    assert prices[0].timestamp is None
    assert str(prices[0].last_price) == "1111"


@respx.mock
async def test_auth_failure_raises_toss_auth_error(client: TossInvestClient) -> None:
    respx.post(f"{BASE_URL}/oauth2/token").mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )

    with pytest.raises(TossAuthError):
        await client.get_prices(["005930"])


@respx.mock
async def test_401_on_request_forces_token_refresh_and_retries(
    client: TossInvestClient,
) -> None:
    token_responses = [
        httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600}),
        httpx.Response(200, json={"access_token": "tok-2", "expires_in": 3600}),
    ]
    respx.post(f"{BASE_URL}/oauth2/token").mock(side_effect=token_responses)

    price_responses = [
        httpx.Response(401, json={"error": "expired"}),
        httpx.Response(
            200,
            json={
                "result": [
                    {
                        "symbol": "AAPL",
                        "timestamp": "2026-07-21T09:30:00+09:00",
                        "lastPrice": "230.50",
                        "currency": "USD",
                    }
                ]
            },
        ),
    ]
    respx.get(f"{BASE_URL}/api/v1/prices").mock(side_effect=price_responses)

    prices = await client.get_prices(["AAPL"])

    assert prices[0].symbol == "AAPL"


@respx.mock
async def test_403_raises_forbidden_with_ip_hint(client: TossInvestClient) -> None:
    _token_route()
    respx.get(f"{BASE_URL}/api/v1/prices").mock(
        return_value=httpx.Response(403, json={"error": "forbidden"})
    )

    with pytest.raises(TossForbiddenError, match="허용 IP"):
        await client.get_prices(["005930"])


@respx.mock
async def test_429_retries_once_then_succeeds(client: TossInvestClient) -> None:
    _token_route()
    responses = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(
            200,
            json={
                "result": [
                    {
                        "symbol": "005930",
                        "timestamp": "2026-07-21T09:30:00+09:00",
                        "lastPrice": "71000",
                        "currency": "KRW",
                    }
                ]
            },
        ),
    ]
    respx.get(f"{BASE_URL}/api/v1/prices").mock(side_effect=responses)

    prices = await client.get_prices(["005930"])

    assert prices[0].symbol == "005930"


@respx.mock
async def test_429_exhausted_raises_rate_limit_error(client: TossInvestClient) -> None:
    _token_route()
    respx.get(f"{BASE_URL}/api/v1/prices").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"})
    )

    with pytest.raises(TossRateLimitError):
        await client.get_prices(["005930"])


@respx.mock
async def test_get_previous_close_skips_todays_candle(client: TossInvestClient) -> None:
    _token_route()
    today = datetime.now(timezone.utc).astimezone()
    yesterday = today - timedelta(days=1)

    respx.get(f"{BASE_URL}/api/v1/candles").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "candles": [
                        {
                            "timestamp": yesterday.isoformat(),
                            "openPrice": "100",
                            "highPrice": "110",
                            "lowPrice": "95",
                            "closePrice": "105",
                            "volume": "1000",
                        },
                        {
                            "timestamp": today.isoformat(),
                            "openPrice": "105",
                            "highPrice": "108",
                            "lowPrice": "103",
                            "closePrice": "107",
                            "volume": "500",
                        },
                    ],
                    "nextBefore": None,
                }
            },
        )
    )

    prev_close = await client.get_previous_close("005930")

    assert str(prev_close) == "105"


@respx.mock
async def test_get_previous_close_krw_uses_krx_closing_auction_not_nxt_afterhours(
    client: TossInvestClient,
) -> None:
    """KRW previous close must use the 15:30 KST KRX auction print, not the
    1d candle's close - which keeps drifting through NXT after-hours trading
    (until ~20:00 KST) and can end up 1%+ away from the official close."""
    _token_route()
    kst = ZoneInfo("Asia/Seoul")
    today = datetime.now(kst).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)

    def candles_side_effect(request: httpx.Request) -> httpx.Response:
        interval = request.url.params.get("interval")
        if interval == "1d":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "candles": [
                            {
                                "timestamp": today.isoformat(),
                                "openPrice": "268000",
                                "highPrice": "269500",
                                "lowPrice": "265000",
                                "closePrice": "267500",
                                "volume": "1513909",
                            },
                            {
                                "timestamp": yesterday.isoformat(),
                                "openPrice": "265000",
                                "highPrice": "273000",
                                "lowPrice": "257000",
                                # Contaminated by NXT after-hours trading.
                                "closePrice": "273000",
                                "volume": "29117852",
                            },
                        ],
                        "nextBefore": None,
                    }
                },
            )

        base = yesterday.replace(hour=15, minute=20)
        minute_candles = []
        for i in range(16):
            ts = base + timedelta(minutes=i)
            if i == 11:  # 15:31 KST: the closing single-price auction print.
                minute_candles.append(
                    {
                        "timestamp": ts.isoformat(),
                        "openPrice": "269750",
                        "highPrice": "270000",
                        "lowPrice": "269750",
                        "closePrice": "270000",
                        "volume": "1752210",
                    }
                )
            else:
                minute_candles.append(
                    {
                        "timestamp": ts.isoformat(),
                        "openPrice": "269750",
                        "highPrice": "269750",
                        "lowPrice": "269750",
                        "closePrice": "269750",
                        "volume": "0",
                    }
                )
        return httpx.Response(
            200, json={"result": {"candles": minute_candles, "nextBefore": None}}
        )

    respx.get(f"{BASE_URL}/api/v1/candles").mock(side_effect=candles_side_effect)

    prev_close = await client.get_previous_close("005930", "KRW")

    assert str(prev_close) == "270000"


@respx.mock
async def test_get_previous_close_usd_uses_16_00_et_closing_auction(
    client: TossInvestClient,
) -> None:
    """USD previous close must be the 16:00 ET closing-auction print, not
    the 1d candle's close.

    After-hours trades (16:00-20:00 ET) get bucketed by this feed under the
    *next* calendar day instead of staying in the session's own day, so
    during after-hours the most recently *completed* regular session is
    "today" (ET), not yesterday - the reference must not be pushed back an
    extra day just because a stray "tomorrow"-dated placeholder exists.
    Separately, the 1d candle's own close field can drift from the true
    16:00 print, so the 1-minute auction-window volume spike must win.
    """
    _token_route()
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    reference_date = (
        now_et.date()
        if now_et.time() >= dt_time(16, 0)
        else now_et.date() - timedelta(days=1)
    )
    day_before = reference_date - timedelta(days=1)
    tomorrow_stub = datetime.combine(
        reference_date + timedelta(days=1), dt_time(0, 0), tzinfo=et
    )
    reference_open = datetime.combine(reference_date, dt_time(0, 0), tzinfo=et)
    day_before_open = datetime.combine(day_before, dt_time(0, 0), tzinfo=et)

    def candles_side_effect(request: httpx.Request) -> httpx.Response:
        interval = request.url.params.get("interval")
        if interval == "1d":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "candles": [
                            {
                                # Stray placeholder for tomorrow's session.
                                "timestamp": tomorrow_stub.isoformat(),
                                "openPrice": "545",
                                "highPrice": "546",
                                "lowPrice": "544",
                                "closePrice": "545",
                                "volume": "10",
                            },
                            {
                                # Drifted from the true 16:00 print (541.20
                                # instead of 539.69) - must be overridden.
                                "timestamp": reference_open.isoformat(),
                                "openPrice": "530",
                                "highPrice": "546",
                                "lowPrice": "529",
                                "closePrice": "541.20",
                                "volume": "27000000",
                            },
                            {
                                "timestamp": day_before_open.isoformat(),
                                "openPrice": "550",
                                "highPrice": "555",
                                "lowPrice": "548",
                                "closePrice": "552.33",
                                "volume": "24000000",
                            },
                        ],
                        "nextBefore": None,
                    }
                },
            )

        base = datetime.combine(reference_date, dt_time(15, 55), tzinfo=et)
        minute_candles = []
        for i in range(11):
            ts = base + timedelta(minutes=i)
            if i == 5:  # 16:00 ET: the closing-auction print.
                minute_candles.append(
                    {
                        "timestamp": ts.isoformat(),
                        "openPrice": "538",
                        "highPrice": "540",
                        "lowPrice": "538",
                        "closePrice": "539.69",
                        "volume": "1879184",
                    }
                )
            else:
                minute_candles.append(
                    {
                        "timestamp": ts.isoformat(),
                        "openPrice": "541",
                        "highPrice": "541",
                        "lowPrice": "541",
                        "closePrice": "541",
                        "volume": "0",
                    }
                )
        return httpx.Response(
            200, json={"result": {"candles": minute_candles, "nextBefore": None}}
        )

    respx.get(f"{BASE_URL}/api/v1/candles").mock(side_effect=candles_side_effect)

    prev_close = await client.get_previous_close("AMD", "USD")

    assert str(prev_close) == "539.69"


@respx.mock
async def test_get_candles_skips_null_timestamp_entries(
    client: TossInvestClient,
) -> None:
    """Thin/leveraged symbols can report timestamp: null for candles with no
    trades - the same quirk already handled for /prices. A naive
    datetime.fromisoformat(None) would raise and, left uncaught, permanently
    kills the price-poll worker (all watchlist rows freeze until restart).
    Such entries must be dropped instead of crashing the parse."""
    _token_route()

    respx.get(f"{BASE_URL}/api/v1/candles").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "candles": [
                        {
                            "timestamp": None,
                            "openPrice": "0",
                            "highPrice": "0",
                            "lowPrice": "0",
                            "closePrice": "0",
                            "volume": "0",
                        },
                        {
                            "timestamp": "2026-07-27T00:00:00+00:00",
                            "openPrice": "100",
                            "highPrice": "110",
                            "lowPrice": "95",
                            "closePrice": "105",
                            "volume": "1000",
                        },
                    ],
                    "nextBefore": None,
                }
            },
        )
    )

    candles = await client.get_candles("KORU", interval="1d", count=5)

    assert len(candles) == 1
    assert str(candles[0].close) == "105"


@respx.mock
async def test_get_market_indicator_prices_parses_without_currency_field(
    client: TossInvestClient,
) -> None:
    _token_route()
    respx.get(f"{BASE_URL}/api/v1/market-indicators/prices").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": [
                    {
                        "symbol": "KOSPI",
                        "timestamp": "2026-07-21T09:30:00+09:00",
                        "lastPrice": "2650.32",
                    }
                ]
            },
        )
    )

    prices = await client.get_market_indicator_prices(["KOSPI"])

    assert prices[0].symbol == "KOSPI"
    assert prices[0].currency == "PT"
    assert str(prices[0].last_price) == "2650.32"


@respx.mock
async def test_get_index_previous_close_uses_krx_closing_auction(
    client: TossInvestClient,
) -> None:
    """KOSPI/KOSDAQ share the KRX session close logic with KRW stocks, just
    against the market-indicators candle endpoint."""
    _token_route()
    kst = ZoneInfo("Asia/Seoul")
    today = datetime.now(kst).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)

    def candles_side_effect(request: httpx.Request) -> httpx.Response:
        interval = request.url.params.get("interval")
        if interval == "1d":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "candles": [
                            {
                                "timestamp": today.isoformat(),
                                "openPrice": "2640",
                                "highPrice": "2660",
                                "lowPrice": "2635",
                                "closePrice": "2650",
                                "volume": "500000",
                            },
                            {
                                "timestamp": yesterday.isoformat(),
                                "openPrice": "2600",
                                "highPrice": "2670",
                                "lowPrice": "2590",
                                "closePrice": "2670",
                                "volume": "600000",
                            },
                        ],
                        "nextBefore": None,
                    }
                },
            )

        base = yesterday.replace(hour=15, minute=20)
        minute_candles = []
        for i in range(16):
            ts = base + timedelta(minutes=i)
            if i == 11:  # 15:31 KST: the closing single-price auction print.
                minute_candles.append(
                    {
                        "timestamp": ts.isoformat(),
                        "openPrice": "2652.10",
                        "highPrice": "2652.50",
                        "lowPrice": "2652.10",
                        "closePrice": "2652.50",
                        "volume": "50000",
                    }
                )
            else:
                minute_candles.append(
                    {
                        "timestamp": ts.isoformat(),
                        "openPrice": "2650",
                        "highPrice": "2650",
                        "lowPrice": "2650",
                        "closePrice": "2650",
                        "volume": "0",
                    }
                )
        return httpx.Response(
            200, json={"result": {"candles": minute_candles, "nextBefore": None}}
        )

    respx.get(f"{BASE_URL}/api/v1/market-indicators/KOSPI/candles").mock(
        side_effect=candles_side_effect
    )

    prev_close = await client.get_index_previous_close("KOSPI")

    assert str(prev_close) == "2652.50"


@pytest.mark.parametrize(
    "utc_hour, expected",
    [
        (19, "2026-07-26"),  # 15:00 ET - before today's 16:00 close
        (21, "2026-07-27"),  # 17:00 ET - after the close, in after-hours
    ],
)
def test_trading_day_key_flips_at_session_close(
    monkeypatch: pytest.MonkeyPatch, utc_hour: int, expected: str
) -> None:
    """The staleness key that gates prev_close refetching must change the
    moment the regular session closes, not just at midnight.

    Previously it only tracked the calendar date, so a prev_close cached
    during today's pre-close hours (baseline: yesterday's close) was never
    invalidated when the market closed - after-hours change %/amount kept
    comparing the live price against that stale, already-superseded
    baseline for the rest of the session instead of today's own close."""
    import stock_terminal.api.client as client_module

    fixed_now = datetime(2026, 7, 27, utc_hour, 0, tzinfo=timezone.utc)

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now.astimezone(tz) if tz else fixed_now

    monkeypatch.setattr(client_module, "datetime", _FrozenDateTime)

    assert trading_day_key("USD") == expected


@pytest.mark.parametrize("endpoint", ["/oauth2/token", "/api/v1/prices"])
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
@respx.mock
async def test_transport_errors_are_api_errors(client, endpoint, error_type):
    from stock_terminal.api.exceptions import TossAPIError

    _token_route()
    route = (respx.post if endpoint == "/oauth2/token" else respx.get)(BASE_URL + endpoint)
    route.mock(side_effect=error_type("offline"))
    with pytest.raises(TossAPIError):
        await client.get_prices(["AAPL"])


@pytest.mark.parametrize("payload", [None, {}, {"result": None}, {"result": [{}]},
    {"result": [{"symbol": "AAPL", "timestamp": "invalid", "lastPrice": "1", "currency": "USD"}]},
    {"result": [{"symbol": "AAPL", "timestamp": None, "lastPrice": "bad", "currency": "USD"}]}])
@respx.mock
async def test_malformed_prices_are_api_errors(client, payload):
    from stock_terminal.api.exceptions import TossAPIError

    _token_route()
    respx.get(BASE_URL + "/api/v1/prices").respond(200, content="invalid" if payload is None else json.dumps(payload))
    with pytest.raises(TossAPIError):
        await client.get_prices(["AAPL"])


@respx.mock
async def test_token_server_error_is_api_error(client):
    from stock_terminal.api.exceptions import TossAPIError

    respx.post(BASE_URL + "/oauth2/token").respond(503)
    with pytest.raises(TossAPIError, match="503"):
        await client.get_prices(["AAPL"])


@respx.mock
async def test_request_cancellation_propagates(client, monkeypatch):
    import asyncio

    _token_route()
    from unittest.mock import AsyncMock

    monkeypatch.setattr(client._http, "request", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await client.get_prices(["AAPL"])


@pytest.mark.parametrize("retry_after, expected", [("bad", 1.0), ("nan", 1.0), ("-1", 1.0), ("999999", 60.0)])
@respx.mock
async def test_invalid_retry_after_is_bounded(client, monkeypatch, retry_after, expected):
    from unittest.mock import AsyncMock
    from stock_terminal.api.exceptions import TossRateLimitError

    sleep = AsyncMock()
    monkeypatch.setattr("stock_terminal.api.client.asyncio.sleep", sleep)
    _token_route()
    respx.get(BASE_URL + "/api/v1/prices").respond(429, headers={"Retry-After": retry_after})
    with pytest.raises(TossRateLimitError):
        await client.get_prices(["AAPL"])
    sleep.assert_awaited_once_with(expected)


@pytest.mark.parametrize("payload", [{}, {"access_token": "test", "expires_in": "bad"}])
@respx.mock
async def test_invalid_token_response_is_api_error(client, payload):
    from stock_terminal.api.exceptions import TossAPIError

    respx.post(BASE_URL + "/oauth2/token").respond(200, json=payload)
    with pytest.raises(TossAPIError):
        await client.get_prices(["AAPL"])


@pytest.mark.parametrize("method, path", [
    ("get_stocks", "/api/v1/stocks"),
    ("get_candles", "/api/v1/candles"),
    ("get_market_indicator_prices", "/api/v1/market-indicators/prices"),
    ("get_market_indicator_candles", "/api/v1/market-indicators/KOSPI/candles"),
])
@respx.mock
async def test_other_endpoints_normalize_malformed_responses(client, method, path):
    from stock_terminal.api.exceptions import TossAPIError

    _token_route()
    respx.get(BASE_URL + path).respond(200, json={})
    argument = "KOSPI" if "candles" in method else ["KOSPI"]
    with pytest.raises(TossAPIError):
        await getattr(client, method)(argument)
