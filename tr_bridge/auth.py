"""Authentication — X-API-Key validation for FastAPI routes."""

from __future__ import annotations

import secrets

from fastapi import Request


class UnauthorizedException(Exception):
    """Raised when the X-API-Key header is missing or incorrect."""


def check_api_key(request: Request) -> None:
    """Validate the ``X-API-Key`` request header against ``app.state.config``.

    Reads the expected API key from the config cached on ``app.state`` at
    startup — no per-request filesystem I/O.
    Uses ``secrets.compare_digest`` to prevent timing-based side-channel attacks.

    Raises:
        UnauthorizedException: if the header is absent or does not match.
    """
    x_api_key = request.headers.get("X-API-Key")
    config = request.app.state.config
    valid = x_api_key is not None and secrets.compare_digest(x_api_key, config.api_key)
    if not valid:
        raise UnauthorizedException
