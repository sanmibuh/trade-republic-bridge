"""Authentication — X-API-Key dependency for FastAPI routes."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header

from tr_bridge.config import Config


class UnauthorizedException(Exception):
    """Raised when the X-API-Key header is missing or incorrect."""


def _check_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """FastAPI dependency that validates the ``X-API-Key`` request header.

    Loads the service configuration on each call to obtain the expected key.
    Uses ``secrets.compare_digest`` to prevent timing-based side-channel attacks.

    Raises:
        UnauthorizedException: if the header is absent or does not match.
    """
    config = Config.load()
    valid = x_api_key is not None and secrets.compare_digest(x_api_key, config.api_key)
    if not valid:
        raise UnauthorizedException


require_api_key = Depends(_check_api_key)
