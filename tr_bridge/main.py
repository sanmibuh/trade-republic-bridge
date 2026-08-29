"""FastAPI application entry point — the composition root.

Wires the layers together: it builds the FastAPI app, registers the web
adapter's handlers and routes, and on startup constructs the per-instance
registry (which injects the pytr secondary adapter into the use case). It owns
no business logic itself.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from tr_bridge.adapters.web.handlers import (
    auth_middleware as _auth_middleware,
    domain_error_handler as _domain_error_handler,
    http_exception_handler as _http_exception_handler,
    register_handlers,
    request_validation_error_handler as _request_validation_error_handler,
    unhandled_exception_handler as _unhandled_exception_handler,
)
from tr_bridge.adapters.web.routes import register_routes
from tr_bridge.config import Config
from tr_bridge.instance_registry import InstanceRegistry

logger = logging.getLogger(__name__)

_VERSION_FILE = Path(__file__).parent.parent / "VERSION"

# Re-exported for tests and external callers that reference the web adapter's
# handlers through the composition root.
__all__ = [
    "_auth_middleware",
    "_domain_error_handler",
    "_http_exception_handler",
    "_request_validation_error_handler",
    "_unhandled_exception_handler",
    "app",
    "start",
]


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
    # The schema and doc UIs are public: access control is the X-API-Key header,
    # not hiding the schema. The API key never appears in the schema.
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

register_handlers(app)
register_routes(app, read_version=_read_version)


def start() -> None:
    """Start the tr-bridge server on port 8000, bound to 127.0.0.1.

    Intended for local development. The Docker image launches uvicorn directly
    via ``CMD`` with ``--host 0.0.0.0``.
    """
    uvicorn.run("tr_bridge.main:app", host="127.0.0.1", port=8000)
