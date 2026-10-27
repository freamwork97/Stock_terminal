"""Main Textual application: layout, keybindings, polling workers."""

from __future__ import annotations

import asyncio

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header

from stock_terminal.api.client import TossInvestClient, trading_day_key as _trading_day_key
from stock_terminal.api.exceptions import TossAPIError
from stock_terminal.config import TOSS_API_BASE_URL, WATCHLIST_PATH, Settings, ensure_data_dir
from stock_terminal.storage.watchlist import Watchlist, WatchlistItem
from stock_terminal.tui.screens.add_symbol import AddSymbolResult, AddSymbolScreen
from stock_terminal.tui.screens.set_alert import AlertResult, SetAlertScreen
from stock_terminal.tui.widgets.chart_panel import ChartPanel
from stock_terminal.tui.widgets.watchlist_table import WatchlistTable


class StockTerminalApp(App):
    """Real-time stock watchlist terminal backed by the Toss Invest Open API."""

    TITLE = "Stock Terminal"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        ("a", "add_symbol", "종목추가"),
        ("d", "delete_symbol", "삭제"),
        ("t", "set_alert", "알림설정"),
        ("m", "toggle_chart_mode", "차트모드"),
        ("i", "toggle_interval", "봉주기"),
        ("v", "cycle_view", "화면전환"),
        ("q", "quit", "종료"),
    ]

    _VIEW_MODES = ["both", "chart", "list"]
    _VIEW_MODE_LABELS = {
        "both": "전체 보기",
        "chart": "차트만 보기",
        "list": "목록만 보기",
    }

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.client = TossInvestClient(
            settings.client_id, settings.client_secret, TOSS_API_BASE_URL
        )
        ensure_data_dir()
        self.watchlist = Watchlist.load(WATCHLIST_PATH)
        self._view_mode = "both"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            yield WatchlistTable(id="watchlist", stale_after=max(30.0, self.settings.poll_interval_seconds * 3))
            yield ChartPanel(id="chart")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one(WatchlistTable)
        table.sync_rows(self.watchlist)
        self.poll_prices()
        self.poll_chart()

    async def on_unmount(self) -> None:
        await self.client.aclose()

    # -- polling workers ----------------------------------------------------

    @work(exclusive=True, group="prices")
    async def poll_prices(self) -> None:
        table = self.query_one(WatchlistTable)
        while True:
            await self._refresh_prices(table)
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def _refresh_prices(self, table: WatchlistTable) -> None:
        symbols = self.watchlist.symbols
        if not symbols:
            return
        await self._refresh_stale_prev_closes(symbols)
        stock_symbols = [s for s in symbols if not self.watchlist.items[s].is_index]
        index_symbols = [s for s in symbols if self.watchlist.items[s].is_index]
        errors = []
        for requested, fetch in (
            (stock_symbols, self.client.get_prices),
            (index_symbols, self.client.get_market_indicator_prices),
        ):
            if not requested:
                continue
            try:
                prices = await fetch(requested)
                price_map = {p.symbol: p for p in prices if p.symbol in requested}
                triggered = table.apply_prices(price_map, self.watchlist)
                table.mark_missing(requested, set(price_map))
                for symbol in triggered:
                    table.flash(symbol)
                if triggered:
                    self.bell()
            except TossAPIError as exc:
                table.mark_failed(requested)
                errors.append(str(exc))
        self.sub_title = f"시세 오류: {'; '.join(errors)}" if errors else ""

    async def _refresh_stale_prev_closes(self, symbols: list[str]) -> None:
        """Re-fetch prev_close for items whose baseline predates today.

        prev_close is a snapshot taken when a symbol was added (or last
        refreshed); without this, change %/amount would keep comparing the
        live price against a stale, ever-older baseline instead of resetting
        to yesterday's close each trading day. Staleness is judged per-market
        (see client.trading_day_key) since the correct baseline flips twice
        a day, not once: at the regular-session close (yesterday's close ->
        today's own close, for after-hours) and again at midnight (today's
        close -> tomorrow's pre-market baseline). Missing the close-time
        flip is what left after-hours prices comparing against a stale,
        already-superseded baseline for the rest of the session.

        One symbol's fetch failing (API error, unexpected candle data, ...)
        must never abort this loop: it runs inside the same exclusive worker
        as the live price poll, so an uncaught exception here doesn't just
        skip that symbol - it kills the worker outright and every symbol's
        price/change freezes at its last value until the app is restarted.
        Catch broadly per-symbol rather than trusting only TossAPIError.
        """
        for symbol in symbols:
            item = self.watchlist.items[symbol]

            if item.is_index:
                # KOSPI/KOSDAQ both run on the KRX session, same calendar
                # as KRW stocks - see client.INDEX_CATALOG.
                today = _trading_day_key("KRW")
                if item.prev_close_date == today:
                    continue
                try:
                    prev_close = await self.client.get_index_previous_close(symbol)
                except Exception:
                    continue
                self.watchlist.set_prev_close(symbol, prev_close, today)
                continue

            if not item.currency:
                # Legacy items saved before currency was tracked.
                try:
                    stocks = await self.client.get_stocks([symbol])
                except Exception:
                    stocks = []
                if stocks:
                    item.currency = stocks[0].currency
                    self.watchlist.set_currency(symbol, item.currency)

            today = _trading_day_key(item.currency)
            if item.prev_close_date == today:
                continue
            try:
                prev_close = await self.client.get_previous_close(
                    symbol, item.currency
                )
            except Exception:
                continue
            self.watchlist.set_prev_close(symbol, prev_close, today)

    @work(exclusive=True, group="chart-poll")
    async def poll_chart(self) -> None:
        while True:
            await asyncio.sleep(self.settings.chart_refresh_seconds)
            self.load_chart()

    @work(exclusive=True, group="chart")
    async def load_chart(self) -> None:
        table = self.query_one(WatchlistTable)
        chart = self.query_one(ChartPanel)
        symbol = table.selected_symbol
        if not symbol:
            return
        item = self.watchlist.items.get(symbol)
        try:
            if item is not None and item.is_index:
                candles = await self.client.get_market_indicator_candles(
                    symbol, interval=chart.interval, count=100
                )
            else:
                candles = await self.client.get_candles(
                    symbol, interval=chart.interval, count=100
                )
            chart.show_candles(symbol, candles)
        except TossAPIError as exc:
            self.sub_title = f"차트 오류: {exc}"

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.load_chart()

    # -- actions --------------------------------------------------------------

    def action_add_symbol(self) -> None:
        self.push_screen(AddSymbolScreen(self.client), self._on_symbol_added)

    def _on_symbol_added(self, result: AddSymbolResult | None) -> None:
        if result is None:
            return
        item = WatchlistItem(
            symbol=result.symbol,
            name=result.name,
            prev_close=result.prev_close,
            prev_close_date=_trading_day_key(
                "KRW" if result.is_index else result.currency
            ),
            currency=result.currency,
            is_index=result.is_index,
        )
        self.watchlist.add(item)
        self.query_one(WatchlistTable).sync_rows(self.watchlist)
        self._refresh_one(result.symbol)

    @work(exclusive=False, group="refresh-one")
    async def _refresh_one(self, symbol: str) -> None:
        try:
            item = self.watchlist.items.get(symbol)
            if item is not None and item.is_index:
                prices = await self.client.get_market_indicator_prices([symbol])
            else:
                prices = await self.client.get_prices([symbol])
            table = self.query_one(WatchlistTable)
            price_map = {p.symbol: p for p in prices if p.symbol == symbol}
            table.apply_prices(price_map, self.watchlist)
            table.mark_missing([symbol], set(price_map))
        except TossAPIError:
            self.query_one(WatchlistTable).mark_failed([symbol])

    def action_delete_symbol(self) -> None:
        table = self.query_one(WatchlistTable)
        symbol = table.selected_symbol
        if not symbol:
            return
        self.watchlist.remove(symbol)
        table.sync_rows(self.watchlist)

    def action_set_alert(self) -> None:
        table = self.query_one(WatchlistTable)
        symbol = table.selected_symbol
        if not symbol:
            return
        item = self.watchlist.items[symbol]

        def callback(result: AlertResult | None) -> None:
            self._on_alert_set(symbol, result)

        self.push_screen(
            SetAlertScreen(symbol, item.alert_upper, item.alert_lower), callback
        )

    def _on_alert_set(self, symbol: str, result: AlertResult | None) -> None:
        if result is None:
            return
        self.watchlist.set_alerts(symbol, result.upper, result.lower)

    def action_cycle_view(self) -> None:
        idx = self._VIEW_MODES.index(self._view_mode)
        self._view_mode = self._VIEW_MODES[(idx + 1) % len(self._VIEW_MODES)]

        table = self.query_one(WatchlistTable)
        chart = self.query_one(ChartPanel)
        table.display = self._view_mode in ("both", "list")
        chart.display = self._view_mode in ("both", "chart")

        self.notify(self._VIEW_MODE_LABELS[self._view_mode], timeout=1.5)

    def action_toggle_chart_mode(self) -> None:
        self.query_one(ChartPanel).toggle_mode()

    def action_toggle_interval(self) -> None:
        chart = self.query_one(ChartPanel)
        chart.toggle_interval()
        self.load_chart()
