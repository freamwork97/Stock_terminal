"""Tests for TossInvestClient auth/retry/rate-limit handling, fully mocked via respx."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
async def test_get_previous_close_usd_uses_us_eastern_trading_day(
    client: TossInvestClient,
) -> None:
    """USD previous close must reset on the US/Eastern calendar day, not KST
    or system-local time. US regular hours run 22:30/23:30-05:00/06:00 KST,
    so for most of the Korean day the "current" US/Eastern trading day is
    still in progress (or its regular session already closed but its own
    day hasn't rolled over) - using anything but US/Eastern to judge which
    candle is "today's" makes the lookup pick that day's own close instead
    of the real previous day's, one day too recent. It must also ignore a
    stray candle dated *after* today (e.g. a not-yet-started next session
    placeholder the feed may emit early), not just skip candles dated today.
    """
    _token_route()
    et = ZoneInfo("America/New_York")
    et_today = datetime.now(et).replace(hour=0, minute=0, second=0, microsecond=0)
    et_yesterday = et_today - timedelta(days=1)
    et_before_that = et_today - timedelta(days=2)

    respx.get(f"{BASE_URL}/api/v1/candles").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "candles": [
                        {
                            # Stray placeholder for a session that hasn't
                            # started yet.
                            "timestamp": (et_today + timedelta(days=1)).isoformat(),
                            "openPrice": "545",
                            "highPrice": "546",
                            "lowPrice": "544",
                            "closePrice": "545",
                            "volume": "10",
                        },
                        {
                            # Today's (ET) own completed regular session.
                            "timestamp": et_today.isoformat(),
                            "openPrice": "530",
                            "highPrice": "541",
                            "lowPrice": "529",
                            "closePrice": "539.69",
                            "volume": "27000000",
                        },
                        {
                            # The real previous close.
                            "timestamp": et_yesterday.isoformat(),
                            "openPrice": "550",
                            "highPrice": "555",
                            "lowPrice": "548",
                            "closePrice": "552.33",
                            "volume": "24000000",
                        },
                        {
                            "timestamp": et_before_that.isoformat(),
                            "openPrice": "500",
                            "highPrice": "510",
                            "lowPrice": "495",
                            "closePrice": "503.57",
                            "volume": "23000000",
                        },
                    ],
                    "nextBefore": None,
                }
            },
        )
    )

    prev_close = await client.get_previous_close("AMD", "USD")

    assert str(prev_close) == "552.33"
