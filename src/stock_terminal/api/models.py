"""Typed views over the Toss Invest Open API JSON responses.

The `/api/v1/prices` endpoint only returns the last traded price - no
previous-close/change fields - so change % is computed client-side from a
daily candle lookup (see `client.TossInvestClient.get_previous_close`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import math


def _number(value: object, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("API 숫자 형식 오류")
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or (not allow_zero and number == 0):
        raise ValueError("API 숫자 범위 오류")
    # The chart renderer uses float; reject overflow and price underflow here.
    rendered = float(number)
    if not math.isfinite(rendered) or (not allow_zero and rendered == 0):
        raise ValueError("표시할 수 없는 API 숫자 범위")
    return number


@dataclass(frozen=True, slots=True)
class Price:
    symbol: str
    last_price: Decimal
    currency: str
    timestamp: datetime | None

    @classmethod
    def from_json(cls, data: dict) -> Price:
        # The API sends timestamp: null for symbols with no trade yet today
        # (e.g. an ETF that hasn't opened) even though lastPrice is present.
        raw_timestamp = data["timestamp"]
        return cls(
            symbol=data["symbol"],
            last_price=_number(data["lastPrice"]),
            currency=data["currency"],
            timestamp=datetime.fromisoformat(raw_timestamp) if raw_timestamp else None,
        )

    @classmethod
    def from_market_indicator_json(cls, data: dict) -> Price:
        # /api/v1/market-indicators/prices has the same shape as
        # /api/v1/prices minus "currency" - indices are quoted in points,
        # not a currency, so callers get the "PT" pseudo-currency instead.
        raw_timestamp = data["timestamp"]
        return cls(
            symbol=data["symbol"],
            last_price=_number(data["lastPrice"]),
            currency="PT",
            timestamp=datetime.fromisoformat(raw_timestamp) if raw_timestamp else None,
        )


@dataclass(frozen=True, slots=True)
class StockInfo:
    symbol: str
    name: str
    english_name: str
    market: str
    currency: str
    security_type: str

    @classmethod
    def from_json(cls, data: dict) -> StockInfo:
        return cls(
            symbol=data["symbol"],
            name=data.get("name") or data.get("englishName") or data["symbol"],
            english_name=data.get("englishName", ""),
            market=data.get("market", ""),
            currency=data.get("currency", ""),
            security_type=data.get("securityType", ""),
        )


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @classmethod
    def from_json(cls, data: dict) -> Candle:
        opening, high, low, close = (_number(data[key]) for key in
                                     ("openPrice", "highPrice", "lowPrice", "closePrice"))
        volume = _number(data.get("volume", "0"), allow_zero=True)
        if not (low <= opening <= high and low <= close <= high):
            raise ValueError("API OHLC 범위 오류")
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            open=opening, high=high, low=low, close=close, volume=volume,
        )


@dataclass(frozen=True, slots=True)
class CloseReference:
    value: Decimal
    session_date: str
    source: str
    confirmed: bool = False  # Candle-derived prices are estimates, not official closes.
