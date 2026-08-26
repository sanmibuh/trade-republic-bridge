"""RFC 9457 Problem Details — error model and response helper."""

import json

from pydantic import BaseModel, model_validator
from starlette.responses import Response

_BASE_TYPE_URL = "https://tr-bridge/errors"


class ProblemDetail(BaseModel):
    """Represents an RFC 9457 Problem Details object."""

    status: int
    code: str
    title: str
    detail: str
    type: str = ""

    @model_validator(mode="after")
    def _derive_type(self) -> "ProblemDetail":
        if not self.type:
            slug = self.code.replace("_", "-")
            self.type = f"{_BASE_TYPE_URL}/{slug}"
        return self


def problem_response(detail: ProblemDetail) -> Response:
    """Return a Starlette ``Response`` with ``application/problem+json`` content type."""
    return Response(
        content=json.dumps(detail.model_dump()),
        status_code=detail.status,
        media_type="application/problem+json",
    )
