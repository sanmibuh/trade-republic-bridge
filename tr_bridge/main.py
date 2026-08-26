"""FastAPI application entry point."""

import logging
from http import HTTPStatus
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from starlette.exceptions import HTTPException

from tr_bridge.errors import ProblemDetail, problem_response

logger = logging.getLogger(__name__)

_VERSION_FILE = Path(__file__).parent.parent / "VERSION"


def _read_version() -> str:
    try:
        return _VERSION_FILE.read_text().strip()
    except OSError:
        return "unknown"


app = FastAPI(
    title="tr-bridge",
    version=_read_version(),
    description="Thin HTTP wrapper around pytr for Trade Republic session management.",
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


def start() -> None:
    """Start the tr-bridge server on port 8000, bound to 127.0.0.1.

    Intended for local development. The Docker image launches uvicorn directly
    via ``CMD`` with ``--host 0.0.0.0``.
    """
    uvicorn.run("tr_bridge.main:app", host="127.0.0.1", port=8000)
