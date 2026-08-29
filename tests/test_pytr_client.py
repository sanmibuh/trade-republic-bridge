"""Tests for the pytr secondary adapter (adapters.pytr.pytr_client.PytrClient).

These are the only tests that mock pytr internals; they verify the adapter's
translation of pytr/``requests`` failures into domain errors and its async
bridging of blocking calls.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest
import requests

from tr_bridge.adapters.pytr.pytr_client import PytrClient
from tr_bridge.config import InstanceConfig
from tr_bridge.domain.state import (
    CodeRejectedError,
    LoginChallenge,
    RateLimitedError,
    SessionExpiredError,
    TrUpstreamError,
)

_INSTANCE = InstanceConfig(name="user1", phone="+49123456789", pin="1234")


def _make_client(tmp_path: Path) -> PytrClient:
    return PytrClient(config=_INSTANCE, session_dir=str(tmp_path / "tr_session_user1"))


def _mock_api(
    resume_returns: bool = False,
    needs_authenticator: bool = False,
) -> MagicMock:
    api = MagicMock()
    api.resume_websession.return_value = resume_returns
    api.weblogin_needs_authenticator = needs_authenticator
    api.initiate_weblogin.return_value = 120
    api.complete_weblogin.return_value = None
    return api


def _http_error(status_code: int) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    return requests.exceptions.HTTPError(response=response)


class TestResumeSession:
    @pytest.mark.asyncio
    async def test_returns_true_on_success(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        api = _mock_api(resume_returns=True)
        with patch(
            "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi", return_value=api
        ):
            assert await client.resume_session() is True

    @pytest.mark.asyncio
    async def test_returns_false_when_no_session(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        api = _mock_api(resume_returns=False)
        with patch(
            "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi", return_value=api
        ):
            assert await client.resume_session() is False

    @pytest.mark.asyncio
    async def test_upstream_error_on_request_exception(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        api = _mock_api()
        api.resume_websession.side_effect = _http_error(503)
        with (
            patch(
                "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi",
                return_value=api,
            ),
            pytest.raises(TrUpstreamError),
        ):
            await client.resume_session()


class TestStartLogin:
    @pytest.mark.asyncio
    async def test_returns_authenticator_challenge(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        api = _mock_api(needs_authenticator=True)
        with patch(
            "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi", return_value=api
        ):
            assert await client.start_login() is LoginChallenge.authenticator

    @pytest.mark.asyncio
    async def test_returns_push_challenge(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        api = _mock_api(needs_authenticator=False)
        with patch(
            "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi", return_value=api
        ):
            assert await client.start_login() is LoginChallenge.push

    @pytest.mark.asyncio
    async def test_rate_limited_on_429(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        api = _mock_api()
        api.initiate_weblogin.side_effect = _http_error(429)
        with (
            patch(
                "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi",
                return_value=api,
            ),
            pytest.raises(RateLimitedError),
        ):
            await client.start_login()

    @pytest.mark.asyncio
    async def test_upstream_error_on_500(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        api = _mock_api()
        api.initiate_weblogin.side_effect = _http_error(500)
        with (
            patch(
                "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi",
                return_value=api,
            ),
            pytest.raises(TrUpstreamError),
        ):
            await client.start_login()

    @pytest.mark.asyncio
    async def test_upstream_error_on_connection_failure(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        api = _mock_api()
        api.initiate_weblogin.side_effect = requests.exceptions.ConnectionError("boom")
        with (
            patch(
                "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi",
                return_value=api,
            ),
            pytest.raises(TrUpstreamError),
        ):
            await client.start_login()


class TestCompleteLogin:
    @pytest.mark.asyncio
    async def test_passes_code_to_pytr(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        api = _mock_api()
        with patch(
            "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi", return_value=api
        ):
            await client.complete_login("123456")
        api.complete_weblogin.assert_called_once_with("123456")

    @pytest.mark.asyncio
    async def test_authenticator_polls_confirmation_and_saves(
        self, tmp_path: Path
    ) -> None:
        # pytr's authenticator path verifies the code but, unlike the push path,
        # never polls the login process to completion — so the real session
        # cookies are never issued. The adapter must poll and persist them.
        client = _make_client(tmp_path)
        api = _mock_api()
        order: list[str] = []
        api.complete_weblogin.side_effect = lambda code: order.append("verify")
        api._await_weblogin_confirmation.side_effect = lambda: order.append("confirm")
        api.save_websession.side_effect = lambda: order.append("save")
        with patch(
            "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi", return_value=api
        ):
            await client.complete_login("123456")
        assert order == ["verify", "confirm", "save"]

    @pytest.mark.asyncio
    async def test_missing_confirmation_hook_maps_to_upstream(
        self, tmp_path: Path
    ) -> None:
        # If a pytr upgrade renames/removes the private confirmation hook, fail
        # with a clear mapped error instead of an unhandled AttributeError.
        client = _make_client(tmp_path)
        api = _mock_api()
        del api._await_weblogin_confirmation
        with (
            patch(
                "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi", return_value=api
            ),
            pytest.raises(TrUpstreamError, match="pytr"),
        ):
            await client.complete_login("123456")

    @pytest.mark.asyncio
    async def test_push_does_not_poll_confirmation(self, tmp_path: Path) -> None:
        # The push path already polls internally inside complete_weblogin(); the
        # adapter must not poll again or re-save.
        client = _make_client(tmp_path)
        api = _mock_api()
        with patch(
            "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi", return_value=api
        ):
            await client.complete_login()
        api._await_weblogin_confirmation.assert_not_called()
        api.save_websession.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_pytr_without_code_for_push(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        api = _mock_api()
        with patch(
            "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi", return_value=api
        ):
            await client.complete_login()
        api.complete_weblogin.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_value_error_maps_to_code_rejected(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        api = _mock_api()
        api.complete_weblogin.side_effect = ValueError("wrong code")
        with (
            patch(
                "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi",
                return_value=api,
            ),
            pytest.raises(CodeRejectedError),
        ):
            await client.complete_login("000000")

    @pytest.mark.asyncio
    async def test_rate_limited_on_429(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        api = _mock_api()
        api.complete_weblogin.side_effect = _http_error(429)
        with (
            patch(
                "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi",
                return_value=api,
            ),
            pytest.raises(RateLimitedError),
        ):
            await client.complete_login("123456")

    @pytest.mark.asyncio
    async def test_upstream_message_includes_status_and_instance(
        self, tmp_path: Path
    ) -> None:
        client = _make_client(tmp_path)
        api = _mock_api()
        # HTTPError raised without args stringifies to "" — the message must
        # still carry the status code and instance name.
        api.complete_weblogin.side_effect = _http_error(503)
        with (
            patch(
                "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi", return_value=api
            ),
            pytest.raises(TrUpstreamError) as excinfo,
        ):
            await client.complete_login("123456")
        message = str(excinfo.value)
        assert "503" in message
        assert "user1" in message

    @pytest.mark.asyncio
    async def test_upstream_message_falls_back_to_exception_type(
        self, tmp_path: Path
    ) -> None:
        client = _make_client(tmp_path)
        api = _mock_api()
        api.complete_weblogin.side_effect = requests.exceptions.ConnectionError()
        with (
            patch(
                "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi", return_value=api
            ),
            pytest.raises(TrUpstreamError) as excinfo,
        ):
            await client.complete_login("123456")
        message = str(excinfo.value)
        assert "ConnectionError" in message
        assert "user1" in message


class _FakeTimeline:
    """Stand-in for pytr's ``Timeline`` that records its window and events."""

    last_kwargs: ClassVar[dict] = {}

    def __init__(self, tr, output_path, **kwargs) -> None:
        type(self).last_kwargs = {"tr": tr, "output_path": output_path, **kwargs}
        self.events = [{"id": "e1", "timestamp": "2026-08-03T10:12:04.000+0000"}]

    async def tl_loop(self) -> None:
        return None


