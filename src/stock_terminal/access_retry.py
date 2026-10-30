"""Shared retry cooldown for persistent authentication/access failures."""
from time import monotonic

from stock_terminal.api.exceptions import TossAPIError, TossAuthError, TossForbiddenError


class AccessRetry:
    def __init__(self):
        self.failures = 0
        self.retry_at = 0.0
        self.error: TossAPIError | None = None
        self.version = 0

    @property
    def wait(self) -> float:
        return max(0.0, self.retry_at - monotonic())

    def check(self) -> None:
        if self.error is not None and self.wait > 0:
            # Re-raise a typed, safe failure without issuing another HTTP request.
            if isinstance(self.error, TossForbiddenError):
                raise TossForbiddenError() from self.error
            raise TossAuthError("인증 재시도 대기 중입니다. 설정을 확인하세요.") from self.error

    def fail(self, error: TossAPIError, *, version: int | None = None) -> None:
        if version is not None and version != self.version:
            return
        if self.wait > 0:
            return  # Concurrent failures in one request wave share a cooldown.
        self.failures = min(self.failures + 1, 5)
        self.error = error
        self.retry_at = monotonic() + min(60 * 2 ** (self.failures - 1), 900)
        self.version += 1

    def success(self, version: int) -> None:
        if version == self.version:
            self.failures = 0
            self.retry_at = 0
            self.error = None

    def retry_now(self) -> None:
        self.retry_at = 0
        self.version += 1  # An old in-flight success must not reset this retry.

    @property
    def guidance(self) -> str:
        if self.error is None:
            return ""
        if isinstance(self.error, TossForbiddenError):
            return "403 접근 거부: 토스증권 Open API의 허용 IP를 확인하세요."
        return "인증 실패: .env의 TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 확인 후 재시작하세요."
