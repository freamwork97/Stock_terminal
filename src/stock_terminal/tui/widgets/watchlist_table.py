"""Live-updating price table for the tracked watchlist."""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
import time

from rich.text import Text
from textual.widgets import DataTable

from stock_terminal.api.models import Price
from stock_terminal.api.client import trading_day_key
from stock_terminal.storage.watchlist import Watchlist

COL_SYMBOL = "symbol"
COL_NAME = "name"
COL_PRICE = "price"
COL_CHANGE = "change"
COL_CHANGE_PCT = "change_pct"
COL_UPDATED = "updated"
COL_RECEIVED = "received"
COL_STATUS = "status"
COL_BASELINE = "baseline"

UP_STYLE = "bold green"
DOWN_STYLE = "bold red"
FLAT_STYLE = "bold"
FLASH_STYLE = "reverse bold yellow"


def _format_amount(value: Decimal, currency: str, *, signed: bool = False) -> str:
    """KRW as a whole-won amount ('12,345원'), PT (index points) as a bare
    number ('2,573.45pt'), everything else as USD ('$12.34')."""
    sign = ""
    if signed and value > 0:
        sign = "+"
    elif signed and value < 0:
        sign = "-"
        value = -value

    if currency == "KRW":
        return f"{sign}{value:,.0f}원"
    if currency == "PT":
        return f"{sign}{value:,.2f}pt"
    return f"{sign}${value:,.2f}"


class WatchlistTable(DataTable):
    """DataTable keyed by symbol; one row per watchlist item."""

    def __init__(self, *, stale_after: float = 30.0, **kwargs) -> None:
        super().__init__(**kwargs)
        self.stale_after = stale_after
        self._received_at: dict[str, float] = {}
        self._states: dict[str, str] = {}

    def refresh_freshness(self) -> None:
        for symbol, received in self._received_at.items():
            if (symbol in self.rows and time.monotonic() - received > self.stale_after
                    and self._states.get(symbol) not in ("조회 실패", "응답 누락")):
                self._set_status(symbol, "수신 지연")

    def _set_status(self, symbol: str, status: str) -> None:
        if symbol in self.rows:
            self._states[symbol] = status
            self.update_cell(symbol, COL_STATUS, Text(status, style="green" if status == "정상" else "yellow"))

    def mark_failed(self, symbols: list[str]) -> None:
        for symbol in symbols:
            self._set_status(symbol, "조회 실패")

    def mark_missing(self, symbols: list[str], received: set[str]) -> None:
        for symbol in symbols:
            if symbol not in received:
                self._set_status(symbol, "응답 누락")

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_column("심볼", key=COL_SYMBOL, width=10)
        self.add_column("상태", key=COL_STATUS, width=14)
        self.add_column("종목명", key=COL_NAME, width=18)
        self.add_column("현재가", key=COL_PRICE, width=14)
        self.add_column("등락", key=COL_CHANGE, width=12)
        self.add_column("등락률", key=COL_CHANGE_PCT, width=10)
        self.add_column("기준 종가", key=COL_BASELINE, width=32)
        self.add_column("체결시각 (로컬)", key=COL_UPDATED, width=19)
        self.add_column("수신시각 (로컬)", key=COL_RECEIVED, width=19)
        self.set_interval(1.0, self.refresh_freshness)

    def sync_rows(self, watchlist: Watchlist) -> None:
        """Add/remove rows so the table matches the current watchlist.

        New rows are added in watchlist order (i.e. the order symbols were
        added), not set-iteration order, so the table stays stable across runs.
        """
        existing = set(self.rows.keys())
        wanted = set(watchlist.symbols)

        for symbol in existing - wanted:
            self.remove_row(symbol)
            self._received_at.pop(symbol, None)
            self._states.pop(symbol, None)

        for symbol in watchlist.symbols:
            if symbol not in existing:
                item = watchlist.items[symbol]
                self.add_row(
                    item.symbol,
                    "대기",
                    item.name or item.symbol,
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    key=symbol,
                )

    @property
    def selected_symbol(self) -> str | None:
        if self.row_count == 0 or self.cursor_row is None:
            return None
        try:
            row_key, _ = self.coordinate_to_cell_key(self.cursor_coordinate)
        except Exception:
            return None
        return str(row_key.value) if row_key.value is not None else None

    def apply_prices(
        self, prices: dict[str, Price], watchlist: Watchlist
    ) -> list[str]:
        """Update rows with fresh prices. Returns symbols whose alert threshold fired."""
        triggered: list[str] = []

        for symbol, price in prices.items():
            item = watchlist.items.get(symbol)
            if item is None or symbol not in self.rows:
                continue

            prev_close = item.prev_close
            style = FLAT_STYLE
            change_text = "-"
            change_pct_text = "-"

            current_key = trading_day_key("KRW" if item.is_index else item.currency)
            usable = (prev_close is not None and prev_close.is_finite() and prev_close > 0
                      and item.prev_close_date == current_key
                      and item.prev_close_source != "unknown")
            if not usable:
                baseline_label = "기준가 재조회 중"
            else:
                source_label = {"minute_estimate": "분봉 추정", "daily_fallback": "일봉 대체"}.get(item.prev_close_source, "출처 불명")
                certainty = "확정" if item.prev_close_confirmed else "미확정"
                baseline_label = f"{item.prev_close_session or '?'} {source_label} ({certainty})"
            self.update_cell(symbol, COL_BASELINE, baseline_label)

            if usable:
                change = price.last_price - prev_close
                change_pct = (change / prev_close) * Decimal(100)
                style = UP_STYLE if change > 0 else DOWN_STYLE if change < 0 else FLAT_STYLE
                sign = "+" if change > 0 else ""
                change_text = _format_amount(change, price.currency, signed=True)
                change_pct_text = f"{sign}{change_pct:.2f}%"

            self.update_cell(
                symbol,
                COL_PRICE,
                Text(_format_amount(price.last_price, price.currency), style=style),
            )
            self.update_cell(symbol, COL_CHANGE, Text(change_text, style=style))
            self.update_cell(
                symbol, COL_CHANGE_PCT, Text(change_pct_text, style=style)
            )
            now = datetime.now(timezone.utc)
            self._received_at[symbol] = time.monotonic()
            self.update_cell(symbol, COL_RECEIVED, now.astimezone().strftime("%m-%d %H:%M:%S"))
            timestamp = price.timestamp
            if timestamp is None or timestamp.tzinfo is None:
                status = "체결시각 불명"
                updated_text = "-"
            else:
                age = (now - timestamp).total_seconds()
                status = "체결 오래됨" if age > self.stale_after else "정상"
                if age < -5:
                    status = "체결시각 이상"
                updated_text = timestamp.astimezone().strftime("%m-%d %H:%M:%S")
            self._set_status(symbol, status)
            self.update_cell(symbol, COL_UPDATED, updated_text)

            if item.alert_upper is not None and price.last_price >= item.alert_upper:
                triggered.append(symbol)
            elif item.alert_lower is not None and price.last_price <= item.alert_lower:
                triggered.append(symbol)

        return triggered

    def flash(self, symbol: str) -> None:
        if symbol not in self.rows:
            return
        self.update_cell(symbol, COL_SYMBOL, Text(symbol, style=FLASH_STYLE))
        self.set_timer(2.0, lambda: self._unflash(symbol))

    def _unflash(self, symbol: str) -> None:
        if symbol in self.rows:
            self.update_cell(symbol, COL_SYMBOL, symbol)
