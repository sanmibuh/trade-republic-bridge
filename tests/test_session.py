"""Tests for tr_bridge.session — InstanceSession state machine."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest
import requests

from tr_bridge.config import InstanceConfig
from tr_bridge.session import (
    CodeRejectedError,
    InstanceSession,
    InvalidStateError,
    LoginInProgressError,
    NoLoginPendingError,
    RateLimitedError,
    SessionExpiredError,
    SessionState,
    TrUpstreamError,
)

_INSTANCE = InstanceConfig(name="user1", phone="+49123456789", pin="1234")
_TFA_TIMEOUT = 120


def _make_session(tmp_path: Path, **kwargs) -> InstanceSession:
    defaults: dict = {
        "config": _INSTANCE,
        "session_dir": str(tmp_path / "tr_session_user1"),
        "tfa_timeout": _TFA_TIMEOUT,
    }
    defaults.update(kwargs)
    return InstanceSession(**defaults)


def _mock_api(
    resume_returns: bool = False,
    needs_authenticator: bool = False,
    complete_raises: Exception | None = None,
) -> MagicMock:
    api = MagicMock()
    api.resume_websession.return_value = resume_returns
    api.weblogin_needs_authenticator = needs_authenticator
    api.initiate_weblogin.return_value = 120
    if complete_raises:
        api.complete_weblogin.side_effect = complete_raises
    else:
        api.complete_weblogin.return_value = None
    return api


class TestInitialState:
    def test_initial_state_is_idle(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        assert session.state == SessionState.idle


class TestResume:
    @pytest.mark.asyncio
    async def test_resume_sets_confirmed_when_session_valid(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path)
        api = _mock_api(resume_returns=True)
        with patch("tr_bridge.session.TradeRepublicApi", return_value=api):
            await session.resume()
        assert session.state == SessionState.confirmed

    @pytest.mark.asyncio
    async def test_resume_stays_idle_when_session_invalid(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        api = _mock_api(resume_returns=False)
        with patch("tr_bridge.session.TradeRepublicApi", return_value=api):
            await session.resume()
        assert session.state == SessionState.idle


class TestStartLogin:
    @pytest.mark.asyncio
    async def test_start_login_confirms_when_resume_succeeds(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path)
        api = _mock_api(resume_returns=True)
        with patch("tr_bridge.session.TradeRepublicApi", return_value=api):
            state = await session.start_login()
        assert state == SessionState.confirmed
        assert session.state == SessionState.confirmed

    @pytest.mark.asyncio
    async def test_start_login_goes_authenticator_when_needed(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path)
        api = _mock_api(resume_returns=False, needs_authenticator=True)
        with patch("tr_bridge.session.TradeRepublicApi", return_value=api):
            state = await session.start_login()
        assert state == SessionState.authenticator
        assert session.state == SessionState.authenticator

    @pytest.mark.asyncio
    async def test_start_login_goes_push_when_no_authenticator(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path)
        api = _mock_api(resume_returns=False, needs_authenticator=False)
        with patch("tr_bridge.session.TradeRepublicApi", return_value=api):
            state = await session.start_login()
        assert state == SessionState.push
        assert session.state == SessionState.push

    @pytest.mark.asyncio
    async def test_concurrent_login_raises_409_from_authenticator(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path)
        session._state = SessionState.authenticator
        with pytest.raises(LoginInProgressError):
            await session.start_login()

    @pytest.mark.asyncio
    async def test_concurrent_login_raises_409_from_push(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        session._state = SessionState.push
        with pytest.raises(LoginInProgressError):
            await session.start_login()

    @staticmethod
    def _http_error(status_code: int) -> requests.exceptions.HTTPError:
        response = requests.Response()
        response.status_code = status_code
        return requests.exceptions.HTTPError(response=response)

    @pytest.mark.asyncio
    async def test_start_login_rate_limited_on_429(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        api = _mock_api(resume_returns=False)
        api.initiate_weblogin.side_effect = self._http_error(429)
        with (
            patch("tr_bridge.session.TradeRepublicApi", return_value=api),
            pytest.raises(RateLimitedError),
        ):
            await session.start_login()
        assert session.state == SessionState.failed

    @pytest.mark.asyncio
    async def test_start_login_upstream_error_on_500(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        api = _mock_api(resume_returns=False)
        api.initiate_weblogin.side_effect = self._http_error(500)
        with (
            patch("tr_bridge.session.TradeRepublicApi", return_value=api),
            pytest.raises(TrUpstreamError),
        ):
            await session.start_login()
        assert session.state == SessionState.failed

    @pytest.mark.asyncio
    async def test_start_login_upstream_error_on_connection_failure(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path)
        api = _mock_api(resume_returns=False)
        api.initiate_weblogin.side_effect = requests.exceptions.ConnectionError("boom")
        with (
            patch("tr_bridge.session.TradeRepublicApi", return_value=api),
            pytest.raises(TrUpstreamError),
        ):
            await session.start_login()
        assert session.state == SessionState.failed

    @pytest.mark.asyncio
    async def test_start_login_upstream_error_when_resume_fails(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path)
        api = _mock_api()
        api.resume_websession.side_effect = self._http_error(503)
        with (
            patch("tr_bridge.session.TradeRepublicApi", return_value=api),
            pytest.raises(TrUpstreamError),
        ):
            await session.start_login()
        assert session.state == SessionState.failed


class TestSubmit2FA:
    @pytest.mark.asyncio
    async def test_submit_2fa_confirms_session(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        api = _mock_api()
        session._api = api
        session._state = SessionState.authenticator
        await session.submit_2fa("123456")
        assert session.state == SessionState.confirmed
        api.complete_weblogin.assert_called_once_with("123456")

    @pytest.mark.asyncio
    async def test_submit_2fa_raises_code_rejected_on_wrong_code(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path)
        api = _mock_api(complete_raises=ValueError("wrong code"))
        session._api = api
        session._state = SessionState.authenticator
        with pytest.raises(CodeRejectedError):
            await session.submit_2fa("000000")
        assert session.state == SessionState.authenticator

    @pytest.mark.asyncio
    async def test_submit_2fa_rate_limited_on_429(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        response = requests.Response()
        response.status_code = 429
        api = _mock_api(
            complete_raises=requests.exceptions.HTTPError(response=response)
        )
        session._api = api
        session._state = SessionState.authenticator
        with pytest.raises(RateLimitedError):
            await session.submit_2fa("123456")
        assert session.state == SessionState.failed

    @pytest.mark.asyncio
    async def test_submit_2fa_upstream_error_on_request_exception(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path)
        api = _mock_api(complete_raises=requests.exceptions.ConnectionError("boom"))
        session._api = api
        session._state = SessionState.authenticator
        with pytest.raises(TrUpstreamError):
            await session.submit_2fa("123456")
        assert session.state == SessionState.failed

    @pytest.mark.asyncio
    async def test_upstream_error_message_includes_status_and_instance(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path)
        response = requests.Response()
        response.status_code = 503
        # HTTPError raised without args stringifies to "" — the message must
        # still carry the status code and instance name.
        api = _mock_api(
            complete_raises=requests.exceptions.HTTPError(response=response)
        )
        session._api = api
        session._state = SessionState.authenticator
        with pytest.raises(TrUpstreamError) as excinfo:
            await session.submit_2fa("123456")
        message = str(excinfo.value)
        assert "503" in message
        assert "user1" in message

    @pytest.mark.asyncio
    async def test_upstream_error_message_falls_back_to_exception_type(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path)
        api = _mock_api(complete_raises=requests.exceptions.ConnectionError())
        session._api = api
        session._state = SessionState.authenticator
        with pytest.raises(TrUpstreamError) as excinfo:
            await session.submit_2fa("123456")
        message = str(excinfo.value)
        assert "ConnectionError" in message
        assert "user1" in message

    @pytest.mark.asyncio
    async def test_submit_2fa_wrong_state_raises_no_login_pending(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path)
        session._state = SessionState.idle
        with pytest.raises(NoLoginPendingError):
            await session.submit_2fa("123456")

    @pytest.mark.asyncio
    async def test_no_login_pending_is_invalid_state(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        session._state = SessionState.idle
        with pytest.raises(InvalidStateError):
            await session.submit_2fa("123456")

    @pytest.mark.asyncio
    async def test_submit_2fa_without_api_raises_no_login_pending(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path)
        session._state = SessionState.authenticator
        session._api = None
        with pytest.raises(NoLoginPendingError):
            await session.submit_2fa("123456")


class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_transitions_authenticator_to_failed(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path, tfa_timeout=0)
        api = _mock_api(resume_returns=False, needs_authenticator=True)
        with patch("tr_bridge.session.TradeRepublicApi", return_value=api):
            await session.start_login()
        await asyncio.sleep(0.05)
        assert session.state == SessionState.failed

    @pytest.mark.asyncio
    async def test_timeout_transitions_push_to_failed(self, tmp_path: Path) -> None:
        import time

        session = _make_session(tmp_path, tfa_timeout=0)
        api = _mock_api(resume_returns=False, needs_authenticator=False)

        def _blocking_complete(*_args):
            # A short sleep is enough: tfa_timeout=0 fires before this returns.
            time.sleep(0.5)

        api.complete_weblogin.side_effect = _blocking_complete
        with patch("tr_bridge.session.TradeRepublicApi", return_value=api):
            await session.start_login()
        await asyncio.sleep(0.05)
        assert session.state == SessionState.failed

    @pytest.mark.asyncio
    async def test_timeout_does_not_fire_after_confirmation(
        self, tmp_path: Path
    ) -> None:
        # Use a large timeout so it cannot fire before submit_2fa completes.
        session = _make_session(tmp_path, tfa_timeout=10)
        api = _mock_api()
        session._api = api
        session._state = SessionState.authenticator
        session._schedule_timeout()
        await session.submit_2fa("123456")
        await asyncio.sleep(0.05)
        assert session.state == SessionState.confirmed


class TestPushConfirmation:
    @pytest.mark.asyncio
    async def test_push_transitions_to_confirmed_after_approval(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path, tfa_timeout=120)
        api = _mock_api(resume_returns=False, needs_authenticator=False)
        with patch("tr_bridge.session.TradeRepublicApi", return_value=api):
            await session.start_login()
        await asyncio.sleep(0.05)
        assert session.state == SessionState.confirmed

    @pytest.mark.asyncio
    async def test_push_transitions_to_failed_on_api_error(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path, tfa_timeout=120)
        api = _mock_api(
            resume_returns=False,
            needs_authenticator=False,
            complete_raises=ValueError("rejected"),
        )
        with patch("tr_bridge.session.TradeRepublicApi", return_value=api):
            await session.start_login()
        await asyncio.sleep(0.05)
        assert session.state == SessionState.failed


async def _confirmed_session(tmp_path: Path) -> InstanceSession:
    """Return a session driven into the ``confirmed`` state with a mock API."""
    session = _make_session(tmp_path)
    api = _mock_api(resume_returns=True)
    with patch("tr_bridge.session.TradeRepublicApi", return_value=api):
        await session.resume()
    return session


class _FakeTimeline:
    """Stand-in for pytr's ``Timeline`` that records its window and events."""

    last_kwargs: ClassVar[dict] = {}

    def __init__(self, tr, output_path, **kwargs) -> None:
        type(self).last_kwargs = {
            "tr": tr,
            "output_path": output_path,
            **kwargs,
        }
        self.events = [{"id": "e1", "timestamp": "2026-08-03T10:12:04.000+0000"}]

    async def tl_loop(self) -> None:
        return None


