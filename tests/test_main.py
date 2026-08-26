"""Tests for tr_bridge.main — app wiring and global exception handler."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

import tr_bridge.main as main_module
from tr_bridge.main import (
    _http_exception_handler,
    _request_validation_error_handler,
    _unhandled_exception_handler,
    app,
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


class TestHttpExceptionHandler:
    def test_http_exception_returns_problem_json(self) -> None:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/does-not-exist")

        assert resp.status_code == 404
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert body["status"] == 404
        assert "code" in body

    def test_http_exception_detail_is_preserved(self) -> None:
        test_app = FastAPI()

        @test_app.get("/_test_403")
        async def _forbidden():
            raise HTTPException(status_code=403, detail="Forbidden resource")

        test_app.add_exception_handler(HTTPException, _http_exception_handler)
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


class TestStart:
    def test_start_calls_uvicorn_run(self) -> None:
        with patch("tr_bridge.main.uvicorn.run") as mock_run:
            main_module.start()

        mock_run.assert_called_once_with(
            "tr_bridge.main:app", host="127.0.0.1", port=8000
        )
