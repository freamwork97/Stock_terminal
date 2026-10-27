"""Round-trip persistence tests for the watchlist store."""

from __future__ import annotations

import json
import os

import pytest

from decimal import Decimal
from pathlib import Path

from stock_terminal.storage.watchlist import Watchlist, WatchlistItem, WatchlistError


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


def test_reference_metadata_and_failed_update(tmp_path):
    watchlist = Watchlist(tmp_path / "watchlist.json")
    watchlist.add(WatchlistItem("AAPL"))
    watchlist.set_prev_close("AAPL", Decimal("100"), "2026-10-25",
        session_date="2026-10-23", source="daily_fallback", confirmed=False)
    for invalid in (None, Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")):
        watchlist.set_prev_close("AAPL", invalid, "2026-10-26")
    item = Watchlist.load(watchlist.path).items["AAPL"]
    assert item.prev_close == Decimal("100")
    assert item.prev_close_date == "2026-10-25"
    assert item.prev_close_session == "2026-10-23"
    assert item.prev_close_source == "daily_fallback"
    assert item.prev_close_confirmed is False


def test_legacy_reference_is_unknown():
    item = WatchlistItem.from_dict({"symbol": "AAPL", "prev_close": "100"})
    assert item.prev_close_source == "unknown"
    assert item.prev_close_session is None
    assert item.prev_close_confirmed is False


def test_backup_contains_previous_successful_version(tmp_path):
    path = tmp_path / "watchlist.json"
    watchlist = Watchlist(path)
    watchlist.add(WatchlistItem("AAPL"))
    assert Watchlist._decode(watchlist.backup_path.read_bytes()).keys() == {"AAPL"}
    watchlist.add(WatchlistItem("MSFT"))
    assert Watchlist.load(path).symbols == ["AAPL", "MSFT"]
    assert list(Watchlist._decode(watchlist.backup_path.read_bytes())) == ["AAPL"]


@pytest.mark.parametrize("broken", [b"{broken", b"", b"\xff\xfe", b"{}", b'[{}]', b'[{"symbol":"AAPL","alert_upper":"NaN"}]'])
def test_corrupt_primary_restores_backup_and_preserves_original(tmp_path, broken):
    path = tmp_path / "watchlist.json"
    watchlist = Watchlist(path)
    watchlist.add(WatchlistItem("AAPL"))
    backup = watchlist.backup_path.read_bytes()
    path.write_bytes(broken)
    recovered = Watchlist.load(path)
    assert recovered.symbols == ["AAPL"]
    assert recovered.recovery_notice is not None
    assert "백업 이후" in recovered.recovery_notice
    assert path.read_bytes() == backup
    assert recovered.backup_path.read_bytes() == backup
    preserved = list(tmp_path.glob("*.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == broken
    assert Watchlist.load(path).recovery_notice is None


def test_missing_primary_restores_backup(tmp_path):
    path = tmp_path / "watchlist.json"
    watchlist = Watchlist(path)
    watchlist.add(WatchlistItem("AAPL"))
    path.unlink()
    assert Watchlist.load(path).symbols == ["AAPL"]
    assert path.exists()


@pytest.mark.parametrize("with_backup", [False, True])
def test_unrecoverable_files_are_never_replaced_with_empty_list(tmp_path, with_backup):
    path = tmp_path / "watchlist.json"
    path.write_bytes(b"broken")
    if with_backup:
        path.with_suffix(".json.bak").write_bytes(b"also broken")
    with pytest.raises(WatchlistError, match="복구할 수 없습니다"):
        Watchlist.load(path)
    assert path.read_bytes() == b"broken"
    if with_backup:
        assert path.with_suffix(".json.bak").read_bytes() == b"also broken"


@pytest.mark.parametrize("failure", ["backup_replace", "primary_replace", "fsync"])
@pytest.mark.parametrize("operation", ["add", "remove", "alerts", "currency", "baseline"])
def test_failed_write_is_atomic_and_memory_is_rolled_back(tmp_path, monkeypatch, failure, operation):
    path = tmp_path / "watchlist.json"
    watchlist = Watchlist(path)
    item = WatchlistItem("AAPL")
    watchlist.add(item)
    original = path.read_bytes()
    saved_item = item.to_dict()
    real_replace = os.replace

    def fail_replace(source, destination):
        if (failure == "backup_replace" and Path(destination) == watchlist.backup_path
                or failure == "primary_replace" and Path(destination) == path):
            raise PermissionError("simulated failure")
        return real_replace(source, destination)

    monkeypatch.setattr("stock_terminal.storage.watchlist.os.replace", fail_replace)
    if failure == "fsync":
        def fail_fsync(fd):
            raise OSError("disk full")
        monkeypatch.setattr("stock_terminal.storage.watchlist.os.fsync", fail_fsync)
    actions = {
        "add": lambda: watchlist.add(WatchlistItem("MSFT")),
        "remove": lambda: watchlist.remove("AAPL"),
        "alerts": lambda: watchlist.set_alerts("AAPL", Decimal("200"), None),
        "currency": lambda: watchlist.set_currency("AAPL", "USD"),
        "baseline": lambda: watchlist.set_prev_close("AAPL", Decimal("100"), "2026-10-26"),
    }
    with pytest.raises(WatchlistError, match="저장 실패"):
        actions[operation]()
    assert path.read_bytes() == original
    assert watchlist.items["AAPL"] is item
    assert item.to_dict() == saved_item
    assert watchlist.symbols == ["AAPL"]
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_primary_cannot_poison_good_backup_on_save(tmp_path):
    path = tmp_path / "watchlist.json"
    watchlist = Watchlist(path)
    watchlist.add(WatchlistItem("AAPL"))
    backup = watchlist.backup_path.read_bytes()
    path.write_bytes(b"corrupt externally")
    with pytest.raises(WatchlistError):
        watchlist.add(WatchlistItem("MSFT"))
    assert watchlist.backup_path.read_bytes() == backup
    assert path.read_bytes() == b"corrupt externally"
    assert watchlist.symbols == ["AAPL"]


def test_recovery_replace_failure_keeps_both_sources(tmp_path, monkeypatch):
    path = tmp_path / "watchlist.json"
    watchlist = Watchlist(path)
    watchlist.add(WatchlistItem("AAPL"))
    backup = watchlist.backup_path.read_bytes()
    path.write_bytes(b"broken")
    real_replace = os.replace
    def fail_primary(source, destination):
        if Path(destination) == path:
            raise PermissionError("locked")
        return real_replace(source, destination)
    monkeypatch.setattr("stock_terminal.storage.watchlist.os.replace", fail_primary)
    with pytest.raises(WatchlistError):
        Watchlist.load(path)
    assert path.read_bytes() == b"broken"
    assert watchlist.backup_path.read_bytes() == backup
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("entry", [None, {}, {"symbol": ""}, {"symbol": 1},
    {"symbol": "AAPL", "is_index": "false"}, {"symbol": "AAPL", "name": []},
    {"symbol": "AAPL", "prev_close_date": "bad"},
    {"symbol": "AAPL", "alert_upper": "Infinity"},
    {"symbol": "AAPL", "alert_upper": "1", "alert_lower": "2"},
    {"symbol": "AAPL", "prev_close": True}])
def test_invalid_saved_schema_is_reported_as_storage_error(tmp_path, entry):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps([entry]), encoding="utf-8")
    with pytest.raises(WatchlistError):
        Watchlist.load(path)


def test_primary_read_permission_error_does_not_trigger_recovery(tmp_path, monkeypatch):
    path = tmp_path / "watchlist.json"
    watchlist = Watchlist(path)
    watchlist.add(WatchlistItem("AAPL"))
    real_read = Path.read_bytes
    def fail_read(target):
        if target == path:
            raise PermissionError("locked")
        return real_read(target)
    monkeypatch.setattr(Path, "read_bytes", fail_read)
    with pytest.raises(WatchlistError, match="접근/복구 실패"):
        Watchlist.load(path)
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_failed_first_save_does_not_restore_uncommitted_item(tmp_path, monkeypatch):
    path = tmp_path / "watchlist.json"
    watchlist = Watchlist(path)
    real_replace = os.replace
    def fail_primary(source, destination):
        if Path(destination) == path:
            raise OSError("disk failure")
        return real_replace(source, destination)
    monkeypatch.setattr("stock_terminal.storage.watchlist.os.replace", fail_primary)
    with pytest.raises(WatchlistError):
        watchlist.add(WatchlistItem("AAPL"))
    assert not path.exists()
    assert not watchlist.backup_path.exists()
    assert Watchlist.load(path).symbols == []
    assert not list(tmp_path.glob("*.tmp"))


def test_save_with_missing_primary_preserves_existing_backup(tmp_path):
    path = tmp_path / "watchlist.json"
    watchlist = Watchlist(path)
    watchlist.add(WatchlistItem("AAPL"))
    backup = watchlist.backup_path.read_bytes()
    path.unlink()
    watchlist.add(WatchlistItem("MSFT"))
    assert watchlist.backup_path.read_bytes() == backup
    assert Watchlist.load(path).symbols == ["AAPL", "MSFT"]


def test_duplicate_symbols_are_rejected_without_silent_data_loss(tmp_path):
    path = tmp_path / "watchlist.json"
    content = b'[{"symbol":"AAPL"}, {"symbol":"AAPL", "name":"duplicate"}]'
    path.write_bytes(content)
    with pytest.raises(WatchlistError):
        Watchlist.load(path)
    assert path.read_bytes() == content
