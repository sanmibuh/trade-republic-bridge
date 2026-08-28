"""Web adapter response/request schemas (Pydantic models)."""

from __future__ import annotations

from pydantic import BaseModel


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


class TimelineResponse(BaseModel):
    instance: str
    since: str
    until: str
    count: int
    events: list[dict]
