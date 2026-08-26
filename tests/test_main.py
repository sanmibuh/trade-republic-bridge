"""Tests for tr_bridge.main — app wiring and global exception handler."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

import tr_bridge.main as main_module
from tr_bridge.auth import UnauthorizedException
from tr_bridge.main import (
    _http_exception_handler,
    _request_validation_error_handler,
    _unauthorized_exception_handler,
    _unhandled_exception_handler,
    app,
    protected_router,
)


class TestStartup:
    def test_config_is_loaded_at_startup(self) -> None:
        """App must call Config.load() during lifespan startup."""
        mock_config = MagicMock()
        with patch("tr_bridge.main.Config") as mock_cfg_cls:
            mock_cfg_cls.load.return_value = mock_config
            with TestClient(app):
                mock_cfg_cls.load.assert_called_once()

    def test_startup_fails_fast_on_invalid_config(self) -> None:
        """App must raise on startup when config is invalid."""
        from tr_bridge.config import ConfigError

        with patch("tr_bridge.main.Config") as mock_cfg_cls:
            mock_cfg_cls.load.side_effect = ConfigError("bad config")
            with pytest.raises(ConfigError, match="bad config"), TestClient(app):
                pass


class TestUnhandledExceptionHandler:
    def test_unhandled_exception_returns_500_problem_json(self) -> None:
        # Use a dedicated app to avoid mutating the global app state.
        test_app = FastAPI()
        test_app.add_exception_handler(Exception, _unhandled_exception_handler)

        @test_app.get("/_test_crash")
        async def _crash():
            raise RuntimeError("boom")

        client = TestClient(test_app, raise_server_exceptions=False)
        resp = client.get("/_test_crash")

        assert resp.status_code == 500
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert body["code"] == "internal_error"
        assert body["status"] == 500


def _make_http_exception_app() -> FastAPI:
    """Dedicated app with only the HTTP exception handler — no lifespan."""
    test_app = FastAPI()
    test_app.add_exception_handler(HTTPException, _http_exception_handler)
    return test_app


class TestHttpExceptionHandler:
    def test_http_exception_returns_problem_json(self) -> None:
        # Use a dedicated app to avoid triggering the lifespan (which requires
        # /data/config.yml) and to keep this test focused on handler behaviour.
        test_app = _make_http_exception_app()
        client = TestClient(test_app, raise_server_exceptions=False)
        resp = client.get("/does-not-exist")

        assert resp.status_code == 404
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert body["status"] == 404
        assert "code" in body

    def test_http_exception_detail_is_preserved(self) -> None:
        test_app = _make_http_exception_app()

        @test_app.get("/_test_403")
        async def _forbidden():
            raise HTTPException(status_code=403, detail="Forbidden resource")

        client = TestClient(test_app, raise_server_exceptions=False)
        resp = client.get("/_test_403")

        assert resp.status_code == 403
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert body["status"] == 403
        assert body["detail"] == "Forbidden resource"


class TestRequestValidationErrorHandler:
    def test_validation_error_returns_422_problem_json(self) -> None:
        test_app = FastAPI()

        @test_app.get("/items/{item_id}")
        async def _get_item(item_id: int):
            return {"id": item_id}

        test_app.add_exception_handler(
            RequestValidationError, _request_validation_error_handler
        )
        client = TestClient(test_app, raise_server_exceptions=False)
        resp = client.get("/items/not-an-int")

        assert resp.status_code == 422
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert body["status"] == 422
        assert "code" in body


class TestVersionReading:
    def test_app_version_is_set(self) -> None:
        assert app.version != ""

    def test_read_version_returns_unknown_when_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "NO_VERSION"
        monkeypatch.setattr(main_module, "_VERSION_FILE", missing)

        version = main_module._read_version()

        assert version == "unknown"


class TestHealthEndpoint:
    def test_health_returns_200_without_api_key(self) -> None:
        """/health must be reachable without any authentication."""
        with patch("tr_bridge.main.Config") as mock_cfg_cls:
            mock_cfg_cls.load.return_value = MagicMock()
            with TestClient(app) as client:
                resp = client.get("/health")

        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_health_does_not_require_x_api_key(self) -> None:
        """/health must return 200 even when X-API-Key is absent."""
        with patch("tr_bridge.main.Config") as mock_cfg_cls:
            mock_cfg_cls.load.return_value = MagicMock()
            with TestClient(app) as client:
                resp = client.get("/health", headers={})

        assert resp.status_code == 200


class TestUnauthorizedExceptionHandler:
    def test_unauthorized_exception_returns_401_problem_json(self) -> None:
        test_app = FastAPI()
        test_app.add_exception_handler(
            UnauthorizedException, _unauthorized_exception_handler
        )

        @test_app.get("/_test_auth")
        async def _auth_route():
            raise UnauthorizedException

        client = TestClient(test_app, raise_server_exceptions=False)
        resp = client.get("/_test_auth")

        assert resp.status_code == 401
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert body["status"] == 401
        assert body["code"] == "unauthorized"


class TestAuthWiring:
    """Integration tests that verify auth dependency wiring on the real app.

    A probe endpoint is registered on ``protected_router`` so that we can make
    real requests through the full app stack and confirm:
    - Routes on ``protected_router`` require a valid ``X-API-Key``.
    - ``GET /health`` remains publicly accessible without any key.
    """

    @pytest.fixture(autouse=True)
    def _register_probe(self) -> None:
        """Add a temporary /_probe route to the protected router for this test."""

        @protected_router.get("/_probe")
        async def _probe():
            return {"probed": True}

    def test_protected_route_without_key_returns_401(self) -> None:
        mock_cfg = MagicMock()
        mock_cfg.api_key = "testkey"
        with patch("tr_bridge.auth.Config") as mock_cfg_cls:
            mock_cfg_cls.load.return_value = mock_cfg
            with patch("tr_bridge.main.Config") as mock_main_cls:
                mock_main_cls.load.return_value = mock_cfg
                with TestClient(app) as client:
                    resp = client.get("/_probe")

        assert resp.status_code == 401
        assert resp.headers["content-type"] == "application/problem+json"
        assert resp.json()["code"] == "unauthorized"

    def test_protected_route_with_wrong_key_returns_401(self) -> None:
        mock_cfg = MagicMock()
        mock_cfg.api_key = "testkey"
        with patch("tr_bridge.auth.Config") as mock_cfg_cls:
            mock_cfg_cls.load.return_value = mock_cfg
            with patch("tr_bridge.main.Config") as mock_main_cls:
                mock_main_cls.load.return_value = mock_cfg
                with TestClient(app) as client:
                    resp = client.get("/_probe", headers={"X-API-Key": "wrong"})

        assert resp.status_code == 401

    def test_protected_route_with_valid_key_returns_200(self) -> None:
        mock_cfg = MagicMock()
        mock_cfg.api_key = "testkey"
        with patch("tr_bridge.auth.Config") as mock_cfg_cls:
            mock_cfg_cls.load.return_value = mock_cfg
            with patch("tr_bridge.main.Config") as mock_main_cls:
                mock_main_cls.load.return_value = mock_cfg
                with TestClient(app) as client:
                    resp = client.get("/_probe", headers={"X-API-Key": "testkey"})

        assert resp.status_code == 200

    def test_health_is_public_while_protected_routes_require_key(self) -> None:
        """Health endpoint must be reachable while protected routes are locked."""
        mock_cfg = MagicMock()
        mock_cfg.api_key = "testkey"
        with patch("tr_bridge.auth.Config") as mock_cfg_cls:
            mock_cfg_cls.load.return_value = mock_cfg
            with patch("tr_bridge.main.Config") as mock_main_cls:
                mock_main_cls.load.return_value = mock_cfg
                with TestClient(app) as client:
                    health_resp = client.get("/health")
                    probe_resp = client.get("/_probe")

        assert health_resp.status_code == 200
        assert probe_resp.status_code == 401


class TestStart:
    def test_start_calls_uvicorn_run(self) -> None:
        with patch("tr_bridge.main.uvicorn.run") as mock_run:
            main_module.start()

        mock_run.assert_called_once_with(
            "tr_bridge.main:app", host="127.0.0.1", port=8000
        )
