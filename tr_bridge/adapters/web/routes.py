"""Web adapter: FastAPI route definitions.

The primary adapter. Routes are thin: they parse/echo the HTTP contract and
delegate to the use case (via the per-instance :class:`InstanceRegistry`).
Business orchestration and pytr access live behind the application and secondary
adapter layers respectively.

Most routes defined here are protected: the auth middleware registered by
``register_handlers`` enforces ``X-API-Key`` for any path not in the public set.
The public exceptions are the base URL (``GET /``, which redirects to ``/docs``),
the ``/health`` liveness probe, and the documentation endpoints.
"""

from __future__ import annotations

import importlib.metadata
import sys
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from tr_bridge.adapters.web.schemas import (
    DependenciesModel,
    HealthResponse,
    InstancesResponse,
    LoginStateResponse,
    StatusResponse,
    TimelineResponse,
    TwoFactorRequest,
)
from tr_bridge.config import Config
from tr_bridge.instance_registry import InstanceRegistry
from tr_bridge.timewindow import parse_window, to_utc_iso


def register_routes(app: FastAPI, *, read_version: Callable[[], str]) -> None:
    """Register all HTTP routes on *app*.

    ``read_version`` is injected by the composition root so the health route can
    report the running service version without this module owning that concern.
    """

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        """Redirect the base URL to the Swagger UI for a friendly landing page."""
        return RedirectResponse(url="/docs")

    @app.get("/health", tags=["ops"])
    async def health() -> HealthResponse:
        """Liveness probe — returns 200 OK with no authentication required."""
        return HealthResponse(
            status="ok",
            service="tr-bridge",
            version=read_version(),
            dependencies=DependenciesModel(
                pytr=importlib.metadata.version("pytr"),
                python=sys.version.split()[0],
            ),
        )

    @app.get("/instances", tags=["instances"])
    async def get_instances(request: Request) -> InstancesResponse:
        """List all configured instance names — requires X-API-Key."""
        config: Config = request.app.state.config
        return InstancesResponse(instances=config.instance_names)

    @app.get("/instances/{name}/status", tags=["instances"])
    async def get_instance_status(name: str, request: Request) -> StatusResponse:
        """Return the current login state for *name*.

        Raises ``InstanceNotFoundError`` (404) if the instance is unknown.
        """
        registry: InstanceRegistry = request.app.state.registry
        session = registry.get(name)
        return StatusResponse(name=name, state=session.state)

    @app.post("/instances/{name}/login", tags=["instances"])
    async def post_instance_login(name: str, request: Request) -> LoginStateResponse:
        """Initiate login for *name* and return the resulting state."""
        registry: InstanceRegistry = request.app.state.registry
        session = registry.get(name)
        state = await session.start_login()
        return LoginStateResponse(state=state)

    @app.post("/instances/{name}/login/2fa", tags=["instances"])
    async def post_instance_login_2fa(
        name: str, body: TwoFactorRequest, request: Request
    ) -> LoginStateResponse:
        """Submit a 2FA authenticator code to complete a pending login."""
        registry: InstanceRegistry = request.app.state.registry
        session = registry.get(name)
        await session.submit_2fa(body.code)
        return LoginStateResponse(state=session.state)

    @app.get(
        "/instances/{name}/timeline",
        tags=["instances"],
        response_model_exclude_unset=True,
    )
    async def get_instance_timeline(
        name: str,
        request: Request,
        since: str | None = None,
        until: str | None = None,
    ) -> TimelineResponse:
        """Return raw pytr timeline events in the ``[since, until)`` window.

        ``since`` is required; ``until`` defaults to now. Malformed timestamps
        raise ``400 invalid_request``; a missing session raises
        ``401 session_expired``; a pytr failure raises ``502 tr_upstream_error``.

        The echoed ``since``/``until`` are normalised to UTC (``Z`` suffix); raw
        ``events`` are passed through unchanged.
        """
        since_dt, until_dt = parse_window(since, until)
        registry: InstanceRegistry = request.app.state.registry
        session = registry.get(name)
        events = await session.fetch_timeline(since_dt, until_dt)
        return TimelineResponse(
            instance=name,
            since=to_utc_iso(since_dt),
            until=to_utc_iso(until_dt),
            count=len(events),
            events=events,
        )
