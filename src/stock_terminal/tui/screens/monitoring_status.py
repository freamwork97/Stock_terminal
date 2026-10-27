"""On-demand live monitoring status."""
from collections.abc import Callable

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class MonitoringStatusScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "닫기")]
    DEFAULT_CSS = """
    MonitoringStatusScreen { align: center middle; }
    #monitoring-status { width: 85%; max-width: 100; height: auto; max-height: 85%; background: $surface; border: thick $accent; padding: 1 2; }
    #monitoring-details { height: auto; margin: 1 0; }
    """

    def __init__(self, provider: Callable[[], str]):
        super().__init__()
        self.provider = provider

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="monitoring-status"):
            yield Label("모니터링 상태")
            yield Static("", id="monitoring-details", markup=False)
            yield Button("닫기", id="close-monitoring-status")

    def on_mount(self) -> None:
        self._update_status()
        self.set_interval(1.0, self._update_status)

    def _update_status(self) -> None:
        self.query_one("#monitoring-details", Static).update(self.provider())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-monitoring-status":
            self.dismiss()
