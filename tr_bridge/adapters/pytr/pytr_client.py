"""Secondary adapter: implements :class:`TradeRepublicClient` using pytr.

This is the *only* module that imports pytr. It is deliberately thin and holds
no business state: it builds and caches the ``TradeRepublicApi`` handle, bridges
pytr's blocking calls onto a thread via ``run_in_executor``, and translates
transport-level failures (``requests`` exceptions, pytr's ``ValueError`` on a
bad code, HTTP 401 on an expired timeline) into the domain errors declared in
:mod:`tr_bridge.domain.state`.

All orchestration (locks, timeouts, push polling, state transitions) lives in
the use case and must never leak in here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NoReturn, TypeVar

import requests
from pytr.api import TradeRepublicApi
from pytr.timeline import Timeline

from tr_bridge.config import InstanceConfig
from tr_bridge.domain.state import (
    CodeRejectedError,
    LoginChallenge,
    RateLimitedError,
    SessionExpiredError,
    TrUpstreamError,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class PytrClient:
    """Concrete :class:`TradeRepublicClient` backed by pytr for one TR account."""

    def __init__(self, config: InstanceConfig, session_dir: str) -> None:
        self._config = config
        self._session_dir = session_dir
        self._api: TradeRepublicApi | None = None

    # ------------------------------------------------------------------
    # Port implementation
    # ------------------------------------------------------------------

    async def resume_session(self) -> bool:
        api = self._ensure_api()
        try:
            return bool(await self._run(api.resume_websession))
        except requests.exceptions.RequestException as exc:
            self._raise_upstream(exc)

    async def start_login(self) -> LoginChallenge:
        api = self._ensure_api()
        try:
            await self._run(api.initiate_weblogin)
        except requests.exceptions.RequestException as exc:
            self._raise_upstream(exc)
        if api.weblogin_needs_authenticator:
            return LoginChallenge.authenticator
        return LoginChallenge.push

    async def complete_login(self, code: str | None = None) -> None:
        api = self._ensure_api()
        try:
            if code is None:
                await self._run(api.complete_weblogin)
            else:
                await self._run(api.complete_weblogin, code)
        except ValueError as exc:
            logger.warning("Instance %r: 2FA code rejected: %s", self._config.name, exc)
            raise CodeRejectedError(str(exc)) from exc
        except requests.exceptions.RequestException as exc:
            self._raise_upstream(exc)

    async def fetch_timeline(self, since: datetime, until: datetime) -> list[dict]:
        api = self._ensure_api()
        timeline = Timeline(
            api,
            Path(self._session_dir),
            not_before=since.timestamp(),
            not_after=until.timestamp(),
            store_event_database=False,
        )
        try:
            await timeline.tl_loop()
        except Exception as exc:
            # asyncio.CancelledError is a BaseException, not an Exception, so it
            # is intentionally not caught here and propagates for graceful task
            # cancellation.
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code == 401:
                logger.info(
                    "Instance %r: timeline request returned 401; session expired.",
                    self._config.name,
                )
                raise SessionExpiredError(
                    f"Trade Republic session for instance {self._config.name!r} "
                    f"has expired; login required."
                ) from exc
            logger.warning(
                "Instance %r: timeline fetch failed: %s",
                self._config.name,
                exc,
                exc_info=True,
            )
            detail = str(exc) or type(exc).__name__
            raise TrUpstreamError(
                f"Trade Republic timeline fetch failed for instance "
                f"{self._config.name!r}: {detail}"
            ) from exc
        return list(timeline.events)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run(self, func: Callable[..., _T], *args: object) -> _T:
        """Run a blocking pytr callable on the default executor thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)

    def _ensure_api(self) -> TradeRepublicApi:
        """Build the pytr API handle on first use and reuse it thereafter."""
        if self._api is None:
            self._api = self._build_api()
        return self._api

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

    def _raise_upstream(self, exc: requests.exceptions.RequestException) -> NoReturn:
        """Translate a ``requests`` failure into a domain error.

        HTTP 429 becomes :class:`RateLimitedError`; anything else becomes
        :class:`TrUpstreamError`.
        """
        response = getattr(exc, "response", None)
        status_code = response.status_code if response is not None else None
        if status_code == 429:
            logger.warning("Instance %r: login rate-limited.", self._config.name)
            raise RateLimitedError(
                f"Trade Republic rate-limited login for instance {self._config.name!r}."
            ) from exc
        logger.warning("Instance %r: upstream error: %s", self._config.name, exc)
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
