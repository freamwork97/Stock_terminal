"""Persisted watchlist: symbols the user is tracking, plus alert thresholds."""

from __future__ import annotations

import json
from contextlib import contextmanager
import os
import tempfile
from datetime import date
from functools import wraps
from uuid import uuid4
from dataclasses import asdict, dataclass, field, fields, replace
from decimal import Decimal, DecimalException
from pathlib import Path


class WatchlistError(Exception):
    """Storage failure safe to display without losing the existing watchlist."""


@contextmanager
def _file_lock(path: Path):
    """Serialize cooperating processes; OS releases locks if a process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WatchlistError("다른 프로세스가 관심목록 파일을 사용 중입니다. 잠시 후 다시 시도하세요.") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, content: bytes) -> None:
    """Replace only after a same-directory temporary file is fully flushed."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _transaction(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        original = dict(self.items)
        snapshots = {symbol: replace(item) for symbol, item in original.items()}
        try:
            return method(self, *args, **kwargs)
        except WatchlistError:
            for symbol, item in original.items():
                for attr in fields(item):
                    setattr(item, attr.name, getattr(snapshots[symbol], attr.name))
            self.items = original
            raise
    return wrapped


@dataclass(slots=True)
class WatchlistItem:
    symbol: str
    name: str = ""
    alert_upper: Decimal | None = None
    alert_lower: Decimal | None = None
    alerts_enabled: bool = True
    prev_close: Decimal | None = None
    prev_close_date: str | None = None  # ISO date the prev_close was fetched for
    currency: str = ""  # market's trading-day calendar depends on this
    is_index: bool = False  # routes polling to /api/v1/market-indicators/* instead

    prev_close_session: str | None = None
    prev_close_source: str = "unknown"
    prev_close_confirmed: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("alert_upper", "alert_lower", "prev_close"):
            if d[key] is not None:
                d[key] = str(d[key])
        return d

    @classmethod
    def from_dict(cls, data: dict) -> WatchlistItem:
        if not isinstance(data, dict) or not isinstance(data.get("symbol"), str) or not data["symbol"].strip():
            raise ValueError("종목 코드 형식 오류")
        for key in ("name", "currency", "prev_close_source"):
            if key in data and not isinstance(data[key], str):
                raise ValueError(f"{key} 형식 오류")
        for key in ("is_index", "prev_close_confirmed", "alerts_enabled"):
            if key in data and not isinstance(data[key], bool):
                raise ValueError(f"{key} 형식 오류")
        for key in ("prev_close_date", "prev_close_session"):
            if data.get(key) is not None:
                if not isinstance(data[key], str):
                    raise ValueError(f"{key} 형식 오류")
                date.fromisoformat(data[key])

        def dec(key: str) -> Decimal | None:
            value = data.get(key)
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError(f"{key} 형식 오류")
            result = Decimal(str(value))
            if not result.is_finite() or result <= 0:
                raise ValueError(f"{key} 가격 오류")
            return result

        upper, lower = dec("alert_upper"), dec("alert_lower")
        if upper is not None and lower is not None and upper <= lower:
            raise ValueError("알림 상한은 하한보다 커야 합니다")

        return cls(
            symbol=data["symbol"],
            name=data.get("name", ""),
            alert_upper=dec("alert_upper"),
            alert_lower=dec("alert_lower"),
            alerts_enabled=data.get("alerts_enabled", True),
            prev_close=dec("prev_close"),
            prev_close_date=data.get("prev_close_date"),
            prev_close_session=data.get("prev_close_session"),
            prev_close_source=data.get("prev_close_source", "unknown"),
            prev_close_confirmed=data.get("prev_close_confirmed", False),
            currency=data.get("currency", ""),
            is_index=data.get("is_index", False),
        )


