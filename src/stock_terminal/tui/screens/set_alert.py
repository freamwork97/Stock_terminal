"""Modal: set upper/lower price alert thresholds for a symbol."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label


@dataclass(slots=True)
class AlertResult:
    upper: Decimal | None
    lower: Decimal | None


class SetAlertScreen(ModalScreen[AlertResult | None]):
    """Prompt for upper/lower alert prices; blank clears that threshold."""

    DEFAULT_CSS = """
    SetAlertScreen {
        align: center middle;
    }
    #dialog {
        width: 50;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #status {
        height: auto;
        margin-top: 1;
        color: $error;
    }
    #buttons {
        height: auto;
        margin-top: 1;
        align-horizontal: right;
    }
    """

    def __init__(
        self, symbol: str, upper: Decimal | None, lower: Decimal | None
    ) -> None:
        super().__init__()
        self._symbol = symbol
        self._initial_upper = upper
        self._initial_lower = lower

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"{self._symbol} 알림 설정 (비워두면 해제)")
            yield Label("상한가 (이상이면 알림)")
            yield Input(
                value=str(self._initial_upper) if self._initial_upper else "",
                placeholder="예: 80000",
                id="upper-input",
            )
            yield Label("하한가 (이하이면 알림)")
            yield Input(
                value=str(self._initial_lower) if self._initial_lower else "",
                placeholder="예: 60000",
                id="lower-input",
            )
            yield Label("", id="status")
            with Horizontal(id="buttons"):
                yield Button("저장", id="confirm", variant="success")
                yield Button("취소", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id != "confirm":
            return

        status = self.query_one("#status", Label)
        upper_raw = self.query_one("#upper-input", Input).value.strip()
        lower_raw = self.query_one("#lower-input", Input).value.strip()

        try:
            upper = Decimal(upper_raw) if upper_raw else None
            lower = Decimal(lower_raw) if lower_raw else None
        except InvalidOperation:
            status.update("숫자만 입력하세요.")
            return

        if upper is not None and lower is not None and upper <= lower:
            status.update("상한가는 하한가보다 커야 합니다.")
            return

        self.dismiss(AlertResult(upper=upper, lower=lower))
