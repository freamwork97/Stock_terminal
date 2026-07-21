"""Round-trip persistence tests for the watchlist store."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from stock_terminal.storage.watchlist import Watchlist, WatchlistItem


def test_add_save_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    watchlist = Watchlist.load(path)

    watchlist.add(
        WatchlistItem(
            symbol="005930",
            name="삼성전자",
            alert_upper=Decimal("80000"),
            prev_close=Decimal("72000"),
        )
    )
    watchlist.add(WatchlistItem(symbol="AAPL", name="Apple"))

    reloaded = Watchlist.load(path)

    assert set(reloaded.symbols) == {"005930", "AAPL"}
    item = reloaded.items["005930"]
    assert item.name == "삼성전자"
    assert item.alert_upper == Decimal("80000")
    assert item.alert_lower is None
    assert item.prev_close == Decimal("72000")


def test_remove_and_set_alerts(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    watchlist = Watchlist.load(path)
    watchlist.add(WatchlistItem(symbol="005930"))

    watchlist.set_alerts("005930", Decimal("100"), Decimal("50"))
    reloaded = Watchlist.load(path)
    assert reloaded.items["005930"].alert_upper == Decimal("100")
    assert reloaded.items["005930"].alert_lower == Decimal("50")

    watchlist.remove("005930")
    reloaded_after_remove = Watchlist.load(path)
    assert reloaded_after_remove.symbols == []
