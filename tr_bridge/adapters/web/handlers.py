"""Web adapter: authentication middleware and RFC 9457 exception handlers.

This is the primary adapter's error boundary. The auth middleware enforces
``X-API-Key`` on every non-public path; the exception handlers translate domain
and framework errors into ``application/problem+json`` responses. A single
data-driven handler covers every :class:`DomainError` via the exception's MRO,
so introducing a new domain error never requires touching this module.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from starlette.exceptions import HTTPException

from tr_bridge.auth import UnauthorizedException, check_api_key
from tr_bridge.errors import DomainError, ProblemDetail, problem_response

logger = logging.getLogger(__name__)

# Paths that bypass API-key authentication: the liveness probe and the public
# OpenAPI schema / documentation UIs (the schema carries no secrets). The
# Swagger UI's oauth2-redirect subpath is whitelisted explicitly rather than via
# a prefix, to avoid accidentally exposing unrelated ``/docs*`` routes.
_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
)


async def auth_middleware(
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


def http_exception_handler(request: Request, exc: HTTPException) -> Response:
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


def request_validation_error_handler(
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


def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    logger.exception("Unhandled exception on %s %s", request.method, request.url)
    return problem_response(
        ProblemDetail(
            status=500,
            code="internal_error",
            title="Internal server error",
            detail="An unexpected error occurred.",
        )
    )


def domain_error_handler(request: Request, exc: DomainError) -> Response:
    """Translate any :class:`DomainError` into its RFC 9457 Problem Details.

    A single data-driven handler covers every domain exception: the HTTP
    ``status``, ``code`` and ``title`` live on the exception class itself, so
    introducing a new domain error requires no change here. Starlette resolves
    this handler for subclasses via the exception's MRO.
    """
    return problem_response(exc.to_problem_detail())


def register_handlers(app: FastAPI) -> None:
    """Wire the auth middleware and all exception handlers onto *app*."""
    app.middleware("http")(auth_middleware)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.add_exception_handler(DomainError, domain_error_handler)
