"""stock-terminal: real-time watchlist TUI backed by the Toss Invest Open API."""

from __future__ import annotations

import sys


def main() -> None:
    # Windows consoles often default stdout to a legacy codepage (e.g. cp949)
    # instead of UTF-8, which garbles the Korean setup message below.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    from stock_terminal.config import ConfigError, Settings

    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(str(exc))
        sys.exit(1)

    from stock_terminal.tui.app import StockTerminalApp

    StockTerminalApp(settings).run()
