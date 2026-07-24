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


class _MarketSession:
    """Regular-session close time + closing-auction window, in the market's
    own timezone. Used to pin down the exact previous-close baseline instead
    of trusting the /api/v1/candles 1d aggregate, whose day-bucketing turns
    out to be unreliable for this in different ways per market:

    - KRW: a day's 1d candle keeps absorbing NXT after-hours trades (until
      ~20:00 KST), so its "close" drifts away from the 15:30 KRX auction
      price - sometimes by more than 1%.
    - USD: after-hours trades (16:00-20:00 ET) get bucketed under the
      *next* calendar day instead of staying in today's, so a naive
      "skip the candle dated today" check ends up skipping the correct,
      already-final regular-session close and falling back to the day
      before that.

    Both are avoided by finding the real trading day ourselves (via the
    close-time cutoff) and reading the closing-auction print directly out
    of 1-minute candles, which is a distinct volume spike in both markets.
    """

    def __init__(
        self, zone: ZoneInfo, close: dt_time, auction_start: dt_time, auction_end: dt_time
    ) -> None:
        self.zone = zone
        self.close = close
        self.auction_start = auction_start
        self.auction_end = auction_end


_KRX_SESSION = _MarketSession(_KST, dt_time(15, 30), dt_time(15, 20), dt_time(15, 35))
_US_SESSION = _MarketSession(_US_EASTERN, dt_time(16, 0), dt_time(15, 55), dt_time(16, 5))

_SESSIONS = {"KRW": _KRX_SESSION, "USD": _US_SESSION}


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

        For KRW/USD this is "the most recently completed regular session's
        closing-auction price" (see `_MarketSession`), not the 1d candle's
        close, since that field is unreliable in different ways per market.
        For anything else, falls back to the 1d candle series: the most
        recent candle whose date isn't today.
        """
        session = _SESSIONS.get(currency or "")
        if session is not None:
            return await self._session_close(symbol, session)

        candles = await self.get_candles(symbol, interval="1d", count=2)
        if not candles:
            return None
        today = datetime.now(timezone.utc).astimezone().date()
        for candle in sorted(candles, key=lambda c: c.timestamp, reverse=True):
            if candle.timestamp.date() != today:
                return candle.close
        return candles[0].close

    async def _session_close(
        self, symbol: str, session: _MarketSession
    ) -> Decimal | None:
        now = datetime.now(timezone.utc).astimezone(session.zone)
        reference_date = (
            now.date() if now.time() >= session.close else now.date() - timedelta(days=1)
        )

        # Walk back to the most recent actual trading day <= reference_date
        # (skips weekends/holidays, and any stray candle the feed dates
        # *after* today - e.g. USD after-hours bucketed as tomorrow).
        candles = await self.get_candles(symbol, interval="1d", count=5)
        trading_day = None
        for candle in sorted(candles, key=lambda c: c.timestamp, reverse=True):
            candle_date = candle.timestamp.astimezone(session.zone).date()
            if candle_date <= reference_date:
                trading_day = candle_date
                break
        if trading_day is None:
            return candles[0].close if candles else None

        window_end = datetime.combine(
            trading_day, session.auction_end, tzinfo=session.zone
        ) + timedelta(minutes=1)
        try:
            minute_candles = await self.get_candles(
                symbol, interval="1m", count=20, before=window_end.isoformat()
            )
        except TossAPIError:
            minute_candles = []
        window = [
            c
            for c in minute_candles
            if session.auction_start
            <= c.timestamp.astimezone(session.zone).time()
            <= session.auction_end
        ]
        if window:
            return max(window, key=lambda c: c.volume).close

        # No 1m data for the auction window (e.g. a thin symbol) - fall
        # back to that day's 1d candle close.
        for candle in candles:
            if candle.timestamp.astimezone(session.zone).date() == trading_day:
                return candle.close
        return None


def _chunk(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]