async def _confirmed_client_with_api(tmp_path: Path) -> tuple[PytrClient, MagicMock]:
    client = _make_client(tmp_path)
    api = _mock_api(resume_returns=True)
    with patch(
        "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi", return_value=api
    ):
        await client.resume_session()
    return client, api


async def _confirmed_client(tmp_path: Path) -> PytrClient:
    client, _ = await _confirmed_client_with_api(tmp_path)
    return client


class TestFetchTimeline:
    _SINCE = datetime(2026, 8, 1, tzinfo=UTC)
    _UNTIL = datetime(2026, 8, 10, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_returns_raw_events(self, tmp_path: Path) -> None:
        client = await _confirmed_client(tmp_path)
        with patch("tr_bridge.adapters.pytr.pytr_client.Timeline", _FakeTimeline):
            events = await client.fetch_timeline(self._SINCE, self._UNTIL)
        assert events == [{"id": "e1", "timestamp": "2026-08-03T10:12:04.000+0000"}]

    @pytest.mark.asyncio
    async def test_refreshes_web_session_before_opening_websocket(
        self, tmp_path: Path
    ) -> None:
        # pytr's websocket authenticates via session cookies but never refreshes
        # them; a fresh REST call must mint the cookie before the ws is opened,
        # otherwise Trade Republic answers "No auth token".
        client = _make_client(tmp_path)
        api = _mock_api(resume_returns=True)
        with patch(
            "tr_bridge.adapters.pytr.pytr_client.TradeRepublicApi", return_value=api
        ):
            await client.resume_session()

        order: list[str] = []

        class _RecordingTimeline(_FakeTimeline):
            async def tl_loop(self) -> None:
                order.append("tl_loop")

        api.settings.side_effect = lambda: order.append("settings")

        with patch("tr_bridge.adapters.pytr.pytr_client.Timeline", _RecordingTimeline):
            await client.fetch_timeline(self._SINCE, self._UNTIL)

        assert order == ["settings", "tl_loop"]

    @pytest.mark.asyncio
    async def test_session_refresh_401_maps_to_session_expired(
        self, tmp_path: Path
    ) -> None:
        client, api = await _confirmed_client_with_api(tmp_path)
        api.settings.side_effect = _http_error(401)
        with (
            patch("tr_bridge.adapters.pytr.pytr_client.Timeline", _FakeTimeline),
            pytest.raises(SessionExpiredError),
        ):
            await client.fetch_timeline(self._SINCE, self._UNTIL)

    @pytest.mark.asyncio
    async def test_session_refresh_429_maps_to_rate_limited(
        self, tmp_path: Path
    ) -> None:
        client, api = await _confirmed_client_with_api(tmp_path)
        api.settings.side_effect = _http_error(429)
        with (
            patch("tr_bridge.adapters.pytr.pytr_client.Timeline", _FakeTimeline),
            pytest.raises(RateLimitedError),
        ):
            await client.fetch_timeline(self._SINCE, self._UNTIL)

    @pytest.mark.asyncio
    async def test_session_refresh_other_error_maps_to_upstream(
        self, tmp_path: Path
    ) -> None:
        client, api = await _confirmed_client_with_api(tmp_path)
        api.settings.side_effect = _http_error(500)
        with (
            patch("tr_bridge.adapters.pytr.pytr_client.Timeline", _FakeTimeline),
            pytest.raises(TrUpstreamError),
        ):
            await client.fetch_timeline(self._SINCE, self._UNTIL)

    @pytest.mark.asyncio
    async def test_passes_time_window_to_pytr(self, tmp_path: Path) -> None:
        client = await _confirmed_client(tmp_path)
        with patch("tr_bridge.adapters.pytr.pytr_client.Timeline", _FakeTimeline):
            await client.fetch_timeline(self._SINCE, self._UNTIL)
        assert _FakeTimeline.last_kwargs["not_before"] == self._SINCE.timestamp()
        assert _FakeTimeline.last_kwargs["not_after"] == self._UNTIL.timestamp()
        assert _FakeTimeline.last_kwargs["store_event_database"] is False

    @pytest.mark.asyncio
    async def test_upstream_error_when_pytr_fails(self, tmp_path: Path) -> None:
        client = await _confirmed_client(tmp_path)

        class _FailingTimeline(_FakeTimeline):
            async def tl_loop(self) -> None:
                raise RuntimeError("ws boom")

        with (
            patch("tr_bridge.adapters.pytr.pytr_client.Timeline", _FailingTimeline),
            pytest.raises(TrUpstreamError, match="ws boom"),
        ):
            await client.fetch_timeline(self._SINCE, self._UNTIL)

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self, tmp_path: Path) -> None:
        client = await _confirmed_client(tmp_path)

        class _CancelledTimeline(_FakeTimeline):
            async def tl_loop(self) -> None:
                raise asyncio.CancelledError

        with (
            patch("tr_bridge.adapters.pytr.pytr_client.Timeline", _CancelledTimeline),
            pytest.raises(asyncio.CancelledError),
        ):
            await client.fetch_timeline(self._SINCE, self._UNTIL)

    @pytest.mark.asyncio
    async def test_upstream_401_maps_to_session_expired(self, tmp_path: Path) -> None:
        client = await _confirmed_client(tmp_path)
        response = MagicMock()
        response.status_code = 401
        error = requests.exceptions.HTTPError(response=response)

        class _ExpiredTimeline(_FakeTimeline):
            async def tl_loop(self) -> None:
                raise error

        with (
            patch("tr_bridge.adapters.pytr.pytr_client.Timeline", _ExpiredTimeline),
            pytest.raises(SessionExpiredError),
        ):
            await client.fetch_timeline(self._SINCE, self._UNTIL)

    @pytest.mark.asyncio
    async def test_upstream_non_401_maps_to_upstream_error(
        self, tmp_path: Path
    ) -> None:
        client = await _confirmed_client(tmp_path)
        response = MagicMock()
        response.status_code = 500
        error = requests.exceptions.HTTPError(response=response)

        class _FailingTimeline(_FakeTimeline):
            async def tl_loop(self) -> None:
                raise error

        with (
            patch("tr_bridge.adapters.pytr.pytr_client.Timeline", _FailingTimeline),
            pytest.raises(TrUpstreamError),
        ):
            await client.fetch_timeline(self._SINCE, self._UNTIL)
