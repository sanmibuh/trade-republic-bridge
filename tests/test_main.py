"""Tests for tr_bridge.main — app wiring and global exception handler."""

from unittest.mock import patch

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

import tr_bridge.main as main_module
from tr_bridge.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


class TestUnhandledExceptionHandler:
    def test_unhandled_exception_returns_500_problem_json(
        self, client: TestClient
    ) -> None:
        # Register a route that always raises to trigger the global handler.
        router = APIRouter()

        @router.get("/_test_crash")
        async def _crash():
            raise RuntimeError("boom")

        app.include_router(router)

        resp = client.get("/_test_crash")

        assert resp.status_code == 500
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert body["code"] == "internal_error"
        assert body["status"] == 500


class TestVersionReading:
    def test_app_version_is_set(self) -> None:
        assert app.version != ""

    def test_read_version_returns_unknown_when_file_missing(
        self, tmp_path: pytest.TempPath
    ) -> None:
        missing = tmp_path / "NO_VERSION"
        original = main_module._VERSION_FILE
        main_module._VERSION_FILE = missing
        try:
            version = main_module._read_version()
        finally:
            main_module._VERSION_FILE = original

        assert version == "unknown"


class TestStart:
    def test_start_calls_uvicorn_run(self) -> None:
        with patch("tr_bridge.main.uvicorn.run") as mock_run:
            main_module.start()

        mock_run.assert_called_once_with(
            "tr_bridge.main:app", host="0.0.0.0", port=8000
        )
