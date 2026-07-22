"""Typed views over the Toss Invest Open API JSON responses.

The `/api/v1/prices` endpoint only returns the last traded price - no
previous-close/change fields - so change % is computed client-side from a
daily candle lookup (see `client.TossInvestClient.get_previous_close`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


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
            last_price=Decimal(str(data["lastPrice"])),
            currency=data["currency"],
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
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            open=Decimal(str(data["openPrice"])),
            high=Decimal(str(data["highPrice"])),
            low=Decimal(str(data["lowPrice"])),
            close=Decimal(str(data["closePrice"])),
            volume=Decimal(str(data.get("volume", "0"))),
        )
