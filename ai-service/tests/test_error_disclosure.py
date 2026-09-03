"""What an error response is allowed to tell the caller.

Built from a live finding: the drift guard's diagnostic message was reaching the
caller verbatim as a 400 body, including the engine-computed evidence value.

    guards.py   NumberDriftError("LLM output field 'overall_score' = 87.1 does
                not match evidence 'overall_score' = 98.33 ...")
    errors.py   handle_value_error -> _problem_response(..., 400, str(exc))

NumberDriftError subclasses ValueError, and that inheritance is what silently
turned an internal string into a response body.

The inversion mattered more than the leak: guards.py imports no logger and the
handler did not log, so the evidence value went to the CALLER and nothing went
to the OPERATOR. These tests pin both halves - what is hidden, and that hiding
it costs the operator nothing.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.guards import NumberDriftError
from app.api.errors import ProblemDetailsError, _is_public, register_exception_handlers
from app.repositories.base import RowLimitExceeded

#: The real message shape, taken from guards.py rather than invented - if the
#: guard's wording changes, this test should still be testing the real thing.
DRIFT_MESSAGE = (
    "LLM output field 'overall_score' = 87.1 does not match evidence "
    "'overall_score' = 98.33. Rejecting explanation - numbers must come "
    "from the deterministic engines only."
)


@pytest.fixture
def client():
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/drift")
    def _drift():
        raise NumberDriftError(DRIFT_MESSAGE)

    @app.get("/internal")
    def _internal():
        # The shape the grep found across the codebase: a ValueError nobody
        # wrote for a caller.
        raise ValueError("no servers resolve under cluster CI 4711")

    @app.get("/public")
    def _public():
        raise ProblemDetailsError(400, "Invalid feedback", "rating must be -1, 0 or 1")

    @app.get("/rowlimit")
    def _rowlimit():
        raise RowLimitExceeded("query returned more than 500 rows; narrow the filter")

    return TestClient(app, raise_server_exceptions=False)


class TestTheDriftMessageDoesNotReachTheCaller:
    def test_the_evidence_value_is_not_in_the_body(self, client):
        """The whole point. 98.33 is an engine-computed figure and the caller
        asked a question, not for the platform's internal state."""
        body = client.get("/drift").text
        assert "98.33" not in body
        assert "87.1" not in body
        assert "overall_score" not in body

    def test_the_caller_still_learns_what_happened(self, client):
        """Hiding the number must not mean saying nothing. A refusal the reader
        cannot interpret gets retried until it works, which is worse."""
        detail = client.get("/drift").json()["detail"]
        assert "evidence" in detail.lower()
        assert "discarded" in detail.lower() or "refus" in detail.lower()

    def test_it_is_not_a_500(self, client):
        """The naive fix - stop subclassing ValueError - drops it into
        handle_unexpected and turns a correct rejection into a server error.
        A deliberate refusal is not a crash."""
        assert client.get("/drift").status_code == 422

    def test_the_operator_gets_the_full_message(self, client, capsys):
        """The other half of the inversion. Suppressing the detail is only
        acceptable because it now goes somewhere an operator can read it.

        capsys, NOT caplog - and that is a finding rather than a detail.
        structlog is configured with its own renderer writing to stdout and is
        NOT bridged to stdlib logging, so caplog sees nothing while the line is
        plainly emitted. Anything that consumes logs through stdlib - pytest's
        caplog, and any handler-based shipper - is blind to every structlog
        event this platform produces. That is the F3 gap, demonstrated.
        """
        client.get("/drift")
        out = capsys.readouterr().out
        assert "api.number_drift_rejected" in out
        assert "98.33" in out, (
            "the evidence value must reach the log - hiding it from the caller "
            "and from the operator is not a fix, it is the same bug rotated"
        )


class TestInternalValueErrorsAreSuppressed:
    def test_an_internal_message_is_not_rendered(self, client):
        """"no servers resolve under cluster CI 4711" names an internal id and
        tells the caller about estate structure. It was never written for them."""
        body = client.get("/internal").text
        assert "4711" not in body
        assert "cluster CI" not in body

    def test_it_is_still_a_400_with_a_usable_message(self, client):
        response = client.get("/internal")
        assert response.status_code == 400
        assert "correlationId" in response.json()

    def test_the_suppressed_text_is_logged(self, client, capsys):
        client.get("/internal")
        out = capsys.readouterr().out
        assert "api.invalid_request_suppressed" in out
        assert "4711" in out


class TestDeliberatelyPublicMessagesStillWork:
    def test_problem_details_detail_is_shown(self, client):
        """Route handlers compose these FOR the caller. Suppressing them would
        break every useful 400 in the platform - "rating must be -1, 0 or 1"
        is the entire value of that response."""
        assert "rating must be -1, 0 or 1" in client.get("/public").text

    def test_row_limit_message_is_shown(self, client):
        """Describes the REQUEST, not the data behind it."""
        assert "narrow the filter" in client.get("/rowlimit").text


