"""Persisted watchlist: symbols the user is tracking, plus alert thresholds."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path


@dataclass(slots=True)
class WatchlistItem:
    symbol: str
    name: str = ""
    alert_upper: Decimal | None = None
    alert_lower: Decimal | None = None
    prev_close: Decimal | None = None
    prev_close_date: str | None = None  # ISO date the prev_close was fetched for
    currency: str = ""  # market's trading-day calendar depends on this

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("alert_upper", "alert_lower", "prev_close"):
            if d[key] is not None:
                d[key] = str(d[key])
        return d

    @classmethod
    def from_dict(cls, data: dict) -> WatchlistItem:
        def dec(key: str) -> Decimal | None:
            value = data.get(key)
            return Decimal(value) if value is not None else None

        return cls(
            symbol=data["symbol"],
            name=data.get("name", ""),
            alert_upper=dec("alert_upper"),
            alert_lower=dec("alert_lower"),
            prev_close=dec("prev_close"),
            prev_close_date=data.get("prev_close_date"),
            currency=data.get("currency", ""),
        )


@dataclass(slots=True)
class Watchlist:
    path: Path
    items: dict[str, WatchlistItem] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Watchlist:
        watchlist = cls(path=path)
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for entry in raw:
                item = WatchlistItem.from_dict(entry)
                watchlist.items[item.symbol] = item
        return watchlist

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [item.to_dict() for item in self.items.values()]
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @property
    def symbols(self) -> list[str]:
        return list(self.items.keys())

    def add(self, item: WatchlistItem) -> None:
        self.items[item.symbol] = item
        self.save()

    def remove(self, symbol: str) -> None:
        self.items.pop(symbol, None)
        self.save()

    def set_alerts(
        self, symbol: str, upper: Decimal | None, lower: Decimal | None
    ) -> None:
        item = self.items.get(symbol)
        if item is None:
            return
        item.alert_upper = upper
        item.alert_lower = lower
        self.save()

    def set_currency(self, symbol: str, currency: str) -> None:
        item = self.items.get(symbol)
        if item is None:
            return
        item.currency = currency
        self.save()

    def set_prev_close(
        self, symbol: str, prev_close: Decimal | None, prev_close_date: str | None
    ) -> None:
        item = self.items.get(symbol)
        if item is None:
            return
        item.prev_close = prev_close
        item.prev_close_date = prev_close_date
        self.save()
