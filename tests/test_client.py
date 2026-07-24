"""Tests for TossInvestClient auth/retry/rate-limit handling, fully mocked via respx."""

from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from stock_terminal.api.client import TossInvestClient
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
