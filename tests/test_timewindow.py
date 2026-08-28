"""Functional tests for the time-window parsing module."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from tr_bridge.timewindow import InvalidRequestError, parse_window, to_utc_iso


class TestParseWindow:
    def test_parses_full_utc_timestamps(self) -> None:
        since, until = parse_window("2026-08-01T00:00:00Z", "2026-08-10T00:00:00Z")

        assert since == datetime(2026, 8, 1, tzinfo=UTC)
        assert until == datetime(2026, 8, 10, tzinfo=UTC)

    def test_naive_since_is_assumed_utc(self) -> None:
        since, _ = parse_window("2026-08-01T00:00:00", "2026-08-10T00:00:00")

        assert since.tzinfo == UTC

    def test_non_utc_offset_is_preserved_as_aware_datetime(self) -> None:
        since, until = parse_window(
            "2026-08-01T02:00:00+02:00", "2026-08-10T05:30:00+05:30"
        )

        assert since.utcoffset() == timedelta(hours=2)
        assert until.utcoffset() == timedelta(hours=5, minutes=30)
        assert since.astimezone(UTC) == datetime(2026, 8, 1, tzinfo=UTC)
        assert until.astimezone(UTC) == datetime(2026, 8, 10, tzinfo=UTC)

    def test_until_defaults_to_now(self) -> None:
        before = datetime.now(tz=UTC)
        _, until = parse_window("2026-08-01T00:00:00Z", None)
        after = datetime.now(tz=UTC)

        assert before <= until <= after

    def test_missing_since_raises(self) -> None:
        with pytest.raises(InvalidRequestError):
            parse_window(None, "2026-08-10T00:00:00Z")

    def test_malformed_since_raises(self) -> None:
        with pytest.raises(InvalidRequestError):
            parse_window("not-a-date", None)

    def test_malformed_until_raises(self) -> None:
        with pytest.raises(InvalidRequestError):
            parse_window("2026-08-01T00:00:00Z", "not-a-date")

    def test_date_only_since_raises(self) -> None:
        with pytest.raises(InvalidRequestError):
            parse_window("2026-08-01", None)

    def test_date_only_until_raises(self) -> None:
        with pytest.raises(InvalidRequestError):
            parse_window("2026-08-01T00:00:00Z", "2026-08-10")

    def test_space_separated_timestamp_is_accepted(self) -> None:
        since, _ = parse_window("2026-08-01 00:00:00", None)

        assert since == datetime(2026, 8, 1, tzinfo=UTC)

    def test_until_before_since_raises(self) -> None:
        with pytest.raises(InvalidRequestError):
            parse_window("2026-08-10T00:00:00Z", "2026-08-01T00:00:00Z")

    def test_until_equal_to_since_raises(self) -> None:
        with pytest.raises(InvalidRequestError):
            parse_window("2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z")


class TestInvalidRequestError:
    def test_maps_to_400_invalid_request(self) -> None:
        error = InvalidRequestError("boom")
        problem = error.to_problem_detail()

        assert problem.status == 400
        assert problem.code == "invalid_request"
        assert problem.title == "Invalid request"
        assert problem.detail == "boom"


class TestToUtcIso:
    def test_utc_datetime_gets_z_suffix(self) -> None:
        assert to_utc_iso(datetime(2026, 8, 1, tzinfo=UTC)) == "2026-08-01T00:00:00Z"

    def test_non_utc_offset_is_converted_to_utc(self) -> None:
        aware = datetime(2026, 8, 1, 2, 0, 0, tzinfo=timezone(timedelta(hours=2)))

        assert to_utc_iso(aware) == "2026-08-01T00:00:00Z"

    def test_naive_datetime_is_assumed_utc(self) -> None:
        assert to_utc_iso(datetime(2026, 8, 1)) == "2026-08-01T00:00:00Z"
