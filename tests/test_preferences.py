import json
from unittest.mock import Mock

import pytest

from stock_terminal.config import Settings
from stock_terminal.storage.preferences import Preferences, PreferencesError
from stock_terminal.tui.app import StockTerminalApp
from stock_terminal.tui.widgets.watchlist_table import WatchlistTable
from stock_terminal.tui.widgets.chart_panel import ChartPanel


def test_preferences_roundtrip_and_missing_defaults(tmp_path):
    path = tmp_path / "preferences.json"
    assert Preferences.load(path) == (Preferences(), None)
    expected = Preferences("list", "등락률순", "line", "1m")
    expected.save(path)
    assert Preferences.load(path) == (expected, None)


@pytest.mark.parametrize("content", ["broken", "[]", '{"view_mode":null,"sort_mode":[],"chart_interval":1}'])
def test_invalid_preferences_use_defaults_with_notice(tmp_path, content):
    path = tmp_path / "preferences.json"
    path.write_text(content, encoding="utf-8")
    preferences, notice = Preferences.load(path)
    assert preferences == Preferences()
    assert notice


def test_preference_write_failure_keeps_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    Preferences().save(path)
    original = path.read_bytes()
    def fail(*args):
        raise PermissionError("locked")
    monkeypatch.setattr("stock_terminal.storage.watchlist.os.replace", fail)
    with pytest.raises(PreferencesError):
        Preferences(view_mode="chart").save(path)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


async def test_display_preferences_are_restored_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("stock_terminal.tui.app.WATCHLIST_PATH", tmp_path / "watchlist.json")
    monkeypatch.setattr(StockTerminalApp, "poll_prices", lambda self: None)
    monkeypatch.setattr(StockTerminalApp, "poll_baselines", lambda self: None)
    settings = Settings("id", "secret", 999, 999)
    first = StockTerminalApp(settings)
    try:
        async with first.run_test() as pilot:
            await pilot.press("s", "m", "i", "v")
        second = StockTerminalApp(settings)
        try:
            async with second.run_test():
                assert second.query_one(WatchlistTable).sort_mode == "심볼순"
                assert second.query_one(ChartPanel).mode == "line"
                assert second.query_one(ChartPanel).interval == "1m"
                assert second._view_mode == "chart"
                assert not second.query_one(WatchlistTable).display
                assert second.query_one(ChartPanel).display
        finally:
            await second.client.aclose()
    finally:
        await first.client.aclose()


def test_sound_mute_roundtrip_and_strict_boolean(tmp_path):
    path = tmp_path / "preferences.json"
    Preferences(sound_muted=True).save(path)
    assert Preferences.load(path)[0].sound_muted is True
    path.write_text('{"sound_muted": 1}', encoding="utf-8")
    preferences, notice = Preferences.load(path)
    assert preferences.sound_muted is False
    assert notice
