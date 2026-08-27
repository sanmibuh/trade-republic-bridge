"""FastAPI application entry point."""

import importlib.metadata
import logging
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from starlette.exceptions import HTTPException

from tr_bridge.auth import UnauthorizedException, check_api_key
from tr_bridge.config import Config
from tr_bridge.errors import ProblemDetail, problem_response

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
    application.state.config = Config.load()
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
    request: Request, call_next: Callable[[Request], Response]
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


@app.exception_handler(UnauthorizedException)
async def _unauthorized_exception_handler(
    request: Request, exc: UnauthorizedException
) -> Response:
    return problem_response(
        ProblemDetail(
            status=401,
            code="unauthorized",
            title="Unauthorized",
            detail="Missing or invalid API key. Provide a valid X-API-Key header.",
        )
    )


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


# ---------------------------------------------------------------------------
# Public routes (no authentication required)
# ---------------------------------------------------------------------------


@app.get("/health", tags=["ops"])
async def health() -> dict:
    """Liveness probe — returns 200 OK with no authentication required."""
    return {
        "status": "ok",
        "service": "tr-bridge",
        "version": _read_version(),
        "dependencies": {
            "pytr": importlib.metadata.version("pytr"),
            "python": sys.version.split()[0],
        },
    }


# ---------------------------------------------------------------------------
# Protected routes — register all authenticated endpoints directly on `app`.
# The _auth_middleware above enforces X-API-Key for every path not listed in
# _PUBLIC_PATHS, so any @app.get/post/... route defined here is protected
# automatically without needing a separate router.
# ---------------------------------------------------------------------------


def start() -> None:
    """Start the tr-bridge server on port 8000, bound to 127.0.0.1.

    Intended for local development. The Docker image launches uvicorn directly
    via ``CMD`` with ``--host 0.0.0.0``.
    """
    uvicorn.run("tr_bridge.main:app", host="127.0.0.1", port=8000)
