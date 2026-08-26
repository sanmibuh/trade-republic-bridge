"""Tests for tr_bridge.main — app wiring and global exception handler."""

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import tr_bridge.main as main_module
from tr_bridge.main import _unhandled_exception_handler, app


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

        mock_run.assert_called_once_with("tr_bridge.main:app", port=8000)
