"""Secondary port: the interface the use case depends on to reach Trade Republic.

The use case (``application/session.py``) owns the login/2FA state machine and
orchestration (locks, timeouts, push polling). Everything that actually talks to
Trade Republic — building the pytr client, bridging blocking calls onto a thread,
translating ``requests`` errors into domain errors — lives behind this port and
is provided by a concrete adapter (``adapters/pytr/pytr_client.py``).

Depending on this ``Protocol`` (not on pytr) is what restores the Dependency
Inversion Principle: the use case is testable with an in-memory fake and never
imports pytr.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from tr_bridge.domain.state import LoginChallenge


class TradeRepublicClient(Protocol):
    """Async interface to a single Trade Republic account.

    Implementations must translate transport-level failures into the domain
    errors declared in :mod:`tr_bridge.domain.state` (``RateLimitedError``,
    ``TrUpstreamError``, ``CodeRejectedError``, ``SessionExpiredError``).
    """

    async def resume_session(self) -> bool:
        """Attempt to resume a persisted web session. Returns ``True`` on success."""
        ...

    async def start_login(self) -> LoginChallenge:
        """Initiate a fresh weblogin and report which second factor is required."""
        ...

    async def complete_login(self, code: str | None = None) -> None:
        """Complete a pending login.

        ``code`` carries the TOTP for an authenticator challenge; it is ``None``
        when awaiting a push confirmation.
        """
        ...

    async def fetch_timeline(self, since: datetime, until: datetime) -> list[dict]:
        """Return raw pytr timeline events in the ``[since, until)`` window."""
        ...
