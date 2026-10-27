"""Searchable command menu derived from the application's actual bindings."""
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label


class ShortcutsScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss", "닫기", priority=True),
                Binding("f1", "dismiss", "닫기", priority=True)]
    DEFAULT_CSS = """
    ShortcutsScreen { align: center middle; }
    #shortcuts-dialog { width: 80%; max-width: 90; height: 80%; background: $surface; border: thick $accent; padding: 1; }
    #shortcut-results { height: 1fr; }
    #shortcut-hint { height: auto; color: $text-muted; }
    """

    def __init__(self, entries: tuple[tuple[str, str, str], ...]):
        super().__init__()
        self.entries = entries

    def compose(self) -> ComposeResult:
        with Vertical(id="shortcuts-dialog"):
            yield Label("명령 찾기")
            yield Input(placeholder="기능명 또는 단축키 검색", id="shortcut-query")
            yield DataTable(id="shortcut-results")
            yield Label("Enter: 결과로 이동·명령 실행 / Esc: 닫기", id="shortcut-hint")
            yield Button("닫기", id="close-shortcuts")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("키", "기능")
        self._filter("")
        self.query_one(Input).focus()

    def _filter(self, query: str) -> None:
        table = self.query_one(DataTable)
        table.clear()
        query = query.casefold().strip()
        for key, action, description in self.entries:
            display_key = {"slash": "/", "escape": "Esc", "ctrl+enter": "Ctrl+Enter"}.get(key, key)
            if query in f"{display_key} {description}".casefold():
                table.add_row(display_key, description, key=action)
        self.query_one("#shortcut-hint", Label).update(
            "Enter: 결과로 이동·명령 실행 / Esc: 닫기" if table.row_count else "일치하는 명령이 없습니다.")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "shortcut-query":
            self._filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self.query_one(DataTable).row_count:
            self.query_one(DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.dismiss(str(event.row_key.value))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-shortcuts":
            self.dismiss(None)
