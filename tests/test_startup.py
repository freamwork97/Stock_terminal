"""Unrecoverable storage failures produce an actionable startup message."""
from unittest.mock import Mock

import pytest

from stock_terminal import main
from stock_terminal.config import Settings
from stock_terminal.storage.watchlist import WatchlistError


def test_unrecoverable_watchlist_has_friendly_startup_error(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("stock_terminal.config.DATA_DIR", tmp_path)
    monkeypatch.setattr(Settings, "load", lambda: Settings("id", "secret", 3, 30))
    monkeypatch.setattr("stock_terminal.tui.app.StockTerminalApp",
                        Mock(side_effect=WatchlistError("원본 확인: watchlist.json / watchlist.json.bak")))
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert "watchlist.json.bak" in capsys.readouterr().err
