"""Validated, atomically saved display preferences (no credentials)."""
from dataclasses import asdict, dataclass
import json
from pathlib import Path

from stock_terminal.storage.watchlist import _atomic_write


class PreferencesError(Exception):
    pass


@dataclass(slots=True)
class Preferences:
    view_mode: str = "both"
    sort_mode: str = "등록순"
    chart_mode: str = "candle"
    chart_interval: str = "1d"

    @classmethod
    def load(cls, path: Path) -> tuple["Preferences", str | None]:
        defaults = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("설정은 객체여야 합니다")
        except FileNotFoundError:
            return defaults, None
        except (OSError, ValueError) as exc:
            return defaults, f"화면 설정을 읽지 못해 기본값으로 시작합니다: {path} ({exc})"
        allowed = {"view_mode": ("both", "chart", "list"),
                   "sort_mode": ("등록순", "심볼순", "등락률순"),
                   "chart_mode": ("candle", "line"), "chart_interval": ("1d", "1m")}
        invalid = []
        for key, choices in allowed.items():
            value = raw.get(key, getattr(defaults, key))
            if value in choices:
                setattr(defaults, key, value)
            else:
                invalid.append(key)
        return defaults, ("화면 설정의 잘못된 값을 기본값으로 대체했습니다: " + ", ".join(invalid)) if invalid else None

    def save(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, json.dumps(asdict(self), ensure_ascii=False, indent=2).encode("utf-8"))
        except OSError as exc:
            raise PreferencesError(f"화면 설정 저장 실패: {path} ({exc})") from exc
