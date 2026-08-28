"""Login/2FA use case: the per-instance state machine.

This is the application core. It owns the asyncio-centric orchestration —
serialising logins with a lock, the 2FA timeout, and the background push-polling
task — and drives the transitions between :class:`SessionState` values. It talks
to Trade Republic exclusively through the :class:`TradeRepublicClient` port, so
it has **no** import of pytr and can be exercised with an in-memory fake.

Transitions
-----------
idle → confirmed                     (session resumed)
idle → authenticator | push          (fresh weblogin)
authenticator → confirmed            (valid TOTP submitted)
push → confirmed                     (user approves in the app)
authenticator | push → failed        (upstream error or 2FA timeout)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from tr_bridge.application.ports import TradeRepublicClient
from tr_bridge.domain.state import (
    LoginChallenge,
    LoginInProgressError,
    NoLoginPendingError,
    RateLimitedError,
    SessionExpiredError,
    SessionState,
    TrUpstreamError,
)

logger = logging.getLogger(__name__)

# Upstream failures that must drive the state machine into ``failed`` and be
# re-raised to the caller. ``CodeRejectedError`` is deliberately excluded: a
# wrong 2FA code leaves the login pending so the user can retry.
_UPSTREAM_ERRORS = (RateLimitedError, TrUpstreamError)

_PENDING_STATES = (SessionState.authenticator, SessionState.push)


class InstanceSession:
    """Login/2FA state machine for one TR account, driven through a port."""

    def __init__(
        self,
        name: str,
        client: TradeRepublicClient,
        tfa_timeout: int,
    ) -> None:
        self._name = name
        self._client = client
        self._tfa_timeout = tfa_timeout
        self._state = SessionState.idle
        self._lock = asyncio.Lock()
        self._timeout_task: asyncio.Task[None] | None = None
        self._push_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def state(self) -> SessionState:
        return self._state

    async def resume(self) -> None:
        """Attempt to resume an existing web session from disk.

        Sets state to ``confirmed`` on success; leaves it as ``idle`` otherwise.
        Intended to be called once at startup.
        """
        if await self._client.resume_session():
            self._state = SessionState.confirmed
            logger.info("Instance %r: session resumed.", self._name)
        else:
            logger.info("Instance %r: no valid session found.", self._name)

    async def start_login(self) -> SessionState:
        """Initiate login for this instance.

        1. Tries to resume an existing session. Returns ``confirmed`` on success.
        2. Falls through to a fresh weblogin.
           - If an authenticator code is required → returns ``authenticator``.
           - Otherwise (push notification sent) → returns ``push`` and starts a
             background task that polls for confirmation.

        Raises:
            LoginInProgressError: if a login is already in progress.
            RateLimitedError / TrUpstreamError: on upstream failure.
        """
        if self._state in _PENDING_STATES:
            raise LoginInProgressError(
                f"Login already in progress for instance {self._name!r}"
            )
        async with self._lock:
            # Double-check after acquiring the lock.
            if self._state in _PENDING_STATES:
                raise LoginInProgressError(
                    f"Login already in progress for instance {self._name!r}"
                )

            try:
                if await self._client.resume_session():
                    self._state = SessionState.confirmed
                    logger.info("Instance %r: login via session resume.", self._name)
                    return self._state
                challenge = await self._client.start_login()
            except _UPSTREAM_ERRORS:
                self._fail()
                raise

            if challenge is LoginChallenge.authenticator:
                self._state = SessionState.authenticator
                logger.info("Instance %r: awaiting authenticator code.", self._name)
            else:
                self._state = SessionState.push
                logger.info("Instance %r: awaiting push confirmation.", self._name)
                self._push_task = asyncio.create_task(self._run_push_confirmation())

            self._schedule_timeout()
            return self._state

    async def submit_2fa(self, code: str) -> None:
        """Submit a TOTP code to complete an authenticator-gated login.

        Transitions state from ``authenticator`` to ``confirmed``.

        Raises:
            NoLoginPendingError: if no login is awaiting an authenticator code.
            CodeRejectedError: if Trade Republic rejects the code.
            RateLimitedError / TrUpstreamError: on upstream failure.
        """
        if self._state != SessionState.authenticator:
            raise NoLoginPendingError(self._state)
        try:
            await self._client.complete_login(code)
        except _UPSTREAM_ERRORS:
            self._fail()
            raise

        # Re-check: a concurrent timeout may have transitioned to failed while
        # complete_login was running.
        if self._state != SessionState.authenticator:
            logger.warning(
                "Instance %r: state changed during 2FA submit (now %r); ignoring.",
                self._name,
                self._state,
            )
            return

        self._state = SessionState.confirmed
        self._cancel_timeout()
        logger.info("Instance %r: confirmed via authenticator code.", self._name)

    async def fetch_timeline(self, since: datetime, until: datetime) -> list[dict]:
        """Fetch raw pytr timeline events in the ``[since, until)`` window.

        Raises:
            SessionExpiredError: if there is no confirmed session to query (or
                the port reports an expired session).
            TrUpstreamError: if the timeline request fails otherwise.
        """
        if self._state != SessionState.confirmed:
            raise SessionExpiredError(
                f"No valid session for instance {self._name!r}; login required."
            )
        return await self._client.fetch_timeline(since, until)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fail(self) -> None:
        """Transition to ``failed`` and cancel any pending 2FA timeout."""
        self._state = SessionState.failed
        self._cancel_timeout()

    def _schedule_timeout(self) -> None:
        self._cancel_timeout()
        self._timeout_task = asyncio.create_task(self._run_timeout())

    async def _run_timeout(self) -> None:
        await asyncio.sleep(self._tfa_timeout)
        if self._state in _PENDING_STATES:
            logger.warning(
                "Instance %r: 2FA timeout — transitioning to failed.", self._name
            )
            self._state = SessionState.failed
            self._cancel_push_task()
        self._timeout_task = None

    def _cancel_timeout(self) -> None:
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
        self._timeout_task = None

    def _cancel_push_task(self) -> None:
        if self._push_task and not self._push_task.done():
            self._push_task.cancel()
        self._push_task = None

    async def _run_push_confirmation(self) -> None:
        # NOTE: cancelling this task does NOT stop the underlying blocking call
        # inside the adapter's executor thread — it will run to completion (or
        # raise) regardless. This is an inherent limitation of bridging blocking
        # calls onto a thread pool.
        try:
            await self._client.complete_login()
            if self._state == SessionState.push:
                self._state = SessionState.confirmed
                self._cancel_timeout()
                logger.info("Instance %r: confirmed via push notification.", self._name)
        except asyncio.CancelledError:
            # Expected when the timeout cancels the push task — not an error.
            logger.debug(
                "Instance %r: push confirmation task cancelled (timeout).", self._name
            )
            raise
        except Exception:
            logger.exception("Instance %r: push confirmation failed.", self._name)
            if self._state == SessionState.push:
                self._state = SessionState.failed
                self._cancel_timeout()
        finally:
            self._push_task = None
