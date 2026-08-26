"""Tests for tr_bridge.auth — X-API-Key dependency."""

from unittest.mock import MagicMock, patch

from fastapi import Depends, FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient
from starlette.requests import Request

from tr_bridge.auth import UnauthorizedException, _check_api_key
from tr_bridge.errors import ProblemDetail, problem_response


def _make_test_app() -> FastAPI:
    """Build a minimal app with the auth dependency and a protected route."""
    test_app = FastAPI()

    @test_app.exception_handler(UnauthorizedException)
    async def _handler(request: Request, exc: UnauthorizedException) -> Response:
        return problem_response(
            ProblemDetail(
                status=401,
                code="unauthorized",
                title="Unauthorized",
                detail="Missing or invalid API key.",
            )
        )

    @test_app.get("/protected", dependencies=[Depends(_check_api_key)])
    async def _protected():
        return {"ok": True}

    return test_app


def _mock_config(api_key: str) -> MagicMock:
    cfg = MagicMock()
    cfg.api_key = api_key
    return cfg


class TestRequireApiKey:
    def test_valid_key_passes(self) -> None:
        """A request with the correct X-API-Key header must succeed."""
        test_app = _make_test_app()
        client = TestClient(test_app, raise_server_exceptions=False)

        with patch("tr_bridge.auth.Config") as mock_cfg_cls:
            mock_cfg_cls.load.return_value = _mock_config("secret")
            resp = client.get("/protected", headers={"X-API-Key": "secret"})

        assert resp.status_code == 200

    def test_missing_key_returns_401(self) -> None:
        """A request without the X-API-Key header must return 401."""
        test_app = _make_test_app()
        client = TestClient(test_app, raise_server_exceptions=False)

        with patch("tr_bridge.auth.Config") as mock_cfg_cls:
            mock_cfg_cls.load.return_value = _mock_config("secret")
            resp = client.get("/protected")

        assert resp.status_code == 401
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert body["status"] == 401
        assert body["code"] == "unauthorized"

    def test_wrong_key_returns_401(self) -> None:
        """A request with an incorrect X-API-Key header must return 401."""
        test_app = _make_test_app()
        client = TestClient(test_app, raise_server_exceptions=False)

        with patch("tr_bridge.auth.Config") as mock_cfg_cls:
            mock_cfg_cls.load.return_value = _mock_config("secret")
            resp = client.get("/protected", headers={"X-API-Key": "wrong"})

        assert resp.status_code == 401
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert body["status"] == 401
        assert body["code"] == "unauthorized"
