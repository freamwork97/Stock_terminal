"""Main Textual application: layout, keybindings, polling workers."""

from __future__ import annotations

import asyncio

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header

from stock_terminal.api.client import TossInvestClient
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
            yield WatchlistTable(id="watchlist")
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
        try:
            prices = await self.client.get_prices(symbols)
            price_map = {p.symbol: p for p in prices}
            triggered = table.apply_prices(price_map, self.watchlist)
            for symbol in triggered:
                table.flash(symbol)
            if triggered:
                self.bell()
            self.sub_title = ""
        except TossAPIError as exc:
            self.sub_title = f"시세 오류: {exc}"

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
        try:
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
            symbol=result.symbol, name=result.name, prev_close=result.prev_close
        )
        self.watchlist.add(item)
        self.query_one(WatchlistTable).sync_rows(self.watchlist)
        self._refresh_one(result.symbol)

    @work(exclusive=False, group="refresh-one")
    async def _refresh_one(self, symbol: str) -> None:
        try:
            prices = await self.client.get_prices([symbol])
            table = self.query_one(WatchlistTable)
            table.apply_prices({p.symbol: p for p in prices}, self.watchlist)
        except TossAPIError:
            pass

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
