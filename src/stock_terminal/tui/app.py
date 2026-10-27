"""Main Textual application: layout, keybindings, polling workers."""

from __future__ import annotations

import asyncio
import time

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header

from stock_terminal.api.client import TossInvestClient, trading_day_key as _trading_day_key
from stock_terminal.api.exceptions import TossAPIError
from stock_terminal.config import TOSS_API_BASE_URL, WATCHLIST_PATH, Settings, ensure_data_dir
from stock_terminal.storage.watchlist import Watchlist, WatchlistItem, WatchlistError
from stock_terminal.tui.screens.add_symbol import AddSymbolResult, AddSymbolScreen
from stock_terminal.tui.screens.alert_history import AlertHistoryScreen
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
        ("h", "alert_history", "알림이력"),
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
        ensure_data_dir()
        self.watchlist = Watchlist.load(WATCHLIST_PATH)
        self.client = TossInvestClient(
            settings.client_id, settings.client_secret, TOSS_API_BASE_URL
        )
        self._view_mode = "both"
        self._baseline_retry: dict[str, tuple[str, float]] = {}
        self._baseline_failures: dict[str, int] = {}
        self._baseline_slots = asyncio.Semaphore(3)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            yield WatchlistTable(id="watchlist", stale_after=max(30.0, self.settings.poll_interval_seconds * 3))
            yield ChartPanel(id="chart")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one(WatchlistTable)
        table.sync_rows(self.watchlist)
        if self.watchlist.recovery_notice:
            self.notify(self.watchlist.recovery_notice, title="관심목록 복구", severity="warning", timeout=20)
        self.poll_prices()
        self.poll_baselines()
        self.poll_chart()

    async def on_unmount(self) -> None:
        workers = list(self.workers)
        self.workers.cancel_all()
        await asyncio.gather(*(worker.wait() for worker in workers), return_exceptions=True)
        await self.client.aclose()

    def _persist(self, operation, *args, **kwargs) -> bool:
        try:
            operation(*args, **kwargs)
            return True
        except WatchlistError as exc:
            self.notify(str(exc), title="저장 오류", severity="error", timeout=10)
            return False

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
                table.apply_prices(price_map, self.watchlist)
                table.mark_missing(requested, set(price_map))
            except TossAPIError as exc:
                table.mark_failed(requested)
                errors.append(str(exc))
        self.sub_title = f"시세 오류: {'; '.join(errors)}" if errors else ""

    @work(exclusive=True, group="baselines")
    async def poll_baselines(self) -> None:
        while True:
            await self._refresh_stale_prev_closes(self.watchlist.symbols)
            await asyncio.sleep(1.0)

    async def _refresh_stale_prev_closes(self, symbols: list[str]) -> None:
        """Run a bounded pool independently of live-price polling.

        TaskGroup ensures queued and active lookups are cancelled together on
        shutdown. Each lookup rechecks eligibility after acquiring a slot.
        """
        pending = iter([(symbol, self.watchlist.items.get(symbol))
                        for symbol in dict.fromkeys(symbols)])

        async def consume() -> None:
            for symbol, item in pending:
                if item is not None:
                    await self._refresh_baseline(symbol, item)

        async with asyncio.TaskGroup() as tasks:
            for _ in range(min(3, len(symbols))):
                tasks.create_task(consume())

    async def _refresh_baseline(self, symbol: str, item: WatchlistItem) -> None:
        async with self._baseline_slots:
            if self.watchlist.items.get(symbol) is not item:
                return
            today = _trading_day_key("KRW" if item.is_index else item.currency)
            if (item.prev_close_date == today and item.prev_close is not None
                    and item.prev_close.is_finite() and item.prev_close > 0
                    and item.prev_close_session is not None
                    and item.prev_close_source == "minute_estimate"):
                return
            retry_key, retry_at = self._baseline_retry.get(symbol, ("", 0.0))
            if retry_key == today and time.monotonic() < retry_at:
                return
            if retry_key != today:
                self._baseline_failures.pop(symbol, None)
            reference = None
            try:
                if not item.currency and not item.is_index:
                    stocks = await self.client.get_stocks([symbol])
                    if self.watchlist.items.get(symbol) is not item:
                        return
                    info = next((stock for stock in stocks if stock.symbol == symbol), None)
                    if info is None or not info.currency:
                        raise TossAPIError("종목 통화 정보를 찾을 수 없습니다.")
                    if not self._persist(self.watchlist.set_currency, symbol, info.currency):
                        self._baseline_retry[symbol] = (today, time.monotonic() + 60.0)
                        return
                    today = _trading_day_key(item.currency)
                reference = await self.client.get_close_reference(
                    symbol, item.currency, is_index=item.is_index
                )
            except TossAPIError:
                pass
            if self.watchlist.items.get(symbol) is not item:
                return
            if reference is not None:
                if not self._persist(
                    self.watchlist.set_prev_close, symbol, reference.value, today,
                    session_date=reference.session_date, source=reference.source,
                    confirmed=reference.confirmed,
                ):
                    self._baseline_retry[symbol] = (today, time.monotonic() + 60.0)
                    return
                self._baseline_failures.pop(symbol, None)
                delay = 60.0  # Retry a daily fallback; minute estimates are cached.
            else:
                failures = min(self._baseline_failures.get(symbol, 0) + 1, 5)
                self._baseline_failures[symbol] = failures
                delay = min(60.0 * 2 ** (failures - 1), 900.0)
            # Measure from completion, so slow failures never cause immediate retries.
            self._baseline_retry[symbol] = (today, time.monotonic() + delay)

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
            prev_close_date=(_trading_day_key(
                "KRW" if result.is_index else result.currency
            ) if result.reference else None),
            prev_close_session=result.reference.session_date if result.reference else None,
            prev_close_source=result.reference.source if result.reference else "unknown",
            prev_close_confirmed=result.reference.confirmed if result.reference else False,
            currency=result.currency,
            is_index=result.is_index,
        )
        self._baseline_retry.pop(result.symbol, None)
        self._baseline_failures.pop(result.symbol, None)
        if not self._persist(self.watchlist.add, item):
            return
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
        if not self._persist(self.watchlist.remove, symbol):
            return
        self._baseline_retry.pop(symbol, None)
        self._baseline_failures.pop(symbol, None)
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
        item = self.watchlist.items.get(symbol)
        if item is None:
            return
        changed = (item.alert_upper, item.alert_lower) != (result.upper, result.lower)
        if not self._persist(self.watchlist.set_alerts, symbol, result.upper, result.lower):
            return
        if changed:
            self.query_one(WatchlistTable).alerts.reset(symbol)

    def on_watchlist_table_alert_triggered(self, message: WatchlistTable.AlertTriggered) -> None:
        event = message.event
        self.query_one(WatchlistTable).flash(event.symbol)
        self.bell()
        direction = "상한 이상" if event.direction == "upper" else "하한 이하"
        self.notify(f"{event.symbol}: {direction} ({event.price} / 기준 {event.threshold})", title="가격 알림")

    def action_alert_history(self) -> None:
        self.push_screen(AlertHistoryScreen(tuple(self.query_one(WatchlistTable).alerts.history)))

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
