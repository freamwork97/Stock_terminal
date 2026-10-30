"""Reject out-of-order quotes before touching UI, receive times or alerts."""
from dataclasses import dataclass
from datetime import datetime

from stock_terminal.api.models import Price
from stock_terminal.storage.watchlist import WatchlistItem


@dataclass(slots=True)
class _Accepted:
    item: WatchlistItem
    request: int
    trade_time: datetime | None


class QuoteOrder:
    def __init__(self):
        self._counter = 0
        self._accepted: dict[str, _Accepted] = {}

    def begin(self, items: dict[str, WatchlistItem]) -> int:
        self._accepted = {symbol: previous for symbol, previous in self._accepted.items()
                          if items.get(symbol) is previous.item}
        self._counter += 1
        return self._counter

    def is_current(self, symbol: str, item: WatchlistItem, request: int) -> bool:
        previous = self._accepted.get(symbol)
        return previous is None or previous.item is not item or request >= previous.request

    def accept(self, price: Price, item: WatchlistItem, request: int) -> bool:
        previous = self._accepted.get(price.symbol)
        if previous is not None and previous.item is not item:
            previous = None
        stamp = price.timestamp
        if stamp is not None and (stamp.tzinfo is None or stamp.utcoffset() is None):
            stamp = None
        if previous is not None:
            if request < previous.request:
                return False
            if stamp is not None and previous.trade_time is not None and stamp < previous.trade_time:
                return False
        # Missing timestamps fall back to request order without erasing the last
        # known trade-time watermark, which still protects following responses.
        watermark = stamp if stamp is not None else (previous.trade_time if previous else None)
        self._accepted[price.symbol] = _Accepted(item, request, watermark)
        return True
