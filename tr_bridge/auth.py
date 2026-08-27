"""Authentication — X-API-Key dependency for FastAPI routes."""

from __future__ import annotations

import secrets

from fastapi import Depends, Request


class UnauthorizedException(Exception):
    """Raised when the X-API-Key header is missing or incorrect."""


def _check_api_key(request: Request) -> None:
    """FastAPI dependency that validates the ``X-API-Key`` request header.

    Reads the expected API key from ``request.app.state.config``, which is
    populated once during app startup — avoiding per-request filesystem I/O.
    Uses ``secrets.compare_digest`` to prevent timing-based side-channel attacks.

    Raises:
        UnauthorizedException: if the header is absent or does not match.
    """
    x_api_key = request.headers.get("X-API-Key")
    config = request.app.state.config
    valid = x_api_key is not None and secrets.compare_digest(x_api_key, config.api_key)
    if not valid:
        raise UnauthorizedException


require_api_key = Depends(_check_api_key)