class TestFetchTimeline:
    _SINCE = datetime(2026, 8, 1, tzinfo=UTC)
    _UNTIL = datetime(2026, 8, 10, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_returns_raw_events(self, tmp_path: Path) -> None:
        session = await _confirmed_session(tmp_path)
        with patch("tr_bridge.session.Timeline", _FakeTimeline):
            events = await session.fetch_timeline(self._SINCE, self._UNTIL)
        assert events == [{"id": "e1", "timestamp": "2026-08-03T10:12:04.000+0000"}]

    @pytest.mark.asyncio
    async def test_passes_time_window_to_pytr(self, tmp_path: Path) -> None:
        session = await _confirmed_session(tmp_path)
        with patch("tr_bridge.session.Timeline", _FakeTimeline):
            await session.fetch_timeline(self._SINCE, self._UNTIL)
        assert _FakeTimeline.last_kwargs["not_before"] == self._SINCE.timestamp()
        assert _FakeTimeline.last_kwargs["not_after"] == self._UNTIL.timestamp()
        assert _FakeTimeline.last_kwargs["store_event_database"] is False

    @pytest.mark.asyncio
    async def test_raises_session_expired_when_not_confirmed(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path)
        with pytest.raises(SessionExpiredError):
            await session.fetch_timeline(self._SINCE, self._UNTIL)

    @pytest.mark.asyncio
    async def test_raises_upstream_error_when_pytr_fails(self, tmp_path: Path) -> None:
        session = await _confirmed_session(tmp_path)

        class _FailingTimeline(_FakeTimeline):
            async def tl_loop(self) -> None:
                raise RuntimeError("ws boom")

        with (
            patch("tr_bridge.session.Timeline", _FailingTimeline),
            pytest.raises(TrUpstreamError, match="ws boom"),
        ):
            await session.fetch_timeline(self._SINCE, self._UNTIL)

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self, tmp_path: Path) -> None:
        session = await _confirmed_session(tmp_path)

        class _CancelledTimeline(_FakeTimeline):
            async def tl_loop(self) -> None:
                raise asyncio.CancelledError

        with (
            patch("tr_bridge.session.Timeline", _CancelledTimeline),
            pytest.raises(asyncio.CancelledError),
        ):
            await session.fetch_timeline(self._SINCE, self._UNTIL)