class TestTheOptInItself:
    def test_private_by_default(self):
        """The direction that matters. The previous default published every
        exception message and nobody had to decide."""
        assert _is_public(ValueError("anything")) is False
        assert _is_public(RuntimeError("anything")) is False

    def test_drift_is_explicitly_not_public(self):
        assert NumberDriftError.public_detail is False
        assert _is_public(NumberDriftError("x")) is False

    def test_problem_details_is_explicitly_public(self):
        assert _is_public(ProblemDetailsError(400, "t", "d")) is True


class TestReadinessSaysWhetherNotWhy:
    """/api/ready is PUBLIC and UNAUTHENTICATED, which is not obvious.

    routes_system is mounted at main.py:88 without _auth_dep, and nginx proxies
    `location /api/` as a PREFIX - so /api/ready reaches the internet. The
    root-level 404s for /ready and /readyz never covered it.

    A comment in nginx.conf asserted the opposite, and being written down is
    what stopped anyone rechecking. It previously returned raw driver exception
    text plus the document and cache counts to anyone who asked.
    """

    @pytest.fixture
    def app_client(self):
        from app.main import app

        return TestClient(app, raise_server_exceptions=False)

    def test_healthy_response_carries_no_counts(self, app_client):
        """"ok (96937 documents)" tells a stranger the corpus size. A probe has
        never needed it; /api/index/status serves it behind auth."""
        body = app_client.get("/api/ready").text
        assert "documents" not in body
        assert "keys" not in body
        assert '"ok"' in body

    def test_a_failure_does_not_return_the_driver_message(self, app_client, monkeypatch):
        """The real leak. A pyodbc failure carries the driver, the provider and
        often the server name - handed to an unauthenticated caller."""
        import app.api.routes_system as rs

        boom = ("('08001', \"[08001][Microsoft][ODBC Driver 17 for SQL Server]"
                "Named Pipes Provider: Could not open a connection\")")
        monkeypatch.setattr(rs, "fetch_one", lambda *a, **k: (_ for _ in ()).throw(Exception(boom)))

        response = app_client.get("/api/ready")
        assert response.status_code == 503
        for term in ("ODBC", "Driver 17", "Named Pipes", "SQL Server", "08001"):
            assert term not in response.text, f"leaked: {term}"

    def test_the_verdict_is_still_usable(self, app_client, monkeypatch):
        """Hiding the reason must not make the probe useless - the caller still
        learns WHICH dependency is down, just not what it said."""
        import app.api.routes_system as rs

        monkeypatch.setattr(rs, "fetch_one", lambda *a, **k: (_ for _ in ()).throw(Exception("x")))
        checks = app_client.get("/api/ready").json()["checks"]
        assert checks["database"] == "error"
        assert checks["vector_store"] == "ok"

    def test_the_operator_gets_the_reason(self, app_client, monkeypatch, capsys):
        import app.api.routes_system as rs

        monkeypatch.setattr(
            rs, "fetch_one", lambda *a, **k: (_ for _ in ()).throw(Exception("ODBC Driver 17 exploded"))
        )
        app_client.get("/api/ready")
        out = capsys.readouterr().out
        assert "readiness.check_failed" in out
        assert "ODBC Driver 17 exploded" in out


class TestTheGatewayDoesNotRouteSystemPaths:
    """The shield nobody designed, asserted so it fails loudly.

    /api/ready is unauthenticated and reachable by anything that can reach the
    ai-service process. It is not reachable from the internet, and the reason is
    NOT this codebase: nginx proxies `location /api/` to api-gateway:5090, and
    the .NET gateway has no api/ready route, so the public URL 404s AT THE
    GATEWAY.

    I asserted the opposite in 1cff8a2 - "public, unauthenticated, to anyone" -
    from reading the prefix match without following proxy_pass to its target.
    7b measured it: curl against the public host returns 404 with an empty body,
    against a container still running the pre-fix config.

    That shield is a routing table, not a security boundary, and nothing tested
    it. This does. If someone adds a passthrough controller or points nginx at
    ai-service directly, this fails and names what it means.
    """

    GATEWAY_CONTROLLERS = pathlib.Path(__file__).resolve().parents[2] / (
        "api-gateway/SeekAndDestroy.Api/Controllers"
    )

    def _routes(self) -> set[str]:
        routes: set[str] = set()
        for cs in self.GATEWAY_CONTROLLERS.glob("*.cs"):
            for match in re.finditer(r'\[Route\("([^"]+)"\)\]', cs.read_text(encoding="utf-8")):
                routes.add(match.group(1).lower().rstrip("/"))
        return routes

    @pytest.mark.skipif(
        not GATEWAY_CONTROLLERS.exists(), reason="gateway sources not present"
    )
    @pytest.mark.parametrize("unsafe", ["api/ready", "api/health", "api/index"])
    def test_the_gateway_has_no_route_for_ai_service_system_paths(self, unsafe):
        """Adding one of these to the gateway publishes an unauthenticated
        readiness endpoint to the internet. If that is ever deliberate, it needs
        auth on the ai-service side first."""
        assert unsafe not in self._routes()

    @pytest.mark.skipif(
        not GATEWAY_CONTROLLERS.exists(), reason="gateway sources not present"
    )
    def test_the_gateway_does_have_the_routes_we_expect(self):
        """Guards the test above from passing because the glob broke rather than
        because the routes are absent."""
        routes = self._routes()
        assert "api/investigations" in routes
        assert "api/admin" in routes
