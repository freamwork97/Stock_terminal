"""Exceptions raised by the Toss Invest API client."""

from __future__ import annotations


class TossAPIError(Exception):
    """Base error for any failure talking to the Toss Invest Open API."""


class TossAuthError(TossAPIError):
    """Raised when OAuth2 token issuance fails (bad client_id/client_secret)."""


class TossForbiddenError(TossAPIError):
    """Raised on 403 - most commonly an un-whitelisted caller IP."""

    def __init__(self) -> None:
        super().__init__(
            "403 Forbidden: 토스증권 WTS > 설정 > Open API > 허용 IP 관리에서 "
            "현재 접속 IP가 등록되어 있는지 확인하세요."
        )


class TossRateLimitError(TossAPIError):
    """Raised on 429 after the client's own backoff/retry has been exhausted."""

    def __init__(self, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(f"429 Too Many Requests (retry_after={retry_after})")
