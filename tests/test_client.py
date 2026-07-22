"""Tests for TossInvestClient auth/retry/rate-limit handling, fully mocked via respx."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
