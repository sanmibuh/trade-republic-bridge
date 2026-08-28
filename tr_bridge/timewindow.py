"""Time-window parsing and UTC normalisation for the ``[since, until)`` range.

This module owns the domain-ish logic that resolves and validates the timeline
query window, independent of the FastAPI layer. It parses ISO-8601 timestamps
and enforces the contract (``since`` required, ``until`` defaults to now,
``until`` strictly after ``since``, no date-only values). Naive timestamps are
assumed to be UTC; timezone-aware values keep their original offset. Rendering
to a UTC ``Z``-suffixed string is a separate concern handled by ``to_utc_iso``.
"""

from datetime import UTC, datetime

from tr_bridge.errors import DomainError


class InvalidRequestError(DomainError):
    """Raised when a request carries missing or malformed query parameters."""

    status = 400
    code = "invalid_request"
    title = "Invalid request"


def _parse_iso(value: str, field: str) -> datetime:
    """Parse an ISO-8601 timestamp, normalising naive values to UTC.

    Raises:
        InvalidRequestError: if *value* is not a valid ISO-8601 timestamp.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidRequestError(
            f"Query parameter {field!r} is not a valid ISO-8601 timestamp: {value!r}"
        ) from exc
    # datetime.fromisoformat() also accepts date-only strings (e.g. "2026-08-01"),
    # but the contract requires a full timestamp. Reject values that lack a time
    # component (no 'T'/'t' or space separator between date and time).
    if "T" not in value and "t" not in value and " " not in value.strip():
        raise InvalidRequestError(
            f"Query parameter {field!r} must be a full ISO-8601 timestamp with a "
            f"time component, not a date only: {value!r}"
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_window(since: str | None, until: str | None) -> tuple[datetime, datetime]:
    """Resolve the ``[since, until)`` window from raw query strings.

    ``since`` is required; ``until`` defaults to the current time.

    Raises:
        InvalidRequestError: if ``since`` is missing, either value is malformed,
            or ``until`` is not strictly later than ``since``.
    """
    if since is None:
        raise InvalidRequestError("Query parameter 'since' is required.")
    since_dt = _parse_iso(since, "since")
    until_dt = datetime.now(tz=UTC) if until is None else _parse_iso(until, "until")
    if until_dt <= since_dt:
        raise InvalidRequestError(
            f"Query parameter 'until' ({until_dt.isoformat()}) must be later than "
            f"'since' ({since_dt.isoformat()})."
        )
    return since_dt, until_dt


def to_utc_iso(dt: datetime) -> str:
    """Render *dt* as an ISO-8601 string normalised to UTC with a ``Z`` suffix.

    Naive datetimes are assumed to be UTC (consistent with :func:`parse_window`),
    so the output never depends on the host's local timezone.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
