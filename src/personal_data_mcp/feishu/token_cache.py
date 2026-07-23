"""Cache the tenant access token, refreshing before it expires.

The token endpoint returns a token and its lifetime in seconds. Minting one per
request would be needless load and a needless secret round trip, so it is cached
and reused until a safety margin before expiry. The margin means a token is
never presented to Feishu in the last moments of its life, where clock skew
could get it rejected.

The clock is injected for testing. The fetch is injected too, so the cache does
not know how a token is obtained -- that is the adapter's allowlisted call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final


#: Refresh this many seconds before the provider's stated expiry.
DEFAULT_MARGIN_SECONDS: Final[float] = 300.0

TokenFetch = Callable[[], Awaitable[tuple[str, int]]]


@dataclass
class _CachedToken:
    value: str
    expires_at: float


class TenantTokenCache:
    def __init__(
        self,
        fetch: TokenFetch,
        *,
        now: Callable[[], float],
        margin_seconds: float = DEFAULT_MARGIN_SECONDS,
    ) -> None:
        self._fetch = fetch
        self._now = now
        self._margin = margin_seconds
        self._cached: _CachedToken | None = None

    def _is_fresh(self) -> bool:
        return (
            self._cached is not None
            and self._now() < self._cached.expires_at - self._margin
        )

    async def get(self) -> str:
        """A currently-valid tenant token, minting a new one only if needed."""
        if self._is_fresh():
            assert self._cached is not None
            return self._cached.value
        token, expires_in = await self._fetch()
        self._cached = _CachedToken(
            value=token, expires_at=self._now() + float(expires_in)
        )
        return token

    def invalidate(self) -> None:
        """Drop the cached token, e.g. after a provider says it is invalid."""
        self._cached = None
