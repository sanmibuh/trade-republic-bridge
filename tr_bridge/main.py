"""FastAPI application entry point."""

import importlib.metadata
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from tr_bridge.auth import UnauthorizedException, check_api_key
from tr_bridge.config import Config
from tr_bridge.errors import DomainError, ProblemDetail, problem_response
from tr_bridge.instance_registry import InstanceRegistry

logger = logging.getLogger(__name__)

_VERSION_FILE = Path(__file__).parent.parent / "VERSION"


class InvalidRequestError(DomainError):
    """Raised when a request carries missing or malformed query parameters."""

    status = 400
    code = "invalid_request"
    title = "Invalid request"


# Paths that bypass API-key authentication.
_PUBLIC_PATHS: frozenset[str] = frozenset({"/health"})


def _read_version() -> str:
    try:
        return _VERSION_FILE.read_text().strip()
    except OSError:
        return "unknown"


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Load and validate configuration on startup; store it on app.state."""
    config = Config.load()
    application.state.config = config
    registry = InstanceRegistry(config)
    application.state.registry = registry
    await registry.resume_all()
    yield


app = FastAPI(
    title="tr-bridge",
    version=_read_version(),
    description="Thin HTTP wrapper around pytr for Trade Republic session management.",
    lifespan=_lifespan,
    # Disable built-in doc UIs — this is an internal API protected by X-API-Key.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def _auth_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Enforce X-API-Key on every request except paths in ``_PUBLIC_PATHS``.

    The path is normalised by stripping a trailing slash before the lookup so
    that liveness probes sent to ``/health/`` are not incorrectly rejected.
    """
    path = request.url.path.rstrip("/") or "/"
    if path not in _PUBLIC_PATHS:
        try:
            check_api_key(request)
        except UnauthorizedException:
            return problem_response(
                ProblemDetail(
                    status=401,
                    code="unauthorized",
                    title="Unauthorized",
                    detail=(
                        "Missing or invalid API key. Provide a valid X-API-Key header."
                    ),
                )
            )
    return await call_next(request)


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException) -> Response:
    try:
        phrase = HTTPStatus(exc.status_code).phrase
    except ValueError:
        phrase = "HTTP Error"
    return problem_response(
        ProblemDetail(
            status=exc.status_code,
            code="http_error",
            title=phrase,
            detail=str(exc.detail),
        )
    )


