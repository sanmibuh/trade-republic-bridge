"""FastAPI application entry point."""

import importlib.metadata
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
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
from tr_bridge.errors import ProblemDetail, problem_response
from tr_bridge.instance_registry import InstanceNotFoundError, InstanceRegistry
from tr_bridge.session import CodeRejectedError, InvalidStateError, LoginInProgressError

logger = logging.getLogger(__name__)

_VERSION_FILE = Path(__file__).parent.parent / "VERSION"

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


@app.exception_handler(InstanceNotFoundError)
async def _instance_not_found_handler(
    request: Request, exc: InstanceNotFoundError
) -> Response:
    return problem_response(
        ProblemDetail(
            status=404,
            code="instance_not_found",
            title="Instance not found",
            detail=f"No instance named {exc.name!r} is configured.",
        )
    )


@app.exception_handler(LoginInProgressError)
async def _login_in_progress_handler(
    request: Request, exc: LoginInProgressError
) -> Response:
    return problem_response(
        ProblemDetail(
            status=409,
            code="login_in_progress",
            title="Login already in progress",
            detail=str(exc),
        )
    )


@app.exception_handler(CodeRejectedError)
async def _code_rejected_handler(request: Request, exc: CodeRejectedError) -> Response:
    return problem_response(
        ProblemDetail(
            status=401,
            code="code_rejected",
            title="2FA code rejected",
            detail=str(exc),
        )
    )


@app.exception_handler(InvalidStateError)
async def _invalid_state_handler(request: Request, exc: InvalidStateError) -> Response:
    return problem_response(
        ProblemDetail(
            status=409,
            code="invalid_state",
            title="Operation not valid in current state",
            detail=str(exc),
        )
    )


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


def start() -> None:
    """Start the tr-bridge server on port 8000, bound to 127.0.0.1.

    Intended for local development. The Docker image launches uvicorn directly
    via ``CMD`` with ``--host 0.0.0.0``.
    """
    uvicorn.run("tr_bridge.main:app", host="127.0.0.1", port=8000)
