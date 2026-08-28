"""Domain model for the login/2FA session: states, challenges, and errors.

This module is the pure core of the hexagonal design. It has no dependency on
FastAPI, pytr, or asyncio — only on the RFC 9457 :class:`DomainError` base used
for HTTP error mapping. Both the use case (``application/session.py``) and the
secondary adapter (``adapters/pytr/pytr_client.py``) import their vocabulary
from here.

States
------
idle          Initial state; no active session.
authenticator Waiting for a TOTP code from the user.
push          Waiting for the user to confirm login in the TR mobile app.
confirmed     Session is active and ready.
failed        A hard error occurred; a fresh ``start_login()`` is required.
"""

from __future__ import annotations

from enum import StrEnum

from tr_bridge.errors import DomainError


class SessionState(StrEnum):
    idle = "idle"
    authenticator = "authenticator"
    push = "push"
    confirmed = "confirmed"
    failed = "failed"


class LoginChallenge(StrEnum):
    """The kind of second factor a fresh weblogin requires."""

    authenticator = "authenticator"
    push = "push"


class LoginInProgressError(DomainError):
    """Raised when a login is initiated while one is already in progress."""

    status = 409
    code = "login_in_progress"
    title = "Login already in progress"


class CodeRejectedError(DomainError):
    """Raised when a submitted 2FA code is rejected by Trade Republic."""

    status = 401
    code = "code_rejected"
    title = "2FA code rejected"


class RateLimitedError(DomainError):
    """Raised when Trade Republic rejects a login with HTTP 429 (rate limit)."""

    status = 429
    code = "rate_limited"
    title = "Rate limited"


class TrUpstreamError(DomainError):
    """Raised when a Trade Republic request fails with an unexpected upstream error."""

    status = 502
    code = "tr_upstream_error"
    title = "Trade Republic upstream error"


class SessionExpiredError(DomainError):
    """Raised when a timeline is requested but no valid session is available."""

    status = 401
    code = "session_expired"
    title = "Session expired"


class InvalidStateError(DomainError):
    """Raised when an operation is attempted in an incompatible state."""

    status = 409
    code = "invalid_state"
    title = "Operation not valid in current state"

    def __init__(self, state: SessionState) -> None:
        super().__init__(f"Operation not valid in state {state.value!r}")
        self.state = state


class NoLoginPendingError(InvalidStateError):
    """Raised when a 2FA code is submitted but no login is awaiting a code."""

    code = "no_login_pending"
    title = "No login pending"

    @property
    def detail(self) -> str:
        return "No login is awaiting a 2FA code; start a login first."
