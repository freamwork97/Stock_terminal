"""Headless smoke test: the app mounts, shows widgets, and quits cleanly.

Uses an empty watchlist so the polling workers take their early-return path
(no symbols -> no network calls), keeping this test fully offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stock_terminal.config import Settings
from stock_terminal.tui.app import StockTerminalApp
from stock_terminal.tui.widgets.chart_panel import ChartPanel
from stock_terminal.tui.widgets.watchlist_table import WatchlistTable


@pytest.fixture
def fake_settings() -> Settings:
    return Settings(
        client_id="fake-id",
        client_secret="fake-secret",
        poll_interval_seconds=999,
        chart_refresh_seconds=999,
    )


async def test_app_mounts_and_quits(
    fake_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json"
    )

    app = StockTerminalApp(fake_settings)
    try:
        async with app.run_test() as pilot:
            assert app.query_one(WatchlistTable) is not None
            assert app.query_one(ChartPanel) is not None
            await pilot.press("q")
    finally:
        await app.client.aclose()
