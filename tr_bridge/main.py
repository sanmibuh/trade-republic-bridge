"""FastAPI application entry point."""

import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response

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
    """Entry point for the ``tr-bridge`` console script."""
    uvicorn.run("tr_bridge.main:app", host="0.0.0.0", port=8000)
