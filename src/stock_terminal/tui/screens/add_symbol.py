"""Modal: enter a symbol, validate it against the API, add it to the watchlist."""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from stock_terminal.api.client import TossInvestClient
from stock_terminal.api.exceptions import TossAPIError


@dataclass(slots=True)
class AddSymbolResult:
    symbol: str
    name: str
    prev_close: object  # Decimal | None, kept loosely typed to avoid import cycle


class AddSymbolScreen(ModalScreen[AddSymbolResult | None]):
    """Prompt for a symbol code, verify it exists, then return it for adding."""

    DEFAULT_CSS = """
    AddSymbolScreen {
        align: center middle;
    }
    #dialog {
        width: 56;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #status {
        height: auto;
        margin-top: 1;
        color: $text-muted;
    }
    #buttons {
        height: auto;
        margin-top: 1;
        align-horizontal: right;
    }
    """

    def __init__(self, client: TossInvestClient) -> None:
        super().__init__()
        self._client = client
        self._verified: AddSymbolResult | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("종목 추가 (예: 005930, AAPL)")
            yield Input(placeholder="심볼 코드 입력", id="symbol-input")
            yield Label("", id="status")
            with Horizontal(id="buttons"):
                yield Button("조회", id="lookup", variant="primary")
                yield Button("추가", id="confirm", variant="success", disabled=True)
                yield Button("취소", id="cancel")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.run_worker(self._lookup(), exclusive=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "lookup":
            self.run_worker(self._lookup(), exclusive=True)
        elif event.button.id == "confirm":
            self.dismiss(self._verified)
        elif event.button.id == "cancel":
            self.dismiss(None)

    async def _lookup(self) -> None:
        status = self.query_one("#status", Label)
        confirm_button = self.query_one("#confirm", Button)
        symbol = self.query_one("#symbol-input", Input).value.strip().upper()

        if not symbol:
            status.update("심볼을 입력하세요.")
            return

        status.update("조회 중...")
        confirm_button.disabled = True
        self._verified = None

        try:
            stocks = await self._client.get_stocks([symbol])
            if not stocks:
                status.update(f"'{symbol}' 종목을 찾을 수 없습니다.")
                return
            info = stocks[0]
            prev_close = await self._client.get_previous_close(symbol)
            self._verified = AddSymbolResult(
                symbol=info.symbol, name=info.name, prev_close=prev_close
            )
            status.update(f"확인됨: {info.name} ({info.market})")
            confirm_button.disabled = False
        except TossAPIError as exc:
            status.update(f"오류: {exc}")
