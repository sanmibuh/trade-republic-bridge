"""Tests for tr_bridge.main — app wiring and global exception handler."""

import importlib.metadata
import sys
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException
from starlette.requests import Request

import tr_bridge.main as main_module
from tr_bridge.main import (
    _auth_middleware,
    _http_exception_handler,
    _request_validation_error_handler,
    _unhandled_exception_handler,
    app,
)
from tr_bridge.session import (
    CodeRejectedError,
    LoginInProgressError,
    NoLoginPendingError,
    RateLimitedError,
    SessionExpiredError,
    SessionState,
    TrUpstreamError,
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
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "tr-bridge"
        assert "version" in body
        assert "pytr" in body["dependencies"]
        assert "python" in body["dependencies"]

    def test_health_returns_correct_version_values(self) -> None:
        """/health must return the actual service, pytr and python versions."""
        with patch("tr_bridge.main.Config") as mock_cfg_cls:
            mock_cfg_cls.load.return_value = MagicMock()
            with TestClient(app) as client:
                resp = client.get("/health")

        body = resp.json()
        expected_version = (
            (Path(__file__).parent.parent / "VERSION").read_text().strip()
        )
        assert body["version"] == expected_version
        assert body["dependencies"]["pytr"] == importlib.metadata.version("pytr")
        assert body["dependencies"]["python"] == sys.version.split()[0]

    def test_health_does_not_require_x_api_key(self) -> None:
        """/health must return 200 even when X-API-Key is absent."""
        with patch("tr_bridge.main.Config") as mock_cfg_cls:
            mock_cfg_cls.load.return_value = MagicMock()
            with TestClient(app) as client:
                resp = client.get("/health", headers={})

        assert resp.status_code == 200

    def test_health_trailing_slash_is_not_rejected(self) -> None:
        """/health/ with trailing slash must not be blocked by auth middleware."""
        with patch("tr_bridge.main.Config") as mock_cfg_cls:
            mock_cfg_cls.load.return_value = MagicMock()
            with TestClient(app, follow_redirects=False) as client:
                resp = client.get("/health/")

        # Either a redirect (3xx) to /health or a 200 are acceptable;
        # a 401 from the middleware is not.
        assert resp.status_code != 401


class TestAuthMiddleware:
    """Integration tests that verify the HTTP auth middleware wiring.

    A fresh app mirrors ``main.py``'s structure: middleware enforces X-API-Key
    on all paths except ``/health``.  Using a fresh app avoids mutating global
    state and keeps each test fully isolated.
    """

    def _make_app(self, api_key: str) -> FastAPI:
        """Return a wired app with ``app.state.config`` pre-populated."""

        test_app = FastAPI()
        mock_cfg = MagicMock()
        mock_cfg.api_key = api_key
        test_app.state.config = mock_cfg

        @test_app.middleware("http")
        async def _auth(request: Request, call_next):
            return await _auth_middleware(request, call_next)

        @test_app.get("/health")
        async def _health():
            return {"status": "ok"}

        @test_app.get("/protected")
        async def _protected():
            return {"ok": True}

        return test_app

    def test_protected_route_without_key_returns_401(self) -> None:
        client = TestClient(self._make_app("testkey"), raise_server_exceptions=False)
        resp = client.get("/protected")

        assert resp.status_code == 401
        assert resp.headers["content-type"] == "application/problem+json"
        assert resp.json()["code"] == "unauthorized"

    def test_protected_route_with_wrong_key_returns_401(self) -> None:
        client = TestClient(self._make_app("testkey"), raise_server_exceptions=False)
        resp = client.get("/protected", headers={"X-API-Key": "wrong"})

        assert resp.status_code == 401

    def test_protected_route_with_valid_key_returns_200(self) -> None:
        client = TestClient(self._make_app("testkey"), raise_server_exceptions=False)
        resp = client.get("/protected", headers={"X-API-Key": "testkey"})

        assert resp.status_code == 200

    def test_health_is_public_while_protected_routes_require_key(self) -> None:
        """``/health`` must be reachable without a key; other routes must not."""
        client = TestClient(self._make_app("testkey"), raise_server_exceptions=False)

        health_resp = client.get("/health")
        protected_resp = client.get("/protected")

        assert health_resp.status_code == 200
        assert protected_resp.status_code == 401


class TestStart:
    def test_start_calls_uvicorn_run(self) -> None:
        with patch("tr_bridge.main.uvicorn.run") as mock_run:
            main_module.start()

        mock_run.assert_called_once_with(
            "tr_bridge.main:app", host="127.0.0.1", port=8000
        )


def _make_mock_config(api_key: str = "secret", instance_names: list[str] | None = None):
    mock_cfg = MagicMock()
    mock_cfg.api_key = api_key
    mock_cfg.instance_names = instance_names or []
    return mock_cfg


class TestInstancesEndpoint:
    """Tests for GET /instances — protected endpoint listing configured instances."""

    def test_instances_returns_list_with_valid_key(self) -> None:
        mock_cfg = _make_mock_config(api_key="mykey", instance_names=["alice", "bob"])
        with patch("tr_bridge.main.Config") as mock_cls:
            mock_cls.load.return_value = mock_cfg
            with TestClient(app) as client:
                resp = client.get("/instances", headers={"X-API-Key": "mykey"})

        assert resp.status_code == 200
        body = resp.json()
        assert body == {"instances": ["alice", "bob"]}

    def test_instances_returns_empty_list_when_no_instances(self) -> None:
        mock_cfg = _make_mock_config(api_key="mykey", instance_names=[])
        with patch("tr_bridge.main.Config") as mock_cls:
            mock_cls.load.return_value = mock_cfg
            with TestClient(app) as client:
                resp = client.get("/instances", headers={"X-API-Key": "mykey"})

        assert resp.status_code == 200
        assert resp.json() == {"instances": []}

    def test_instances_without_api_key_returns_401(self) -> None:
        mock_cfg = _make_mock_config(api_key="mykey", instance_names=["alice"])
        with patch("tr_bridge.main.Config") as mock_cls:
            mock_cls.load.return_value = mock_cfg
            with TestClient(app) as client:
                resp = client.get("/instances")

        assert resp.status_code == 401
        assert resp.headers["content-type"] == "application/problem+json"
        assert resp.json()["code"] == "unauthorized"

    def test_instances_with_wrong_api_key_returns_401(self) -> None:
        mock_cfg = _make_mock_config(api_key="mykey", instance_names=["alice"])
        with patch("tr_bridge.main.Config") as mock_cls:
            mock_cls.load.return_value = mock_cfg
            with TestClient(app) as client:
                resp = client.get("/instances", headers={"X-API-Key": "wrong"})

        assert resp.status_code == 401


class TestDomainExceptionHandlers:
    """Verify that domain errors are translated to RFC 9457 responses."""

    def _make_app_with_handlers(self) -> "FastAPI":
        from fastapi import FastAPI

        from tr_bridge.errors import DomainError
        from tr_bridge.instance_registry import InstanceNotFoundError
        from tr_bridge.main import _domain_error_handler
        from tr_bridge.session import (
            CodeRejectedError,
            InvalidStateError,
            LoginInProgressError,
            SessionState,
        )

        test_app = FastAPI()
        # A single generic handler covers every domain error via the MRO.
        test_app.add_exception_handler(DomainError, _domain_error_handler)

        @test_app.get("/_not_found")
        async def _raise_not_found():
            raise InstanceNotFoundError("user1")

        @test_app.get("/_in_progress")
        async def _raise_in_progress():
            raise LoginInProgressError("already running")

        @test_app.get("/_code_rejected")
        async def _raise_code_rejected():
            raise CodeRejectedError("bad code")

        @test_app.get("/_invalid_state")
        async def _raise_invalid_state():
            raise InvalidStateError(SessionState.idle)

        return test_app

    def test_instance_not_found_returns_404_problem_json(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(
            self._make_app_with_handlers(), raise_server_exceptions=False
        )
        resp = client.get("/_not_found")
        assert resp.status_code == 404
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert body["code"] == "instance_not_found"
        assert "user1" in body["detail"]

    def test_login_in_progress_returns_409_problem_json(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(
            self._make_app_with_handlers(), raise_server_exceptions=False
        )
        resp = client.get("/_in_progress")
        assert resp.status_code == 409
        assert resp.headers["content-type"] == "application/problem+json"
        assert resp.json()["code"] == "login_in_progress"

    def test_code_rejected_returns_401_problem_json(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(
            self._make_app_with_handlers(), raise_server_exceptions=False
        )
        resp = client.get("/_code_rejected")
        assert resp.status_code == 401
        assert resp.headers["content-type"] == "application/problem+json"
        assert resp.json()["code"] == "code_rejected"

    def test_invalid_state_returns_409_problem_json(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(
            self._make_app_with_handlers(), raise_server_exceptions=False
        )
        resp = client.get("/_invalid_state")
        assert resp.status_code == 409
        assert resp.headers["content-type"] == "application/problem+json"
        assert resp.json()["code"] == "invalid_state"


class _FakeSession:
    """Minimal stand-in for InstanceSession driving the login endpoints."""

    def __init__(
        self,
        state: SessionState = SessionState.idle,
        start_login: object | None = None,
        submit_2fa: object | None = None,
        fetch_timeline: object | None = None,
    ) -> None:
        self.state = state
        self.start_login = start_login or AsyncMock(return_value=state)
        self.submit_2fa = submit_2fa or AsyncMock(return_value=None)
        self.fetch_timeline = fetch_timeline or AsyncMock(return_value=[])


def _client_with_session(session: object | None, cleanups: list) -> TestClient:
    """Build a TestClient against ``app`` with a registry returning *session*.

    Passing ``session=None`` makes ``registry.get`` raise
    ``InstanceNotFoundError``. Registers teardown callbacks in *cleanups* so
    patches and the client context are released after the test.
    """
    from tr_bridge.instance_registry import InstanceNotFoundError

    registry = MagicMock()
    if session is None:
        registry.get.side_effect = InstanceNotFoundError("ghost")
    else:
        registry.get.return_value = session

    mock_registry_cls = MagicMock()
    instance = mock_registry_cls.return_value
    instance.get = registry.get
    instance.resume_all = AsyncMock(return_value=None)

    mock_cfg = _make_mock_config(api_key="mykey")
    ctx_config = patch("tr_bridge.main.Config")
    ctx_registry = patch("tr_bridge.main.InstanceRegistry", mock_registry_cls)
    cfg_cls = ctx_config.start()
    cfg_cls.load.return_value = mock_cfg
    ctx_registry.start()
    cleanups.append(ctx_config.stop)
    cleanups.append(ctx_registry.stop)

    client = TestClient(app)
    client.__enter__()
    cleanups.append(lambda: client.__exit__(None, None, None))
    return client


@pytest.fixture
def make_client():
    """Yield a factory building login-endpoint clients with automatic teardown."""
    cleanups: list = []

    def _factory(session: object | None) -> TestClient:
        return _client_with_session(session, cleanups)

    yield _factory
    for teardown in reversed(cleanups):
        teardown()


class TestStatusEndpoint:
    def test_status_returns_name_and_state(self, make_client) -> None:
        session = _FakeSession(state=SessionState.confirmed)
        client = make_client(session)
        resp = client.get("/instances/user1/status", headers={"X-API-Key": "mykey"})
        assert resp.status_code == 200
        assert resp.json() == {"name": "user1", "state": "confirmed"}

    def test_status_unknown_instance_returns_404(self, make_client) -> None:
        client = make_client(None)
        resp = client.get("/instances/ghost/status", headers={"X-API-Key": "mykey"})
        assert resp.status_code == 404
        assert resp.json()["code"] == "instance_not_found"

    def test_status_requires_api_key(self, make_client) -> None:
        session = _FakeSession()
        client = make_client(session)
        resp = client.get("/instances/user1/status")
        assert resp.status_code == 401


class TestLoginEndpoint:
    def test_login_returns_state(self, make_client) -> None:
        session = _FakeSession(
            start_login=AsyncMock(return_value=SessionState.authenticator)
        )
        client = make_client(session)
        resp = client.post("/instances/user1/login", headers={"X-API-Key": "mykey"})
        assert resp.status_code == 200
        assert resp.json() == {"state": "authenticator"}

    def test_login_unknown_instance_returns_404(self, make_client) -> None:
        client = make_client(None)
        resp = client.post("/instances/ghost/login", headers={"X-API-Key": "mykey"})
        assert resp.status_code == 404

    def test_login_in_progress_returns_409(self, make_client) -> None:
        session = _FakeSession(
            start_login=AsyncMock(side_effect=LoginInProgressError("busy"))
        )
        client = make_client(session)
        resp = client.post("/instances/user1/login", headers={"X-API-Key": "mykey"})
        assert resp.status_code == 409
        assert resp.json()["code"] == "login_in_progress"

    def test_login_rate_limited_returns_429(self, make_client) -> None:
        session = _FakeSession(
            start_login=AsyncMock(side_effect=RateLimitedError("slow down"))
        )
        client = make_client(session)
        resp = client.post("/instances/user1/login", headers={"X-API-Key": "mykey"})
        assert resp.status_code == 429
        assert resp.json()["code"] == "rate_limited"

    def test_login_upstream_error_returns_502(self, make_client) -> None:
        session = _FakeSession(
            start_login=AsyncMock(side_effect=TrUpstreamError("boom"))
        )
        client = make_client(session)
        resp = client.post("/instances/user1/login", headers={"X-API-Key": "mykey"})
        assert resp.status_code == 502
        assert resp.json()["code"] == "tr_upstream_error"


class TestLogin2faEndpoint:
    def test_2fa_returns_confirmed_state(self, make_client) -> None:
        session = _FakeSession(state=SessionState.confirmed)
        client = make_client(session)
        resp = client.post(
            "/instances/user1/login/2fa",
            headers={"X-API-Key": "mykey"},
            json={"code": "123456"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"state": "confirmed"}
        session.submit_2fa.assert_awaited_once_with("123456")

    def test_2fa_missing_code_returns_422(self, make_client) -> None:
        session = _FakeSession()
        client = make_client(session)
        resp = client.post(
            "/instances/user1/login/2fa",
            headers={"X-API-Key": "mykey"},
            json={},
        )
        assert resp.status_code == 422

    def test_2fa_code_rejected_returns_401(self, make_client) -> None:
        session = _FakeSession(
            submit_2fa=AsyncMock(side_effect=CodeRejectedError("bad code"))
        )
        client = make_client(session)
        resp = client.post(
            "/instances/user1/login/2fa",
            headers={"X-API-Key": "mykey"},
            json={"code": "000000"},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "code_rejected"

    def test_2fa_no_login_pending_returns_409(self, make_client) -> None:
        session = _FakeSession(
            submit_2fa=AsyncMock(side_effect=NoLoginPendingError(SessionState.idle))
        )
        client = make_client(session)
        resp = client.post(
            "/instances/user1/login/2fa",
            headers={"X-API-Key": "mykey"},
            json={"code": "123456"},
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "no_login_pending"

    def test_2fa_unknown_instance_returns_404(self, make_client) -> None:
        client = make_client(None)
        resp = client.post(
            "/instances/ghost/login/2fa",
            headers={"X-API-Key": "mykey"},
            json={"code": "123456"},
        )
        assert resp.status_code == 404

    def test_2fa_rate_limited_returns_429(self, make_client) -> None:
        session = _FakeSession(
            submit_2fa=AsyncMock(side_effect=RateLimitedError("slow down"))
        )
        client = make_client(session)
        resp = client.post(
            "/instances/user1/login/2fa",
            headers={"X-API-Key": "mykey"},
            json={"code": "123456"},
        )
        assert resp.status_code == 429
        assert resp.json()["code"] == "rate_limited"

    def test_2fa_upstream_error_returns_502(self, make_client) -> None:
        session = _FakeSession(
            submit_2fa=AsyncMock(side_effect=TrUpstreamError("boom"))
        )
        client = make_client(session)
        resp = client.post(
            "/instances/user1/login/2fa",
            headers={"X-API-Key": "mykey"},
            json={"code": "123456"},
        )
        assert resp.status_code == 502
        assert resp.json()["code"] == "tr_upstream_error"


class TestTimelineEndpoint:
    _EVENTS: ClassVar[list[dict]] = [
        {"id": "e1", "timestamp": "2026-08-03T10:12:04.000+0000"}
    ]

    def test_timeline_happy_path(self, make_client) -> None:
        session = _FakeSession(
            state=SessionState.confirmed,
            fetch_timeline=AsyncMock(return_value=self._EVENTS),
        )
        client = make_client(session)
        resp = client.get(
            "/instances/user1/timeline",
            headers={"X-API-Key": "mykey"},
            params={"since": "2026-08-01T00:00:00Z", "until": "2026-08-10T00:00:00Z"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["instance"] == "user1"
        assert body["since"] == "2026-08-01T00:00:00Z"
        assert body["until"] == "2026-08-10T00:00:00Z"
        assert body["count"] == 1
        assert body["events"] == self._EVENTS
        session.fetch_timeline.assert_awaited_once()

    def test_timeline_normalizes_non_utc_offset_to_utc(self, make_client) -> None:
        """Non-UTC input offsets are converted to UTC (Z) in the response."""
        session = _FakeSession(
            state=SessionState.confirmed,
            fetch_timeline=AsyncMock(return_value=[]),
        )
        client = make_client(session)
        resp = client.get(
            "/instances/user1/timeline",
            headers={"X-API-Key": "mykey"},
            params={
                "since": "2026-08-01T02:00:00+02:00",
                "until": "2026-08-10T05:30:00+05:30",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["since"] == "2026-08-01T00:00:00Z"
        assert body["until"] == "2026-08-10T00:00:00Z"

    def test_timeline_until_defaults_to_now(self, make_client) -> None:
        session = _FakeSession(
            state=SessionState.confirmed,
            fetch_timeline=AsyncMock(return_value=[]),
        )
        client = make_client(session)
        resp = client.get(
            "/instances/user1/timeline",
            headers={"X-API-Key": "mykey"},
            params={"since": "2026-08-01T00:00:00Z"},
        )
        assert resp.status_code == 200
        assert resp.json()["until"] != ""

    def test_timeline_missing_since_returns_400(self, make_client) -> None:
        session = _FakeSession(state=SessionState.confirmed)
        client = make_client(session)
        resp = client.get("/instances/user1/timeline", headers={"X-API-Key": "mykey"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_request"

    def test_timeline_bad_iso_returns_400(self, make_client) -> None:
        session = _FakeSession(state=SessionState.confirmed)
        client = make_client(session)
        resp = client.get(
            "/instances/user1/timeline",
            headers={"X-API-Key": "mykey"},
            params={"since": "not-a-date"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_request"

    def test_timeline_date_only_since_returns_400(self, make_client) -> None:
        """A date-only 'since' is valid ISO-8601 but not a timestamp → 400."""
        session = _FakeSession(state=SessionState.confirmed)
        client = make_client(session)
        resp = client.get(
            "/instances/user1/timeline",
            headers={"X-API-Key": "mykey"},
            params={"since": "2026-08-01"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_request"

    def test_timeline_bad_until_returns_400(self, make_client) -> None:
        session = _FakeSession(state=SessionState.confirmed)
        client = make_client(session)
        resp = client.get(
            "/instances/user1/timeline",
            headers={"X-API-Key": "mykey"},
            params={"since": "2026-08-01T00:00:00Z", "until": "not-a-date"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_request"

    def test_timeline_date_only_until_returns_400(self, make_client) -> None:
        session = _FakeSession(state=SessionState.confirmed)
        client = make_client(session)
        resp = client.get(
            "/instances/user1/timeline",
            headers={"X-API-Key": "mykey"},
            params={"since": "2026-08-01T00:00:00Z", "until": "2026-08-10"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_request"

    def test_timeline_accepts_z_suffix_utc_timestamps(self, make_client) -> None:
        """RFC 3339 'Z' UTC suffix must be accepted (Python >= 3.11)."""
        session = _FakeSession(
            state=SessionState.confirmed,
            fetch_timeline=AsyncMock(return_value=[]),
        )
        client = make_client(session)
        resp = client.get(
            "/instances/user1/timeline",
            headers={"X-API-Key": "mykey"},
            params={"since": "2026-08-01T00:00:00Z", "until": "2026-08-10T00:00:00Z"},
        )
        assert resp.status_code == 200

    def test_timeline_until_not_after_since_returns_400(self, make_client) -> None:
        session = _FakeSession(state=SessionState.confirmed)
        client = make_client(session)
        resp = client.get(
            "/instances/user1/timeline",
            headers={"X-API-Key": "mykey"},
            params={"since": "2026-08-10T00:00:00Z", "until": "2026-08-01T00:00:00Z"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_request"

    def test_timeline_until_equal_to_since_returns_400(self, make_client) -> None:
        session = _FakeSession(state=SessionState.confirmed)
        client = make_client(session)
        resp = client.get(
            "/instances/user1/timeline",
            headers={"X-API-Key": "mykey"},
            params={"since": "2026-08-01T00:00:00Z", "until": "2026-08-01T00:00:00Z"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_request"

    def test_timeline_expired_session_returns_401(self, make_client) -> None:
        session = _FakeSession(
            fetch_timeline=AsyncMock(side_effect=SessionExpiredError("gone"))
        )
        client = make_client(session)
        resp = client.get(
            "/instances/user1/timeline",
            headers={"X-API-Key": "mykey"},
            params={"since": "2026-08-01T00:00:00Z"},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "session_expired"

    def test_timeline_upstream_error_returns_502(self, make_client) -> None:
        session = _FakeSession(
            fetch_timeline=AsyncMock(side_effect=TrUpstreamError("boom"))
        )
        client = make_client(session)
        resp = client.get(
            "/instances/user1/timeline",
            headers={"X-API-Key": "mykey"},
            params={"since": "2026-08-01T00:00:00Z"},
        )
        assert resp.status_code == 502
        assert resp.json()["code"] == "tr_upstream_error"

    def test_timeline_unknown_instance_returns_404(self, make_client) -> None:
        client = make_client(None)
        resp = client.get(
            "/instances/ghost/timeline",
            headers={"X-API-Key": "mykey"},
            params={"since": "2026-08-01T00:00:00Z"},
        )
        assert resp.status_code == 404

    def test_timeline_requires_api_key(self, make_client) -> None:
        session = _FakeSession(state=SessionState.confirmed)
        client = make_client(session)
        resp = client.get(
            "/instances/user1/timeline",
            params={"since": "2026-08-01T00:00:00Z"},
        )
        assert resp.status_code == 401
