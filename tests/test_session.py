"""Tests for the login/2FA use case (application.session.InstanceSession).

The use case is exercised through an in-memory fake of the ``TradeRepublicClient``
secondary port — no pytr internals are mocked here. pytr-specific translation is
covered separately in ``test_pytr_client.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import ClassVar

import pytest

from tr_bridge.application.ports import TradeRepublicClient
from tr_bridge.application.session import InstanceSession
from tr_bridge.domain.state import (
    CodeRejectedError,
    InvalidStateError,
    LoginChallenge,
    LoginInProgressError,
    NoLoginPendingError,
    RateLimitedError,
    SessionExpiredError,
    SessionState,
    TrUpstreamError,
)

_TFA_TIMEOUT = 120


class FakeClient(TradeRepublicClient):
    """Controllable in-memory implementation of the secondary port."""

    def __init__(
        self,
        *,
        resume_returns: bool = False,
        challenge: LoginChallenge = LoginChallenge.push,
        resume_error: Exception | None = None,
        start_error: Exception | None = None,
        complete_error: Exception | None = None,
        complete_delay: float = 0.0,
        start_delay: float = 0.0,
        timeline_events: list[dict] | None = None,
        timeline_error: Exception | None = None,
    ) -> None:
        self._resume_returns = resume_returns
        self._challenge = challenge
        self._resume_error = resume_error
        self._start_error = start_error
        self._complete_error = complete_error
        self._complete_delay = complete_delay
        self._start_delay = start_delay
        self._timeline_events = timeline_events or []
        self._timeline_error = timeline_error
        self.complete_calls: list[str | None] = []
        self.gate: asyncio.Event | None = None

    async def resume_session(self) -> bool:
        if self._resume_error is not None:
            raise self._resume_error
        return self._resume_returns

    async def start_login(self) -> LoginChallenge:
        if self._start_delay:
            await asyncio.sleep(self._start_delay)
        if self._start_error is not None:
            raise self._start_error
        return self._challenge

    async def complete_login(self, code: str | None = None) -> None:
        self.complete_calls.append(code)
        if self._complete_delay:
            await asyncio.sleep(self._complete_delay)
        if self.gate is not None:
            await self.gate.wait()
        if self._complete_error is not None:
            raise self._complete_error

    async def fetch_timeline(self, since: datetime, until: datetime) -> list[dict]:
        if self._timeline_error is not None:
            raise self._timeline_error
        return list(self._timeline_events)


def _make_session(
    client: FakeClient, tfa_timeout: int = _TFA_TIMEOUT
) -> InstanceSession:
    return InstanceSession(name="user1", client=client, tfa_timeout=tfa_timeout)


async def _authenticator_session(
    tfa_timeout: int = _TFA_TIMEOUT,
    complete_error: Exception | None = None,
    complete_delay: float = 0.0,
) -> tuple[InstanceSession, FakeClient]:
    client = FakeClient(
        challenge=LoginChallenge.authenticator,
        complete_error=complete_error,
        complete_delay=complete_delay,
    )
    session = _make_session(client, tfa_timeout=tfa_timeout)
    state = await session.start_login()
    assert state == SessionState.authenticator
    return session, client


async def _pending_push_session() -> tuple[InstanceSession, FakeClient]:
    """Drive a session into ``push`` and hold it there with a gate."""
    client = FakeClient(challenge=LoginChallenge.push)
    client.gate = asyncio.Event()
    session = _make_session(client)
    state = await session.start_login()
    assert state == SessionState.push
    return session, client


async def _wait_until_left_push(session: InstanceSession) -> None:
    for _ in range(100):
        if session.state != SessionState.push:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("push confirmation task never completed")


class TestInitialState:
    def test_initial_state_is_idle(self) -> None:
        session = _make_session(FakeClient())
        assert session.state == SessionState.idle


class TestResume:
    @pytest.mark.asyncio
    async def test_resume_sets_confirmed_when_session_valid(self) -> None:
        session = _make_session(FakeClient(resume_returns=True))
        await session.resume()
        assert session.state == SessionState.confirmed

    @pytest.mark.asyncio
    async def test_resume_stays_idle_when_session_invalid(self) -> None:
        session = _make_session(FakeClient(resume_returns=False))
        await session.resume()
        assert session.state == SessionState.idle


class TestStartLogin:
    @pytest.mark.asyncio
    async def test_start_login_confirms_when_resume_succeeds(self) -> None:
        session = _make_session(FakeClient(resume_returns=True))
        state = await session.start_login()
        assert state == SessionState.confirmed
        assert session.state == SessionState.confirmed

    @pytest.mark.asyncio
    async def test_start_login_goes_authenticator_when_needed(self) -> None:
        session = _make_session(FakeClient(challenge=LoginChallenge.authenticator))
        state = await session.start_login()
        assert state == SessionState.authenticator
        assert session.state == SessionState.authenticator

    @pytest.mark.asyncio
    async def test_start_login_goes_push_when_no_authenticator(self) -> None:
        session = _make_session(FakeClient(challenge=LoginChallenge.push))
        state = await session.start_login()
        assert state == SessionState.push
        assert session.state == SessionState.push
        await asyncio.sleep(0.05)  # let the push task settle

    @pytest.mark.asyncio
    async def test_concurrent_login_raises_409_from_authenticator(self) -> None:
        session, _client = await _authenticator_session()
        with pytest.raises(LoginInProgressError):
            await session.start_login()

    @pytest.mark.asyncio
    async def test_concurrent_login_raises_409_from_push(self) -> None:
        session, client = await _pending_push_session()
        try:
            with pytest.raises(LoginInProgressError):
                await session.start_login()
        finally:
            client.gate.set()
            await _wait_until_left_push(session)

    @pytest.mark.asyncio
    async def test_second_login_racing_for_the_lock_raises_409(self) -> None:
        # Two logins started from ``idle`` both pass the pre-lock guard; the
        # second must be rejected once it acquires the lock and re-checks state.
        # ``start_delay`` forces the first login to yield inside the lock so the
        # second reaches the in-lock re-check.
        session = _make_session(
            FakeClient(challenge=LoginChallenge.authenticator, start_delay=0.02)
        )
        results = await asyncio.gather(
            session.start_login(),
            session.start_login(),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, Exception)]
        successes = [r for r in results if not isinstance(r, Exception)]
        assert successes == [SessionState.authenticator]
        assert len(errors) == 1
        assert isinstance(errors[0], LoginInProgressError)

    @pytest.mark.asyncio
    async def test_start_login_rate_limited_propagates_and_fails(self) -> None:
        session = _make_session(FakeClient(start_error=RateLimitedError("slow")))
        with pytest.raises(RateLimitedError):
            await session.start_login()
        assert session.state == SessionState.failed

    @pytest.mark.asyncio
    async def test_start_login_upstream_error_propagates_and_fails(self) -> None:
        session = _make_session(FakeClient(start_error=TrUpstreamError("boom")))
        with pytest.raises(TrUpstreamError):
            await session.start_login()
        assert session.state == SessionState.failed

    @pytest.mark.asyncio
    async def test_start_login_upstream_error_when_resume_fails(self) -> None:
        session = _make_session(FakeClient(resume_error=TrUpstreamError("boom")))
        with pytest.raises(TrUpstreamError):
            await session.start_login()
        assert session.state == SessionState.failed


class TestSubmit2FA:
    @pytest.mark.asyncio
    async def test_submit_2fa_confirms_session(self) -> None:
        session, client = await _authenticator_session()
        await session.submit_2fa("123456")
        assert session.state == SessionState.confirmed
        assert client.complete_calls == ["123456"]

    @pytest.mark.asyncio
    async def test_submit_2fa_raises_code_rejected_on_wrong_code(self) -> None:
        session, _client = await _authenticator_session(
            complete_error=CodeRejectedError("wrong code")
        )
        with pytest.raises(CodeRejectedError):
            await session.submit_2fa("000000")
        assert session.state == SessionState.authenticator

    @pytest.mark.asyncio
    async def test_submit_2fa_rate_limited_fails(self) -> None:
        session, _client = await _authenticator_session(
            complete_error=RateLimitedError("slow")
        )
        with pytest.raises(RateLimitedError):
            await session.submit_2fa("123456")
        assert session.state == SessionState.failed

    @pytest.mark.asyncio
    async def test_submit_2fa_upstream_error_fails(self) -> None:
        session, _client = await _authenticator_session(
            complete_error=TrUpstreamError("boom")
        )
        with pytest.raises(TrUpstreamError):
            await session.submit_2fa("123456")
        assert session.state == SessionState.failed

    @pytest.mark.asyncio
    async def test_submit_2fa_wrong_state_raises_no_login_pending(self) -> None:
        session = _make_session(FakeClient())  # fresh session is idle
        with pytest.raises(NoLoginPendingError):
            await session.submit_2fa("123456")

    @pytest.mark.asyncio
    async def test_no_login_pending_is_invalid_state(self) -> None:
        session = _make_session(FakeClient())  # fresh session is idle
        with pytest.raises(InvalidStateError):
            await session.submit_2fa("123456")

    @pytest.mark.asyncio
    async def test_submit_2fa_ignored_when_timeout_fires_during_call(self) -> None:
        # With a zero timeout the login expires while ``complete_login`` is still
        # running; the submission must be ignored and the session left in
        # ``failed`` rather than flipped to ``confirmed``.
        session, _client = await _authenticator_session(
            tfa_timeout=0, complete_delay=0.05
        )
        await session.submit_2fa("123456")
        assert session.state == SessionState.failed


class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_transitions_authenticator_to_failed(self) -> None:
        session = _make_session(
            FakeClient(challenge=LoginChallenge.authenticator), tfa_timeout=0
        )
        await session.start_login()
        await asyncio.sleep(0.05)
        assert session.state == SessionState.failed

    @pytest.mark.asyncio
    async def test_timeout_transitions_push_to_failed(self) -> None:
        client = FakeClient(challenge=LoginChallenge.push, complete_delay=0.5)
        session = _make_session(client, tfa_timeout=0)
        await session.start_login()
        await asyncio.sleep(0.05)
        assert session.state == SessionState.failed

    @pytest.mark.asyncio
    async def test_timeout_does_not_fire_after_confirmation(self) -> None:
        session, _client = await _authenticator_session(tfa_timeout=10)
        await session.submit_2fa("123456")
        await asyncio.sleep(0.05)
        assert session.state == SessionState.confirmed


class TestPushConfirmation:
    @pytest.mark.asyncio
    async def test_push_transitions_to_confirmed_after_approval(self) -> None:
        session = _make_session(FakeClient(challenge=LoginChallenge.push))
        await session.start_login()
        await asyncio.sleep(0.05)
        assert session.state == SessionState.confirmed

    @pytest.mark.asyncio
    async def test_push_transitions_to_failed_on_api_error(self) -> None:
        session = _make_session(
            FakeClient(
                challenge=LoginChallenge.push,
                complete_error=ValueError("rejected"),
            )
        )
        await session.start_login()
        await asyncio.sleep(0.05)
        assert session.state == SessionState.failed


async def _confirmed_session(
    timeline_events: list[dict] | None = None,
    timeline_error: Exception | None = None,
) -> InstanceSession:
    client = FakeClient(
        resume_returns=True,
        timeline_events=timeline_events,
        timeline_error=timeline_error,
    )
    session = _make_session(client)
    await session.resume()
    return session


class TestFetchTimeline:
    _SINCE = datetime(2026, 8, 1, tzinfo=UTC)
    _UNTIL = datetime(2026, 8, 10, tzinfo=UTC)
    _EVENTS: ClassVar[list[dict]] = [
        {"id": "e1", "timestamp": "2026-08-03T10:12:04.000+0000"}
    ]

    @pytest.mark.asyncio
    async def test_returns_events_from_port(self) -> None:
        session = await _confirmed_session(timeline_events=self._EVENTS)
        events = await session.fetch_timeline(self._SINCE, self._UNTIL)
        assert events == self._EVENTS

    @pytest.mark.asyncio
    async def test_raises_session_expired_when_not_confirmed(self) -> None:
        session = _make_session(FakeClient())
        with pytest.raises(SessionExpiredError):
            await session.fetch_timeline(self._SINCE, self._UNTIL)

    @pytest.mark.asyncio
    async def test_propagates_upstream_error_from_port(self) -> None:
        session = await _confirmed_session(timeline_error=TrUpstreamError("ws boom"))
        with pytest.raises(TrUpstreamError, match="ws boom"):
            await session.fetch_timeline(self._SINCE, self._UNTIL)

    @pytest.mark.asyncio
    async def test_propagates_session_expired_from_port(self) -> None:
        session = await _confirmed_session(timeline_error=SessionExpiredError("gone"))
        with pytest.raises(SessionExpiredError):
            await session.fetch_timeline(self._SINCE, self._UNTIL)
