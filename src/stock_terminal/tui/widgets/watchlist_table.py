"""Live-updating price table for the tracked watchlist."""

from __future__ import annotations

from decimal import Decimal

from rich.text import Text
from textual.widgets import DataTable

from stock_terminal.api.models import Price
from stock_terminal.storage.watchlist import Watchlist

COL_SYMBOL = "symbol"
COL_NAME = "name"
COL_PRICE = "price"
COL_CHANGE = "change"
COL_CHANGE_PCT = "change_pct"
COL_UPDATED = "updated"

UP_STYLE = "bold green"
DOWN_STYLE = "bold red"
FLAT_STYLE = "bold"
FLASH_STYLE = "reverse bold yellow"


def _format_amount(value: Decimal, currency: str, *, signed: bool = False) -> str:
    """KRW as a whole-won amount ('12,345원'), everything else as USD ('$12.34')."""
    sign = ""
    if signed and value > 0:
        sign = "+"
    elif signed and value < 0:
        sign = "-"
        value = -value

    if currency == "KRW":
        return f"{sign}{value:,.0f}원"
    return f"{sign}${value:,.2f}"


class WatchlistTable(DataTable):
    """DataTable keyed by symbol; one row per watchlist item."""

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_column("심볼", key=COL_SYMBOL, width=10)
        self.add_column("종목명", key=COL_NAME, width=18)
        self.add_column("현재가", key=COL_PRICE, width=14)
        self.add_column("등락", key=COL_CHANGE, width=12)
        self.add_column("등락률", key=COL_CHANGE_PCT, width=10)
        self.add_column("갱신시각", key=COL_UPDATED, width=10)

    def sync_rows(self, watchlist: Watchlist) -> None:
        """Add/remove rows so the table matches the current watchlist."""
        existing = set(self.rows.keys())
        wanted = set(watchlist.symbols)

        for symbol in existing - wanted:
            self.remove_row(symbol)

        for symbol in wanted - existing:
            item = watchlist.items[symbol]
            self.add_row(
                item.symbol,
                item.name or item.symbol,
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

            if prev_close is not None and prev_close != 0:
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
            self.update_cell(
                symbol, COL_UPDATED, price.timestamp.strftime("%H:%M:%S")
            )

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