@dataclass(slots=True)
class Watchlist:
    path: Path
    items: dict[str, WatchlistItem] = field(default_factory=dict)

    recovery_notice: str | None = None
    _disk_snapshot: bytes | None = field(default=None, repr=False)

    @property
    def backup_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".bak")

    @staticmethod
    def _decode(content: bytes) -> dict[str, WatchlistItem]:
        raw = json.loads(content.decode("utf-8"))
        if not isinstance(raw, list):
            raise ValueError("관심목록은 배열이어야 합니다")
        items = {}
        for entry in raw:
            item = WatchlistItem.from_dict(entry)
            if item.symbol in items:
                raise ValueError("중복 종목 코드")
            items[item.symbol] = item
        return items

    @classmethod
    def load(cls, path: Path) -> Watchlist:
        try:
            with _file_lock(path):
                return cls._load_locked(path)
        except OSError as exc:
            raise WatchlistError(f"관심목록 파일 접근/복구 실패: {path} ({exc})") from exc

    @classmethod
    def _load_locked(cls, path: Path) -> Watchlist:
        watchlist = cls(path=path)
        original = None
        try:
            try:
                original = path.read_bytes()
            except FileNotFoundError:
                if not watchlist.backup_path.exists():
                    return watchlist
            if original is not None:
                try:
                    watchlist.items = cls._decode(original)
                    watchlist._disk_snapshot = original
                    return watchlist
                except (ValueError, TypeError, KeyError, DecimalException):
                    pass
            try:
                backup = watchlist.backup_path.read_bytes()
                recovered = cls._decode(backup)
            except (FileNotFoundError, ValueError, TypeError, KeyError, DecimalException) as exc:
                raise WatchlistError(
                    f"관심목록을 복구할 수 없습니다. 원본과 백업을 보존했습니다. "
                    f"파일을 확인하세요: {path} / {watchlist.backup_path}"
                ) from exc
            preserved = None
            if original is not None:
                preserved = path.with_name(path.name + f".corrupt-{uuid4().hex}")
                _atomic_write(preserved, original)
            _atomic_write(path, backup)
            watchlist.items = recovered
            watchlist._disk_snapshot = backup
            watchlist.recovery_notice = (
                f"관심목록을 백업에서 복구했습니다: {watchlist.backup_path}. "
                "백업 이후 변경사항은 포함되지 않을 수 있습니다."
                + (f" 손상 원본 보존: {preserved}" if preserved else "")
            )
            return watchlist
        except OSError as exc:
            raise WatchlistError(f"관심목록 파일 접근/복구 실패: {path} ({exc})") from exc

    def save(self) -> None:
        try:
            with _file_lock(self.path):
                self._save_locked()
        except OSError as exc:
            raise WatchlistError(f"관심목록 저장 실패: {self.path} ({exc})") from exc

    def _save_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps([item.to_dict() for item in self.items.values()],
                                 ensure_ascii=False, indent=2).encode("utf-8")
            self._decode(payload)
            primary_missing = False
            backup_existed = self.backup_path.exists()
            try:
                previous = self.path.read_bytes()
            except FileNotFoundError:
                primary_missing = True
                previous = self.backup_path.read_bytes() if backup_existed else payload
            # Never overwrite the last good backup with a corrupt primary.
            self._decode(previous)
            observed = None if primary_missing and not backup_existed else previous
            if observed != self._disk_snapshot:
                raise WatchlistError("다른 실행에서 관심목록이 변경되어 저장을 중단했습니다. 프로그램을 다시 실행해 최신 목록을 불러오세요.")
            _atomic_write(self.backup_path, previous)
            try:
                _atomic_write(self.path, payload)
            except OSError:
                if primary_missing and not backup_existed:
                    # A failed first save must not resurrect an uncommitted edit.
                    self.backup_path.unlink(missing_ok=True)
                raise
            self._disk_snapshot = payload
        except (OSError, ValueError, TypeError, KeyError, DecimalException) as exc:
            raise WatchlistError(f"관심목록 저장 실패. 기존 파일을 유지합니다: {self.path} ({exc})") from exc

    @property
    def symbols(self) -> list[str]:
        return list(self.items.keys())

    @_transaction
    def add(self, item: WatchlistItem) -> None:
        self.items[item.symbol] = item
        self.save()

    @_transaction
    def remove(self, symbol: str) -> None:
        self.items.pop(symbol, None)
        self.save()

    @_transaction
    def set_alerts(
        self, symbol: str, upper: Decimal | None, lower: Decimal | None
    ) -> None:
        item = self.items.get(symbol)
        if item is None:
            return
        item.alert_upper = upper
        item.alert_lower = lower
        self.save()

    @_transaction
    def set_alerts_enabled(self, symbol: str, enabled: bool) -> None:
        item = self.items.get(symbol)
        if item is None:
            return
        item.alerts_enabled = enabled
        self.save()

    @_transaction
    def set_currency(self, symbol: str, currency: str) -> None:
        item = self.items.get(symbol)
        if item is None:
            return
        item.currency = currency
        self.save()

    @_transaction
    def set_prev_close(
        self, symbol: str, prev_close: Decimal | None, prev_close_date: str | None,
        *, session_date: str | None = None, source: str = "unknown", confirmed: bool = False
    ) -> None:
        item = self.items.get(symbol)
        if item is None:
            return
        if prev_close is None or not prev_close.is_finite() or prev_close <= 0:
            return
        item.prev_close_session = session_date
        item.prev_close_source = source
        item.prev_close_confirmed = confirmed
        item.prev_close = prev_close
        item.prev_close_date = prev_close_date
        self.save()
