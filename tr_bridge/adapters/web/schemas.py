"""Web adapter response/request schemas (Pydantic models)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DependenciesModel(BaseModel):
    pytr: str
    python: str


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    dependencies: DependenciesModel


class InstancesResponse(BaseModel):
    instances: list[str]


class StatusResponse(BaseModel):
    name: str
    state: str


class LoginStateResponse(BaseModel):
    state: str


class TwoFactorRequest(BaseModel):
    code: str


class TimelineEventAmount(BaseModel):
    """Monetary amount attached to a timeline event, when present.

    ``value``/``currency`` are the fields consumers care about most, but they
    originate from Trade Republic's product surface (not guaranteed by pytr),
    so both are optional. ``value`` accepts float/int/string without coercion
    (a smart union preserves the upstream representation, so an integer stays an
    integer rather than becoming ``12.0``). Any extra keys (e.g.
    ``fractionDigits``) pass through.
    """

    model_config = ConfigDict(extra="allow")

    value: float | int | str | None = None
    currency: str | None = None


class TimelineEvent(BaseModel):
    """A single pytr timeline event.

    Two tiers of fields are declared here:

    * A minimal guaranteed floor — ``id``/``timestamp`` — which pytr accesses by
      direct indexing (and ``timestamp`` feeds the ``[since, until)`` window), so
      a valid event must carry them or pytr would crash before the bridge sees
      it. These are the only required, non-null keys.
    * Commonly-observed fields consumers rely on — ``source``, ``title``,
      ``subtitle``, ``amount``, ``eventType`` and ``status``. pytr usually
      populates ``source``/``title``/``subtitle``, but to keep the endpoint
      resilient against upstream shape drift they are declared **optional** so a
      degenerate event never turns into a 500 at response time. They remain
      documented in the schema for discoverability.

    Everything else — nested ``action``/``details`` payloads and any other
    upstream attribute — is forwarded verbatim via ``extra="allow"``. Combined
    with ``response_model_exclude_unset=True`` on the route, absent optional
    fields are omitted rather than emitted as ``null``, so the response stays a
    faithful echo. Typing this contract in a single place spares every
    downstream consumer from guessing it.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    timestamp: str
    source: str | None = None
    title: str | None = None
    subtitle: str | None = None
    amount: TimelineEventAmount | None = None
    eventType: str | None = None
    status: str | None = None


class TimelineResponse(BaseModel):
    instance: str
    since: str
    until: str
    count: int
    events: list[TimelineEvent]
