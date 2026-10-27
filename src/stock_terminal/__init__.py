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

    from stock_terminal.config import ConfigError, Settings, DATA_DIR
    from stock_terminal.diagnostics import configure_logging, close_logging, record

    notice = configure_logging(DATA_DIR / "logs")
    if notice:
        print(notice, file=sys.stderr)

    try:
        settings = Settings.load()
    except ConfigError as exc:
        record("startup_failed", "configuration", error=exc)
        close_logging()
        print(str(exc))
        sys.exit(1)

    from stock_terminal.tui.app import StockTerminalApp

    from stock_terminal.storage.watchlist import WatchlistError

    try:
        app = StockTerminalApp(settings)
    except (WatchlistError, OSError) as exc:
        record("startup_failed", "watchlist", error=exc)
        close_logging()
        print(f"관심목록 초기화 실패: {exc}", file=sys.stderr)
        sys.exit(1)
    record("app_started", "runtime")
    try:
        app.run()
    except Exception as exc:
        record("app_crashed", "runtime", error=exc)
        raise
    finally:
        record("app_stopped", "runtime")
        close_logging()
