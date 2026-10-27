"""Read-only session alert history."""
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label

from stock_terminal.alerts import AlertEvent
from stock_terminal.tui.widgets.watchlist_table import _format_amount


class AlertHistoryScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "닫기")]
    DEFAULT_CSS = """
    AlertHistoryScreen { align: center middle; }
    #alert-history { width: 90%; height: 80%; background: $surface; border: thick $accent; padding: 1; }
    #alert-events { height: 1fr; }
    """

    def __init__(self, events: tuple[AlertEvent, ...]) -> None:
        super().__init__()
        self.events = events

    def compose(self) -> ComposeResult:
        with Vertical(id="alert-history"):
            yield Label("알림 이력 (이번 실행, 최근 100건)" if self.events else "알림 이력이 없습니다 (이번 실행)")
            yield DataTable(id="alert-events")
            yield Button("닫기", id="close-history")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("발생시각 (로컬)", "심볼", "조건", "현재가", "기준가")
        for event in self.events:
            table.add_row(event.occurred_at.astimezone().strftime("%m-%d %H:%M:%S"),
                          event.symbol, "상한 이상" if event.direction == "upper" else "하한 이하",
                          _format_amount(event.price, event.currency),
                          _format_amount(event.threshold, event.currency))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-history":
            self.dismiss()
