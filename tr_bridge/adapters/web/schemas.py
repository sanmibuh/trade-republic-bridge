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
    so both are optional. Any extra keys (e.g. ``fractionDigits``) pass through.
    """

    model_config = ConfigDict(extra="allow")

    value: float | None = None
    currency: str | None = None


class TimelineEvent(BaseModel):
    """A single pytr timeline event.

    Two tiers of fields are declared here:

    * The subset that ``pytr`` accesses directly and therefore guarantees to be
      present — ``id``/``timestamp``/``source`` (non-null, used as dict keys and
      for ``datetime.fromisoformat``) and ``title``/``subtitle`` (keys always
      present, value may be ``null``).
    * Commonly-observed Trade Republic fields consumers rely on — ``amount``,
      ``eventType`` and ``status``. These come from TR's upstream payload and
      are **not** guaranteed by pytr, so they are optional: documenting them in
      the schema aids discovery, while their absence never breaks validation.

    Everything else — nested ``action``/``details`` payloads and any other
    upstream attribute — is forwarded verbatim via ``extra="allow"``. Typing
    this contract in a single place spares every downstream consumer from
    guessing it.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    timestamp: str
    source: str
    title: str | None
    subtitle: str | None
    amount: TimelineEventAmount | None = None
    eventType: str | None = None
    status: str | None = None


class TimelineResponse(BaseModel):
    instance: str
    since: str
    until: str
    count: int
    events: list[TimelineEvent]
