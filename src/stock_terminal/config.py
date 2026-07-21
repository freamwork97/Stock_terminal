"""Application settings loaded from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DATA_DIR = Path.home() / ".stock_terminal"
WATCHLIST_PATH = DATA_DIR / "watchlist.json"

TOSS_API_BASE_URL = "https://openapi.tossinvest.com"


class ConfigError(Exception):
    """Raised when required settings are missing."""


@dataclass(frozen=True, slots=True)
class Settings:
    client_id: str
    client_secret: str
    poll_interval_seconds: float
    chart_refresh_seconds: float

    @classmethod
    def load(cls) -> Settings:
        load_dotenv()

        client_id = os.environ.get("TOSS_CLIENT_ID", "").strip()
        client_secret = os.environ.get("TOSS_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise ConfigError(
                "TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 이(가) 설정되지 않았습니다.\n"
                "프로젝트 루트에 .env 파일을 만들고 아래 형식으로 채워주세요:\n\n"
                "  TOSS_CLIENT_ID=발급받은_client_id\n"
                "  TOSS_CLIENT_SECRET=발급받은_client_secret\n\n"
                "발급 방법: 토스증권 앱/WTS 로그인 > 설정 > Open API\n"
                "(허용 IP도 같은 화면에서 등록해야 API 호출이 허용됩니다.)"
            )

        poll_interval = float(os.environ.get("POLL_INTERVAL_SECONDS", "3"))
        chart_refresh = float(os.environ.get("CHART_REFRESH_SECONDS", "30"))

        return cls(
            client_id=client_id,
            client_secret=client_secret,
            poll_interval_seconds=poll_interval,
            chart_refresh_seconds=chart_refresh,
        )


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
