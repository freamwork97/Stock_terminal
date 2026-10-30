"""Main Textual application: layout, keybindings, polling workers."""

from __future__ import annotations

import asyncio
import time

from textual import work, events
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Header, Input, Static, Button

from stock_terminal.api.client import INDEX_CATALOG, TossInvestClient, trading_day_key as _trading_day_key
from stock_terminal.api.exceptions import TossAPIError
from stock_terminal.polling import PollingHealth
from stock_terminal.diagnostics import HealthReporter, record
from textual.worker import Worker, WorkerState
from stock_terminal.config import TOSS_API_BASE_URL, WATCHLIST_PATH, Settings, ensure_data_dir
from stock_terminal.storage.preferences import Preferences, PreferencesError
from stock_terminal.storage.watchlist import Watchlist, WatchlistItem, WatchlistError
from stock_terminal.tui.screens.add_symbol import AddSymbolResult
from stock_terminal.tui.screens.alert_history import AlertHistoryScreen
from stock_terminal.tui.screens.monitoring_status import MonitoringStatusScreen
from stock_terminal.tui.screens.shortcuts import ShortcutsScreen
from stock_terminal.tui.screens.set_alert import AlertResult, SetAlertScreen
from stock_terminal.tui.widgets.chart_panel import ChartPanel
from stock_terminal.tui.widgets.watchlist_table import WatchlistTable


