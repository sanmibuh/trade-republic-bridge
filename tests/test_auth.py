"""Tests for tr_bridge.auth — check_api_key validation."""

from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

from tr_bridge.auth import UnauthorizedException, check_api_key


def _make_request(api_key: str | None, config_key: str) -> Request:
    """Build a Starlette Request with the given X-API-Key header and app config."""
    mock_app = MagicMock()
    mock_app.state.config.api_key = config_key
    headers = []
    if api_key is not None:
        headers = [(b"x-api-key", api_key.encode())]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": headers,
        "app": mock_app,
    }
    return Request(scope)


class TestCheckApiKey:
    def test_valid_key_does_not_raise(self) -> None:
        """A matching X-API-Key header must not raise."""
        request = _make_request(api_key="secret", config_key="secret")
        check_api_key(request)  # must not raise

    def test_missing_key_raises_unauthorized(self) -> None:
        """An absent X-API-Key header must raise UnauthorizedException."""
        request = _make_request(api_key=None, config_key="secret")
        with pytest.raises(UnauthorizedException):
            check_api_key(request)

    def test_wrong_key_raises_unauthorized(self) -> None:
        """An incorrect X-API-Key header must raise UnauthorizedException."""
        request = _make_request(api_key="wrong", config_key="secret")
        with pytest.raises(UnauthorizedException):
            check_api_key(request)
