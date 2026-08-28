"""Per-instance login/2FA state machine backed by pytr.

States
------
idle          Initial state; no active session.
authenticator Waiting for a TOTP code from the user.
push          Waiting for the user to confirm login in the TR mobile app.
confirmed     Session is active and ready.
failed        A hard error occurred; a fresh ``start_login()`` is required.
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

import requests
from pytr.api import TradeRepublicApi

from tr_bridge.config import InstanceConfig

logger = logging.getLogger(__name__)


class SessionState(StrEnum):
    idle = "idle"
    authenticator = "authenticator"
    push = "push"
    confirmed = "confirmed"
    failed = "failed"


class LoginInProgressError(Exception):
    """Raised when a login is initiated while one is already in progress."""


class CodeRejectedError(Exception):
    """Raised when a submitted 2FA code is rejected by Trade Republic."""


class RateLimitedError(Exception):
    """Raised when Trade Republic rejects a login with HTTP 429 (rate limit)."""


class TrUpstreamError(Exception):
    """Raised when a Trade Republic request fails with an unexpected upstream error."""


class InvalidStateError(Exception):
    """Raised when an operation is attempted in an incompatible state."""

    def __init__(self, state: SessionState) -> None:
        super().__init__(f"Operation not valid in state {state.value!r}")
        self.state = state


class NoLoginPendingError(InvalidStateError):
    """Raised when a 2FA code is submitted but no login is awaiting a code."""


class InstanceSession:
    """Encapsulates authentication state and pytr API for one TR account."""

    def __init__(
        self,
        config: InstanceConfig,
        session_dir: str,
        tfa_timeout: int,
    ) -> None:
        self._config = config
        self._session_dir = session_dir
        self._tfa_timeout = tfa_timeout
        self._state = SessionState.idle
        self._lock = asyncio.Lock()
        self._api: TradeRepublicApi | None = None
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
        api = self._build_api()
        loop = asyncio.get_running_loop()
        success: bool = await loop.run_in_executor(None, api.resume_websession)
        if success:
            self._api = api
            self._state = SessionState.confirmed
            logger.info("Instance %r: session resumed.", self._config.name)
        else:
            logger.info("Instance %r: no valid session found.", self._config.name)

    async def start_login(self) -> SessionState:
        """Initiate login for this instance.

        1. Tries ``resume_websession()``.  Returns ``confirmed`` on success.
        2. Falls through to ``initiate_weblogin()``.
           - If an authenticator code is required → returns ``authenticator``.
           - Otherwise (push notification sent) → returns ``push`` and starts
             a background task that polls for confirmation.

        Raises:
            LoginInProgressError: if a login is already in progress.
        """
        if self._state in (SessionState.authenticator, SessionState.push):
            raise LoginInProgressError(
                f"Login already in progress for instance {self._config.name!r}"
            )
        async with self._lock:
            # Double-check after acquiring the lock.
            if self._state in (SessionState.authenticator, SessionState.push):
                raise LoginInProgressError(
                    f"Login already in progress for instance {self._config.name!r}"
                )

            api = self._build_api()
            loop = asyncio.get_running_loop()

            try:
                success: bool = await loop.run_in_executor(None, api.resume_websession)
                if success:
                    self._api = api
                    self._state = SessionState.confirmed
                    logger.info(
                        "Instance %r: login via session resume.", self._config.name
                    )
                    return self._state

                await loop.run_in_executor(None, api.initiate_weblogin)
            except requests.exceptions.RequestException as exc:
                self._fail_with_upstream(exc)
            self._api = api

            if api.weblogin_needs_authenticator:
                self._state = SessionState.authenticator
                logger.info(
                    "Instance %r: awaiting authenticator code.", self._config.name
                )
            else:
                self._state = SessionState.push
                logger.info(
                    "Instance %r: awaiting push confirmation.", self._config.name
                )
                self._push_task = asyncio.create_task(self._run_push_confirmation())

            self._schedule_timeout()
            return self._state

    async def submit_2fa(self, code: str) -> None:
        """Submit a TOTP code to complete an authenticator-gated login.

        Transitions state from ``authenticator`` to ``confirmed``.

        Raises:
            NoLoginPendingError: if no login is awaiting an authenticator code.
            CodeRejectedError: if Trade Republic rejects the code.
            RateLimitedError: if Trade Republic rate-limits the request.
            TrUpstreamError: if an unexpected upstream error occurs.
        """
        if self._state != SessionState.authenticator:
            raise NoLoginPendingError(self._state)
        if self._api is None:
            raise NoLoginPendingError(self._state)

        api = self._api
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, api.complete_weblogin, code)
        except ValueError as exc:
            logger.warning("Instance %r: 2FA code rejected: %s", self._config.name, exc)
            raise CodeRejectedError(str(exc)) from exc
        except requests.exceptions.RequestException as exc:
            self._fail_with_upstream(exc)

        # Re-check: a concurrent timeout may have transitioned to failed while
        # complete_weblogin was running in the executor.
        if self._state != SessionState.authenticator:
            logger.warning(
                "Instance %r: state changed during 2FA submit (now %r); ignoring.",
                self._config.name,
                self._state,
            )
            return

        self._state = SessionState.confirmed
        self._cancel_timeout()
        logger.info("Instance %r: confirmed via authenticator code.", self._config.name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fail_with_upstream(
        self, exc: requests.exceptions.RequestException
    ) -> NoReturn:
        """Transition to ``failed`` and re-raise *exc* as a domain error.

        HTTP 429 becomes :class:`RateLimitedError`; anything else becomes
        :class:`TrUpstreamError`.
        """
        self._state = SessionState.failed
        self._cancel_timeout()
        response = getattr(exc, "response", None)
        status_code = response.status_code if response is not None else None
        if status_code == 429:
            logger.warning("Instance %r: login rate-limited.", self._config.name)
            raise RateLimitedError(
                f"Trade Republic rate-limited login for instance {self._config.name!r}."
            ) from exc
        logger.warning("Instance %r: upstream login error: %s", self._config.name, exc)
        raise TrUpstreamError(self._upstream_message(exc, status_code)) from exc

    def _upstream_message(
        self,
        exc: requests.exceptions.RequestException,
        status_code: int | None,
    ) -> str:
        """Build a signal-rich message for an upstream failure.

        Includes the instance name and the upstream HTTP status code when
        available, falling back to the exception type when ``str(exc)`` is empty
        (as happens with ``HTTPError`` raised without arguments).
        """
        detail = str(exc) or type(exc).__name__
        if status_code is not None:
            return (
                f"Trade Republic upstream error for instance "
                f"{self._config.name!r} (HTTP {status_code}): {detail}"
            )
        return (
            f"Trade Republic upstream error for instance "
            f"{self._config.name!r}: {detail}"
        )

    def _build_api(self) -> TradeRepublicApi:
        session_path = Path(self._session_dir)
        session_path.mkdir(parents=True, exist_ok=True)
        return TradeRepublicApi(
            phone_no=self._config.phone,
            pin=self._config.pin,
            save_cookies=True,
            credentials_file=str(session_path / "credentials.json"),
            cookies_file=str(session_path / "cookies.txt"),
            use_v2_login=True,
        )

    def _schedule_timeout(self) -> None:
        self._cancel_timeout()
        self._timeout_task = asyncio.create_task(self._run_timeout())

    async def _run_timeout(self) -> None:
        await asyncio.sleep(self._tfa_timeout)
        if self._state in (SessionState.authenticator, SessionState.push):
            logger.warning(
                "Instance %r: 2FA timeout — transitioning to failed.",
                self._config.name,
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
        # NOTE: cancelling this asyncio task does NOT stop the underlying
        # run_in_executor thread — complete_weblogin() will run to completion
        # (or raise) regardless. This is an inherent limitation of
        # run_in_executor with blocking callables.
        if self._api is None:
            logger.error(
                "Instance %r: _run_push_confirmation called with no API; aborting.",
                self._config.name,
            )
            self._state = SessionState.failed
            self._cancel_timeout()
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._api.complete_weblogin)
            if self._state == SessionState.push:
                self._state = SessionState.confirmed
                self._cancel_timeout()
                logger.info(
                    "Instance %r: confirmed via push notification.",
                    self._config.name,
                )
        except asyncio.CancelledError:
            # Expected when the timeout cancels the push task — not an error.
            logger.debug(
                "Instance %r: push confirmation task cancelled (timeout).",
                self._config.name,
            )
            raise
        except Exception:
            logger.exception(
                "Instance %r: push confirmation failed.", self._config.name
            )
            if self._state == SessionState.push:
                self._state = SessionState.failed
                self._cancel_timeout()
        finally:
            self._push_task = None
