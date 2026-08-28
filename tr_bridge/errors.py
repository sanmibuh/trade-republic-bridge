"""RFC 9457 Problem Details — error model and response helper."""

from typing import ClassVar

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


class DomainError(Exception):
    """Base class for domain errors carrying their own HTTP/Problem metadata.

    Subclasses declare the RFC 9457 mapping as class attributes (``status``,
    ``code``, ``title``). A single generic exception handler can then translate
    *any* :class:`DomainError` into a :class:`ProblemDetail`, so adding a new
    domain error never requires touching the web layer's handler wiring.

    The ``detail`` field defaults to ``str(self)``; subclasses may override the
    :attr:`detail` property when a fixed, message-independent detail is desired.
    """

    status: ClassVar[int]
    code: ClassVar[str]
    title: ClassVar[str]

    @property
    def detail(self) -> str:
        """Human-readable explanation; defaults to the exception message."""
        return str(self)

    def to_problem_detail(self) -> ProblemDetail:
        """Render this error as an RFC 9457 :class:`ProblemDetail`."""
        return ProblemDetail(
            status=self.status,
            code=self.code,
            title=self.title,
            detail=self.detail,
        )


def problem_response(detail: ProblemDetail) -> Response:
    """Return a Starlette ``Response`` with ``application/problem+json`` content."""
    return Response(
        content=detail.model_dump_json(),
        status_code=detail.status,
        media_type="application/problem+json",
    )