class StockTerminalApp(App):
    """Real-time stock watchlist terminal backed by the Toss Invest Open API."""

    TITLE = "Stock Terminal"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        ("a", "add_symbol", "검색·추가"),
        ("d", "delete_symbol", "삭제"),
        ("u", "undo_delete", "삭제취소"),
        ("t", "set_alert", "알림설정"),
        ("h", "alert_history", "알림이력"),
        ("o", "show_status", "모니터링 상태 (관심종목·시세·수신·알림)"),
        ("b", "toggle_sound", "음소거"),
        ("p", "toggle_symbol_alert", "알림중지/재개"),
        ("m", "toggle_chart_mode", "차트모드"),
        ("i", "toggle_interval", "봉주기"),
        ("v", "cycle_view", "화면전환"),
        ("r", "refresh_now", "새로고침"),
        ("s", "sort_watchlist", "정렬"),
        ("slash", "focus_filter", "검색"),
        ("ctrl+enter", "lookup_from_search", "종목조회"),
        ("escape", "clear_filter", "검색해제"),
        ("f1", "show_shortcuts", "명령 찾기"),
        ("question_mark", "show_shortcuts", "명령 찾기"),
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
        self._health = HealthReporter()
        self._monitoring_summary = ""
        self._monitoring_status = ""
        self._poll_health = PollingHealth()
        ensure_data_dir()
        self.watchlist = Watchlist.load(WATCHLIST_PATH)
        self.client = TossInvestClient(
            settings.client_id, settings.client_secret, TOSS_API_BASE_URL
        )
        self._preferences_path = WATCHLIST_PATH.with_name("preferences.json")
        self._preferences, self._preferences_notice = Preferences.load(self._preferences_path)
        self._view_mode = self._preferences.view_mode
        self._sound_muted = self._preferences.sound_muted
        self._deleted_item: WatchlistItem | None = None
        self._search_result: AddSymbolResult | None = None
        self._search_worker = None
        self._search_status = ""
        self._chart_target = None
        self._baseline_retry: dict[str, tuple[str, float]] = {}
        self._baseline_failures: dict[str, int] = {}
        self._baseline_slots = asyncio.Semaphore(3)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="관심종목 검색 · 없는 종목은 코드 입력 후 Enter로 조회", id="filter")
        with Horizontal(id="search-actions"):
            yield Button("종목 조회", id="search-lookup", variant="primary")
            yield Button("관심목록에 추가", id="search-add", variant="success", disabled=True)
        yield Static("", id="summary")
        yield Static("", id="poll-status")
        with Horizontal(id="panels"):
            yield WatchlistTable(id="watchlist", stale_after=max(30.0, self.settings.poll_interval_seconds * 3))
            yield ChartPanel(id="chart")

    async def on_mount(self) -> None:
        table = self.query_one(WatchlistTable)
        table.sort_mode = self._preferences.sort_mode
        chart = self.query_one(ChartPanel)
        chart.mode = self._preferences.chart_mode
        chart.interval = self._preferences.chart_interval
        self._apply_view()
        table.sync_rows(self.watchlist)
        self._update_summary()
        table.focus()
        if self._preferences_notice:
            record("preferences_invalid", "preferences")
            self.notify(self._preferences_notice, title="화면 설정", severity="warning", timeout=10)
        if self.watchlist.recovery_notice:
            record("watchlist_recovered", "watchlist", count=len(self.watchlist.items))
            self.notify(self.watchlist.recovery_notice, title="관심목록 복구", severity="warning", timeout=20)
        self.poll_prices()
        self.poll_baselines()
        self.poll_chart()
        self._update_poll_status()
        self.set_interval(1.0, self._update_poll_status)
        self.set_interval(60.0, self._log_poll_status)

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
            record("operation_failed", "watchlist", error=exc)
            self.notify(str(exc), title="저장 오류", severity="error", timeout=10)
            return False

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state == WorkerState.ERROR:
            record("worker_failed", "worker", error=event.worker.error)

    # -- polling workers ----------------------------------------------------

    @work(exclusive=True, group="prices")
    async def poll_prices(self) -> None:
        table = self.query_one(WatchlistTable)
        while True:
            await self._refresh_prices(table)
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def _refresh_prices(self, table: WatchlistTable) -> None:
        items = dict(self.watchlist.items)
        if not items:
            return
        self._poll_health.begin()
        cancelled = False
        try:
            await self._fetch_price_groups(table, items)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self._poll_health.finish(cancelled=cancelled)
            self._update_poll_status()

    async def _fetch_price_groups(self, table: WatchlistTable, items: dict[str, WatchlistItem]) -> None:
        stock_symbols = [s for s, item in items.items() if not item.is_index]
        index_symbols = [s for s, item in items.items() if item.is_index]
        errors = []
        async def fetch_group(requested, fetch, operation):
            if not requested:
                return
            try:
                prices = await fetch(requested)
                current = {s for s in requested if self.watchlist.items.get(s) is items[s]}
                price_map = {p.symbol: p for p in prices if p.symbol in current}
                self._poll_health.receive(len(price_map), missing=len(current - price_map.keys()))
                table.apply_prices(price_map, self.watchlist)
                table.mark_missing(list(current), set(price_map))
                self._health.report(operation, missing=len(current - price_map.keys()))
            except TossAPIError as exc:
                self._poll_health.receive(0, failure=sum(self.watchlist.items.get(s) is items[s] for s in requested))
                self._health.report(operation, error=exc)
                table.mark_failed([s for s in requested if self.watchlist.items.get(s) is items[s]])
                errors.append(str(exc))
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(fetch_group(stock_symbols, self.client.get_prices, "stock_prices"))
            tasks.create_task(fetch_group(index_symbols, self.client.get_market_indicator_prices, "index_prices"))
        table.border_subtitle = f"시세 오류: {'; '.join(errors)}" if errors else ""
        self._update_summary()

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
            failure = None
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
            except TossAPIError as exc:
                failure = exc
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
                record("baseline_updated", "baseline", count=1)
                self._baseline_failures.pop(symbol, None)
                delay = 60.0  # Retry a daily fallback; minute estimates are cached.
            else:
                failures = min(self._baseline_failures.get(symbol, 0) + 1, 5)
                self._baseline_failures[symbol] = failures
                delay = min(60.0 * 2 ** (failures - 1), 900.0)
                record("baseline_retry", "baseline", error=failure, count=failures, delay=delay)
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
        self._chart_target = (symbol, chart.interval, id(self.watchlist.items.get(symbol)))
        if not symbol:
            chart.clear_chart("표시할 종목 없음 — a: 추가 / Esc: 검색 해제")
            return
        item = self.watchlist.items.get(symbol)
        interval = chart.interval
        keep_data = chart.symbol == symbol and chart.data_interval == interval and bool(chart._candles)
        if not keep_data:
            chart.clear_chart(f"{symbol} 차트 조회 중…")
        chart.border_subtitle = "갱신 중…"
        try:
            if item is not None and item.is_index:
                candles = await self.client.get_market_indicator_candles(
                    symbol, interval=interval, count=100
                )
            else:
                candles = await self.client.get_candles(
                    symbol, interval=interval, count=100
                )
            if (table.selected_symbol == symbol and chart.interval == interval
                    and self.watchlist.items.get(symbol) is item):
                chart.show_candles(symbol, candles)
                chart.border_subtitle = ""
                self._health.report("chart")
        except TossAPIError as exc:
            self._health.report("chart", error=exc)
            if (table.selected_symbol == symbol and chart.interval == interval
                    and self.watchlist.items.get(symbol) is item):
                if not keep_data:
                    chart.clear_chart(f"{symbol} 차트 조회 실패 — r: 재시도")
                chart.border_subtitle = str(exc)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        symbol = self.query_one(WatchlistTable).selected_symbol
        target = (symbol, self.query_one(ChartPanel).interval, id(self.watchlist.items.get(symbol)))
        if target != self._chart_target:
            self._chart_target = target
            self.load_chart()

    # -- actions --------------------------------------------------------------

    def action_add_symbol(self) -> None:
        self.action_focus_filter()

    def _on_symbol_added(self, result: AddSymbolResult | None) -> None:
        if result is None:
            return
        if result.symbol in self.watchlist.items:
            self.notify(f"{result.symbol}은 이미 관심목록에 있습니다.")
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
        self._update_summary()
        self._refresh_one(result.symbol)

    @work(exclusive=False, group="refresh-one")
    async def _refresh_one(self, symbol: str) -> None:
        table = self.query_one(WatchlistTable)
        item = self.watchlist.items.get(symbol)
        if item is None:
            return
        try:
            if item.is_index:
                prices = await self.client.get_market_indicator_prices([symbol])
            else:
                prices = await self.client.get_prices([symbol])
            if self.watchlist.items.get(symbol) is not item:
                return
            price_map = {p.symbol: p for p in prices if p.symbol == symbol}
            table.apply_prices(price_map, self.watchlist)
            table.mark_missing([symbol], set(price_map))
            self._health.report("single_price", missing=int(symbol not in price_map))
        except TossAPIError as exc:
            if self.watchlist.items.get(symbol) is item:
                self._health.report("single_price", error=exc)
                table.mark_failed([symbol])

    def action_delete_symbol(self) -> None:
        table = self.query_one(WatchlistTable)
        symbol = table.selected_symbol
        if not symbol:
            return
        deleted = self.watchlist.items[symbol]
        if not self._persist(self.watchlist.remove, symbol):
            return
        self._deleted_item = deleted
        self.notify(f"{symbol} 삭제됨 — u 키로 취소", timeout=5)
        self._baseline_retry.pop(symbol, None)
        self._baseline_failures.pop(symbol, None)
        table.sync_rows(self.watchlist)
        self._update_summary()
        self.load_chart()

    def action_undo_delete(self) -> None:
        item = self._deleted_item
        if item is None:
            self.notify("취소할 삭제가 없습니다.", timeout=2)
            return
        if item.symbol in self.watchlist.items:
            self.notify("이미 관심목록에 있는 종목입니다.", timeout=2)
            return
        if self._persist(self.watchlist.add, item):
            self._deleted_item = None
            self.query_one(WatchlistTable).sync_rows(self.watchlist)
            self._update_summary()
            self._refresh_one(item.symbol)

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
        self.query_one(WatchlistTable).sync_rows(self.watchlist)

    def on_watchlist_table_alert_triggered(self, message: WatchlistTable.AlertTriggered) -> None:
        event = message.event
        item = self.watchlist.items.get(event.symbol)
        if item is None or not item.alerts_enabled:
            return
        self.query_one(WatchlistTable).flash(event.symbol)
        if not self._sound_muted:
            self.bell()
        direction = "상한 이상" if event.direction == "upper" else "하한 이하"
        self.notify(f"{event.symbol}: {direction} ({event.price} / 기준 {event.threshold})", title="가격 알림")

    def action_toggle_sound(self) -> None:
        previous = self._sound_muted
        self._sound_muted = not previous
        if not self._save_preferences():
            self._sound_muted = previous
        self._update_poll_status()

    def action_toggle_symbol_alert(self) -> None:
        table = self.query_one(WatchlistTable)
        symbol = table.selected_symbol
        if symbol is None:
            return
        item = self.watchlist.items[symbol]
        if self._persist(self.watchlist.set_alerts_enabled, symbol, not item.alerts_enabled):
            table.alerts.reset(symbol)
            table.sync_rows(self.watchlist)
            self.notify(f"{symbol} 알림 {'재개' if item.alerts_enabled else '중지'}", timeout=2)

    def action_show_shortcuts(self) -> None:
        if isinstance(self.screen, ShortcutsScreen):
            return
        # Aliases share one menu row; show the actual action and primary key.
        entries = {}
        for key, action, label in self.BINDINGS:
            if action != "show_shortcuts":
                action = "focus_filter" if action == "add_symbol" else action
                key = {"slash": "/", "escape": "Esc", "ctrl+enter": "Ctrl+Enter"}.get(key, key)
                if action in entries:
                    previous_key, _, previous_label = entries[action]
                    entries[action] = (f"{previous_key}, {key}", action, previous_label)
                else:
                    entries[action] = (key, action, label)
        async def execute(action: str | None) -> None:
            if action is not None:
                await self.run_action(action)
        self.push_screen(ShortcutsScreen(tuple(entries.values())), execute)

    def action_show_status(self) -> None:
        self.push_screen(MonitoringStatusScreen(self._monitoring_details))

    def _monitoring_details(self) -> str:
        self._update_summary()
        self._update_poll_status()
        snapshot = self._poll_health.snapshot(self.settings.poll_interval_seconds, bool(self.watchlist.items))
        paused = sum(not item.alerts_enabled for item in self.watchlist.items.values())
        return (f"{self._monitoring_summary}\n\n{self._monitoring_status}\n\n"
                f"알림 중지 종목 {paused}개\n"
                f"누적 성공 {snapshot['success_count']} · 실패 {snapshot['failure_count']} · 누락 {snapshot['missing_count']}")

    def action_alert_history(self) -> None:
        self.push_screen(AlertHistoryScreen(tuple(self.query_one(WatchlistTable).alerts.history)))

    def _update_poll_status(self) -> None:
        if not self.query("#poll-status"):
            return
        snapshot = self._poll_health.snapshot(self.settings.poll_interval_seconds, bool(self.watchlist.items))
        labels = {"idle": "대기", "normal": "정상", "fetching": "조회 중", "slow": "폴링 지연",
                  "stalled": "갱신 중단 의심", "no_data": "수신 대기·지연", "failed": "조회 실패·누락"}
        received = self._poll_health.last_received
        received_text = received.astimezone().strftime("%m-%d %H:%M:%S") if received else "없음"
        message = (f"시세 {labels[snapshot['state']]} · 조회 {snapshot['duration_seconds']:.1f}초 "
                   f"/ 주기 {self.settings.poll_interval_seconds:g}초 · 마지막 수신 {received_text} · "
                   f"알림 소리 {'꺼짐' if self._sound_muted else '켜짐'}")
        self._monitoring_status = message
        status = self.query_one("#poll-status", Static)
        status.update(message)
        status.set_class(snapshot["state"] in ("slow", "stalled", "failed", "no_data"), "warning")

    def _log_poll_status(self) -> None:
        record("poll_status", "runtime", metrics=self._poll_health.snapshot(
            self.settings.poll_interval_seconds, bool(self.watchlist.items)))

    def _update_summary(self) -> None:
        if not self.query("#summary") or not self.query(WatchlistTable):
            return
        table = self.query_one(WatchlistTable)
        message = f"관심종목 {table.row_count}/{len(self.watchlist.items)} · {table.sort_mode}"
        if not self.watchlist.items:
            message = "관심종목이 없습니다."
        elif not table.row_count:
            message = "관심목록에 일치하는 종목이 없습니다."
        self._monitoring_summary = message
        query = self.query_one("#filter", Input).value.strip()
        feedback = self._search_status or ("검색 결과가 없습니다. 정확한 종목 코드로 조회할 수 있습니다." if query and not table.row_count else "")
        self.query_one("#summary", Static).update(feedback)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._invalidate_search()
            table = self.query_one(WatchlistTable)
            table.filter_text = event.value
            table.sync_rows(self.watchlist)
            self._update_summary()
            self.load_chart()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter":
            if self.query_one(WatchlistTable).row_count:
                self.query_one(WatchlistTable).focus()
            else:
                self.action_lookup_from_search()

    def action_focus_filter(self) -> None:
        search = self.query_one("#filter", Input)
        search.add_class("visible")
        self.query_one("#search-actions").add_class("visible")
        self.query_one("#summary").add_class("visible")
        search.focus()

    def action_clear_filter(self) -> None:
        search = self.query_one("#filter", Input)
        self._invalidate_search()
        search.value = ""
        self.query_one("#search-actions").remove_class("visible")
        self.query_one("#summary").remove_class("visible")
        search.remove_class("visible")
        self.query_one(WatchlistTable).focus()

    def _invalidate_search(self) -> None:
        if self._search_worker is not None:
            self._search_worker.cancel()
            self._search_worker = None
        self._search_result = None
        self._search_status = ""
        self.query_one("#search-add", Button).disabled = True

    @work(exclusive=True, group="search")
    async def lookup_search(self) -> None:
        self._search_result = None
        self.query_one("#search-add", Button).disabled = True
        raw = self.query_one("#filter", Input).value.strip().upper()
        symbol = {"코스피": "KOSPI", "코스닥": "KOSDAQ"}.get(raw, raw)
        if not symbol:
            return
        self._search_status = f"{symbol} 조회 중…"
        self._update_summary()
        try:
            if symbol in self.watchlist.items:
                self._search_status = f"{symbol}은 이미 관심목록에 있습니다."
            elif symbol in INDEX_CATALOG:
                prices = await self.client.get_market_indicator_prices([symbol])
                if not any(price.symbol == symbol for price in prices):
                    self._search_status = f"{symbol} 지수를 찾을 수 없습니다."
                else:
                    self._search_result = AddSymbolResult(symbol, INDEX_CATALOG[symbol], None, "PT", is_index=True)
            else:
                stocks = await self.client.get_stocks([symbol])
                info = next((stock for stock in stocks if stock.symbol == symbol), None)
                if info is None:
                    self._search_status = f"{symbol}을 찾을 수 없습니다. 정확한 종목 코드를 입력하세요."
                else:
                    self._search_result = AddSymbolResult(info.symbol, info.name, None, info.currency)
            if self._search_result is not None:
                self._search_status = f"확인됨: {self._search_result.name} ({symbol}) — Enter로 관심목록에 추가합니다."
                self._health.report("search")
                add_button = self.query_one("#search-add", Button)
                add_button.disabled = False
                if self.query_one("#filter", Input).has_focus or self.query_one("#search-lookup", Button).has_focus:
                    add_button.focus()
        except TossAPIError as exc:
            self._health.report("search", error=exc)
            self._search_status = f"조회 실패: {exc} — Enter로 다시 시도하세요."
        self._update_summary()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "search-lookup":
            self.action_lookup_from_search()
        elif event.button.id == "search-add" and self._search_result is not None:
            result = self._search_result
            self._on_symbol_added(result)
            if result.symbol in self.watchlist.items:
                self.action_clear_filter()
                table = self.query_one(WatchlistTable)
                table.move_cursor(row=table.get_row_index(result.symbol), animate=False)

    def action_lookup_from_search(self) -> None:
        search = self.query_one("#filter", Input)
        if not search.display:
            self.action_focus_filter()
            return
        if not search.value.strip():
            search.focus()
            return
        if self._search_worker is None or not self._search_worker.is_running:
            self._search_worker = self.lookup_search()

    def action_sort_watchlist(self) -> None:
        table = self.query_one(WatchlistTable)
        modes = ["등록순", "심볼순", "등락률순"]
        table.sort_mode = modes[(modes.index(table.sort_mode) + 1) % len(modes)]
        table.sort_rows()
        self._update_summary()
        self._save_preferences()

    def _save_preferences(self) -> bool:
        chart = self.query_one(ChartPanel)
        preferences = Preferences(self._view_mode, self.query_one(WatchlistTable).sort_mode,
                                  chart.mode, chart.interval, self._sound_muted)
        try:
            preferences.save(self._preferences_path)
            self._preferences = preferences
            return True
        except PreferencesError as exc:
            record("operation_failed", "preferences", error=exc)
            self.notify(str(exc), title="화면 설정", severity="error", timeout=10)
            return False

    def action_refresh_now(self) -> None:
        self.poll_prices()
        self.load_chart()
        self.notify("현재가와 차트를 새로고침합니다.", timeout=1.5)

    def on_resize(self, event: events.Resize) -> None:
        if self.query("#panels"):
            self.query_one("#panels").set_class(event.size.width < 150, "narrow")

    def action_cycle_view(self) -> None:
        idx = self._VIEW_MODES.index(self._view_mode)
        self._view_mode = self._VIEW_MODES[(idx + 1) % len(self._VIEW_MODES)]

        self._apply_view()
        self._save_preferences()
        self.notify(self._VIEW_MODE_LABELS[self._view_mode], timeout=1.5)

    def _apply_view(self) -> None:
        table = self.query_one(WatchlistTable)
        chart = self.query_one(ChartPanel)
        table.display = self._view_mode in ("both", "list")
        chart.display = self._view_mode in ("both", "chart")

    def action_toggle_chart_mode(self) -> None:
        self.query_one(ChartPanel).toggle_mode()
        self._save_preferences()

    def action_toggle_interval(self) -> None:
        chart = self.query_one(ChartPanel)
        chart.toggle_interval()
        self._save_preferences()
        self.load_chart()
