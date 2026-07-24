"""Async client for the Toss Invest Open API.

Docs: https://developers.tossinvest.com/docs
Auth: OAuth2 client_credentials grant against POST /oauth2/token.
No WebSocket support yet, so all "real-time" data is REST polling.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx

from stock_terminal.api.exceptions import (
    TossAPIError,
    TossAuthError,
    TossForbiddenError,
    TossRateLimitError,
)
from stock_terminal.api.models import Candle, Price, StockInfo

TOKEN_REFRESH_MARGIN_SECONDS = 60
MAX_SYMBOLS_PER_CALL = 200

_KST = ZoneInfo("Asia/Seoul")
_US_EASTERN = ZoneInfo("America/New_York")
_KRX_CLOSE_AUCTION_START = dt_time(15, 20)
_KRX_CLOSE_AUCTION_END = dt_time(15, 35)


def _market_zone(currency: str | None) -> ZoneInfo | None:
    """Timezone whose calendar date defines "today" for `currency`'s market.

    KRX and NXT both run within a single KST calendar day. US markets
    (pre-market through after-hours) run on the US/Eastern calendar day.
    Falling back to system-local time here would misjudge which candle is
    "today's still-forming one" - and therefore should be excluded from the
    previous-close lookup - whenever the host machine's timezone isn't KST.
    """
    if currency == "KRW":
        return _KST
    if currency == "USD":
        return _US_EASTERN
    return None


def _trading_day(instant: datetime, zone: ZoneInfo | None) -> date:
    if zone is None:
        return instant.astimezone().date()
    return instant.astimezone(zone).date()


class TossInvestClient:
    def __init__(self, client_id: str, client_secret: str, base_url: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.AsyncClient(base_url=base_url, timeout=10.0)
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> TossInvestClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- OAuth -----------------------------------------------------------

    async def _ensure_token(self, *, force: bool = False) -> str:
        async with self._token_lock:
            now = time.monotonic()
            if (
                not force
                and self._access_token is not None
                and now < self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS
            ):
                return self._access_token

            response = await self._http.post(
                "/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code in (401, 400):
                raise TossAuthError(
                    "OAuth 토큰 발급 실패: TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 값을 확인하세요."
                )
            if response.status_code == 403:
                raise TossForbiddenError()
            response.raise_for_status()

            payload = response.json()
            self._access_token = payload["access_token"]
            expires_in = float(payload.get("expires_in", 3600))
            self._token_expires_at = now + expires_in
            return self._access_token

    # -- low-level request with 401/403/429 handling ----------------------

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        token = await self._ensure_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"

        response = await self._http.request(method, path, headers=headers, **kwargs)

        if response.status_code == 401:
            token = await self._ensure_token(force=True)
            headers["Authorization"] = f"Bearer {token}"
            response = await self._http.request(method, path, headers=headers, **kwargs)

        if response.status_code == 403:
            raise TossForbiddenError()

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else 1.0
            await asyncio.sleep(wait)
            response = await self._http.request(method, path, headers=headers, **kwargs)
            if response.status_code == 429:
                raise TossRateLimitError(retry_after=wait)

        if response.status_code >= 400:
            raise TossAPIError(f"{method} {path} -> {response.status_code}: {response.text}")

        return response

    # -- public endpoints ---------------------------------------------------

    async def get_prices(self, symbols: list[str]) -> list[Price]:
        if not symbols:
            return []
        results: list[Price] = []
        for batch in _chunk(symbols, MAX_SYMBOLS_PER_CALL):
            response = await self._request(
                "GET", "/api/v1/prices", params={"symbols": ",".join(batch)}
            )
            data = response.json()
            results.extend(Price.from_json(item) for item in data["result"])
        return results

    async def get_stocks(self, symbols: list[str]) -> list[StockInfo]:
        if not symbols:
            return []
        results: list[StockInfo] = []
        for batch in _chunk(symbols, MAX_SYMBOLS_PER_CALL):
            response = await self._request(
                "GET", "/api/v1/stocks", params={"symbols": ",".join(batch)}
            )
            data = response.json()
            results.extend(StockInfo.from_json(item) for item in data["result"])
        return results

    async def get_candles(
        self,
        symbol: str,
        interval: str = "1d",
        count: int = 100,
        before: str | None = None,
    ) -> list[Candle]:
        params = {"symbol": symbol, "interval": interval, "count": count}
        if before is not None:
            params["before"] = before
        response = await self._request("GET", "/api/v1/candles", params=params)
        data = response.json()["result"]
        return [Candle.from_json(item) for item in data["candles"]]

    async def get_previous_close(
        self, symbol: str, currency: str | None = None
    ) -> Decimal | None:
        """Best-effort previous close, used as the baseline for change %.

        /api/v1/prices has no previousClose field, so we derive it from the
        daily candle series: the most recent candle whose date isn't today.

        count=5 (not 2): the feed can carry a stray same-instant/placeholder
        candle for the not-yet-started next session (seen for USD symbols,
        where "today" briefly holds an early, low-volume entry before the
        US/Eastern calendar day has actually begun) - with count=2 that alone
        could crowd out the real previous session, so we look back further.
        """
        candles = await self.get_candles(symbol, interval="1d", count=5)
        if not candles:
            return None
        zone = _market_zone(currency)
        today = _trading_day(datetime.now(timezone.utc), zone)
        prev_candle = None
        for candle in sorted(candles, key=lambda c: c.timestamp, reverse=True):
            # Strictly earlier than today, not just "!= today": a candle
            # dated *after* today shouldn't happen, but if the upstream feed
            # ever emits one (clock skew, a stray placeholder bucket), it
            # must never be mistaken for a completed prior session.
            if _trading_day(candle.timestamp, zone) < today:
                prev_candle = candle
                break
        prev_candle = prev_candle or candles[0]

        if currency == "KRW":
            official_close = await self._krx_official_close(
                symbol, prev_candle.timestamp.astimezone(_KST).date()
            )
            if official_close is not None:
                return official_close
        return prev_candle.close

    async def _krx_official_close(
        self, symbol: str, trading_day: date
    ) -> Decimal | None:
        """The KRX regular-session closing-auction price for `trading_day`.

        KRX's 1d candle close (as served by /api/v1/candles) keeps updating
        through NXT's after-hours session (until ~20:00 KST), so it drifts
        away from the official 15:30 KST closing-auction price - sometimes
        by more than 1%. The closing auction always prints as a distinct
        volume spike in the surrounding 1m candles, so we pick that instead.
        """
        window_end = datetime.combine(
            trading_day, _KRX_CLOSE_AUCTION_END, tzinfo=_KST
        ) + timedelta(minutes=1)
        try:
            candles = await self.get_candles(
                symbol, interval="1m", count=20, before=window_end.isoformat()
            )
        except TossAPIError:
            return None
        window = [
            c
            for c in candles
            if _KRX_CLOSE_AUCTION_START
            <= c.timestamp.astimezone(_KST).time()
            <= _KRX_CLOSE_AUCTION_END
        ]
        if not window:
            return None
        return max(window, key=lambda c: c.volume).close


def _chunk(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]
