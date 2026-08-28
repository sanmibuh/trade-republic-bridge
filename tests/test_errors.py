"""Tests for tr_bridge.errors — RFC 9457 Problem Details helpers."""

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route

from tr_bridge.errors import DomainError, ProblemDetail, problem_response


def _make_app(detail: ProblemDetail) -> Starlette:
    """Tiny Starlette app that always returns the given problem detail."""

    async def endpoint(request):
        return problem_response(detail)

    return Starlette(routes=[Route("/", endpoint)])


class TestProblemDetail:
    def test_fields_set_correctly(self) -> None:
        p = ProblemDetail(
            status=404,
            code="instance_not_found",
            title="Instance not found",
            detail="No instance named 'x'.",
        )

        assert p.status == 404
        assert p.code == "instance_not_found"
        assert p.title == "Instance not found"
        assert p.detail == "No instance named 'x'."
        assert p.type == "https://tr-bridge/errors/instance-not-found"

    def test_type_derived_from_code(self) -> None:
        p = ProblemDetail(
            status=401,
            code="session_expired",
            title="Session expired",
            detail="Re-login required.",
        )

        assert p.type == "https://tr-bridge/errors/session-expired"

    def test_underscores_become_dashes_in_type(self) -> None:
        p = ProblemDetail(
            status=502,
            code="tr_upstream_error",
            title="Upstream error",
            detail="pytr failed.",
        )

        assert "tr-upstream-error" in p.type


class TestProblemResponse:
    def test_content_type_is_problem_json(self) -> None:
        detail = ProblemDetail(
            status=400,
            code="invalid_request",
            title="Invalid request",
            detail="Missing 'since'.",
        )
        client = TestClient(_make_app(detail), raise_server_exceptions=False)

        resp = client.get("/")

        assert resp.headers["content-type"] == "application/problem+json"

    def test_status_code_matches_detail(self) -> None:
        detail = ProblemDetail(
            status=409,
            code="login_in_progress",
            title="Login in progress",
            detail="Already logging in.",
        )
        client = TestClient(_make_app(detail), raise_server_exceptions=False)

        resp = client.get("/")

        assert resp.status_code == 409

    def test_json_body_contains_required_fields(self) -> None:
        detail = ProblemDetail(
            status=500,
            code="internal_error",
            title="Internal error",
            detail="Unexpected failure.",
        )
        client = TestClient(_make_app(detail), raise_server_exceptions=False)

        body = client.get("/").json()

        assert body["status"] == 500
        assert body["code"] == "internal_error"
        assert body["title"] == "Internal error"
        assert body["detail"] == "Unexpected failure."
        assert "type" in body


class TestDomainError:
    def test_to_problem_detail_uses_class_metadata_and_message(self) -> None:
        class SampleError(DomainError):
            status = 418
            code = "sample_error"
            title = "Sample error"

        problem = SampleError("something went wrong").to_problem_detail()

        assert problem.status == 418
        assert problem.code == "sample_error"
        assert problem.title == "Sample error"
        assert problem.detail == "something went wrong"
        assert problem.type == "https://tr-bridge/errors/sample-error"

    def test_detail_can_be_overridden_independently_of_message(self) -> None:
        class FixedDetailError(DomainError):
            status = 409
            code = "fixed_detail"
            title = "Fixed detail"

            @property
            def detail(self) -> str:
                return "a fixed, message-independent explanation"

        problem = FixedDetailError("internal message").to_problem_detail()

        assert problem.detail == "a fixed, message-independent explanation"