@app.exception_handler(RequestValidationError)
async def _request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> Response:
    return problem_response(
        ProblemDetail(
            status=422,
            code="validation_error",
            title="Unprocessable Entity",
            detail=str(exc),
        )
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    logger.exception("Unhandled exception on %s %s", request.method, request.url)
    return problem_response(
        ProblemDetail(
            status=500,
            code="internal_error",
            title="Internal server error",
            detail="An unexpected error occurred.",
        )
    )


@app.exception_handler(DomainError)
async def _domain_error_handler(request: Request, exc: DomainError) -> Response:
    """Translate any :class:`DomainError` into its RFC 9457 Problem Details.

    A single data-driven handler covers every domain exception: the HTTP
    ``status``, ``code`` and ``title`` live on the exception class itself, so
    introducing a new domain error requires no change here. Starlette resolves
    this handler for subclasses via the exception's MRO.
    """
    return problem_response(exc.to_problem_detail())


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class DependenciesModel(BaseModel):
    pytr: str
    python: str


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    dependencies: DependenciesModel


class InstancesResponse(BaseModel):
    instances: list[str]


class StatusResponse(BaseModel):
    name: str
    state: str


class LoginStateResponse(BaseModel):
    state: str


class TwoFactorRequest(BaseModel):
    code: str


class TimelineResponse(BaseModel):
    instance: str
    since: str
    until: str
    count: int
    events: list[dict]


# ---------------------------------------------------------------------------
# Public routes (no authentication required)
# ---------------------------------------------------------------------------


@app.get("/health", tags=["ops"])
async def health() -> HealthResponse:
    """Liveness probe — returns 200 OK with no authentication required."""
    return HealthResponse(
        status="ok",
        service="tr-bridge",
        version=_read_version(),
        dependencies=DependenciesModel(
            pytr=importlib.metadata.version("pytr"),
            python=sys.version.split()[0],
        ),
    )


# ---------------------------------------------------------------------------
# Protected routes — register all authenticated endpoints directly on `app`.
# The _auth_middleware above enforces X-API-Key for every path not listed in
# _PUBLIC_PATHS, so any @app.get/post/... route defined here is protected
# automatically without needing a separate router.
# ---------------------------------------------------------------------------


@app.get("/instances", tags=["instances"])
async def get_instances(request: Request) -> InstancesResponse:
    """List all configured instance names — requires X-API-Key."""
    config: Config = request.app.state.config
    return InstancesResponse(instances=config.instance_names)


@app.get("/instances/{name}/status", tags=["instances"])
async def get_instance_status(name: str, request: Request) -> StatusResponse:
    """Return the current login state for *name*.

    Raises ``InstanceNotFoundError`` (404) if the instance is unknown.
    """
    registry: InstanceRegistry = request.app.state.registry
    session = registry.get(name)
    return StatusResponse(name=name, state=session.state)


@app.post("/instances/{name}/login", tags=["instances"])
async def post_instance_login(name: str, request: Request) -> LoginStateResponse:
    """Initiate login for *name* and return the resulting state.

    Raises a problem detail on ``login_in_progress``, ``rate_limited`` or
    ``tr_upstream_error``.
    """
    registry: InstanceRegistry = request.app.state.registry
    session = registry.get(name)
    state = await session.start_login()
    return LoginStateResponse(state=state)


@app.post("/instances/{name}/login/2fa", tags=["instances"])
async def post_instance_login_2fa(
    name: str, body: TwoFactorRequest, request: Request
) -> LoginStateResponse:
    """Submit a 2FA authenticator code to complete a pending login.

    Raises a problem detail on ``code_rejected``, ``no_login_pending``,
    ``rate_limited`` or ``tr_upstream_error``.
    """
    registry: InstanceRegistry = request.app.state.registry
    session = registry.get(name)
    await session.submit_2fa(body.code)
    return LoginStateResponse(state=session.state)


def _parse_iso(value: str, field: str) -> datetime:
    """Parse an ISO-8601 timestamp, normalising naive values to UTC.

    Raises:
        InvalidRequestError: if *value* is not a valid ISO-8601 timestamp.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidRequestError(
            f"Query parameter {field!r} is not a valid ISO-8601 timestamp: {value!r}"
        ) from exc
    # datetime.fromisoformat() also accepts date-only strings (e.g. "2026-08-01"),
    # but the contract requires a full timestamp. Reject values that lack a time
    # component (no 'T'/'t' or space separator between date and time).
    if "T" not in value and "t" not in value and " " not in value.strip():
        raise InvalidRequestError(
            f"Query parameter {field!r} must be a full ISO-8601 timestamp with a "
            f"time component, not a date only: {value!r}"
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_time_window(
    since: str | None, until: str | None
) -> tuple[datetime, datetime]:
    """Resolve the ``[since, until)`` window from raw query strings.

    ``since`` is required; ``until`` defaults to the current time.

    Raises:
        InvalidRequestError: if ``since`` is missing, either value is malformed,
            or ``until`` is not strictly later than ``since``.
    """
    if since is None:
        raise InvalidRequestError("Query parameter 'since' is required.")
    since_dt = _parse_iso(since, "since")
    until_dt = datetime.now(tz=UTC) if until is None else _parse_iso(until, "until")
    if until_dt <= since_dt:
        raise InvalidRequestError(
            f"Query parameter 'until' ({until_dt.isoformat()}) must be later than "
            f"'since' ({since_dt.isoformat()})."
        )
    return since_dt, until_dt


def _to_utc_iso(dt: datetime) -> str:
    """Render *dt* as an ISO-8601 string normalised to UTC with a ``Z`` suffix."""
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


@app.get("/instances/{name}/timeline", tags=["instances"])
async def get_instance_timeline(
    name: str,
    request: Request,
    since: str | None = None,
    until: str | None = None,
) -> TimelineResponse:
    """Return raw pytr timeline events in the ``[since, until)`` window.

    ``since`` is required; ``until`` defaults to now. Malformed timestamps raise
    a ``400 invalid_request``; a missing session raises ``401 session_expired``;
    a pytr failure raises ``502 tr_upstream_error``.

    The echoed ``since``/``until`` are normalised to UTC (``Z`` suffix); raw
    ``events`` are passed through unchanged.
    """
    since_dt, until_dt = _parse_time_window(since, until)
    registry: InstanceRegistry = request.app.state.registry
    session = registry.get(name)
    events = await session.fetch_timeline(since_dt, until_dt)
    return TimelineResponse(
        instance=name,
        since=_to_utc_iso(since_dt),
        until=_to_utc_iso(until_dt),
        count=len(events),
        events=events,
    )


def start() -> None:
    """Start the tr-bridge server on port 8000, bound to 127.0.0.1.

    Intended for local development. The Docker image launches uvicorn directly
    via ``CMD`` with ``--host 0.0.0.0``.
    """
    uvicorn.run("tr_bridge.main:app", host="127.0.0.1", port=8000)
