"""FastAPI application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path

import uvicorn
from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from starlette.exceptions import HTTPException

from tr_bridge.auth import UnauthorizedException, require_api_key
from tr_bridge.config import Config
from tr_bridge.errors import ProblemDetail, problem_response

logger = logging.getLogger(__name__)

_VERSION_FILE = Path(__file__).parent.parent / "VERSION"


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
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Protected router — all routes below require a valid X-API-Key header.
# New protected endpoints must be registered on this router BEFORE the
# include_router() call at the bottom of this module.
# ---------------------------------------------------------------------------

protected_router = APIRouter(dependencies=[require_api_key])

# Register protected routes here:
# @protected_router.get("/example")
# async def example() -> dict: ...

# include_router is called last so that all routes added above are mounted.
app.include_router(protected_router)


def start() -> None:
    """Start the tr-bridge server on port 8000, bound to 127.0.0.1.

    Intended for local development. The Docker image launches uvicorn directly
    via ``CMD`` with ``--host 0.0.0.0``.
    """
    uvicorn.run("tr_bridge.main:app", host="127.0.0.1", port=8000)
