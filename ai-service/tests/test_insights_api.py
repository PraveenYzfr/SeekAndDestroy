"""Integration tests for /api/insights/ask - auth, role gate, and the two
intents that need no LLM call (health, impact), so this suite stays fast and
does not depend on a real provider being configured or reachable.

The aggregate intent (spec_parser + narrator, two live LLM calls) is
exercised in tests/test_insights.py against MockChatModel and, in this
session, manually against the real configured provider - not repeated here
as an HTTP round trip.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_ask_requires_authentication():
    response = client.post("/api/insights/ask", json={"query": "How healthy is our CMDB?"})
    assert response.status_code == 401


def test_ask_rejects_empty_query(auth_headers):
    response = client.post("/api/insights/ask", json={"query": ""}, headers=auth_headers)
    assert response.status_code == 422  # Pydantic min_length=1


def test_ask_health_question_returns_200(auth_headers):
    response = client.post("/api/insights/ask", json={"query": "How healthy is our CMDB?"}, headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "health"
    assert "headline" in body and body["headline"]
    assert isinstance(body["caveats"], list)


def test_ask_impact_question_with_no_ci_named_returns_400(auth_headers):
    """UnknownCiError/NoCiNamedError are ValueError subclasses, mapped to 400
    by app.api.errors' generic handler - a refused question must reach the
    caller as an explicit error, not a 200 with a made-up answer."""
    response = client.post(
        "/api/insights/ask", json={"query": "What happens if the primary database fails?"}, headers=auth_headers,
    )
    assert response.status_code == 400
    assert "configuration item" in response.json()["detail"].lower()


def test_ask_impact_question_with_a_real_ci_returns_200(auth_headers):
    from app.repositories.base import T, fetch_all

    cluster = fetch_all(f"SELECT TOP (1) Name FROM {T('ConfigurationItem')} WHERE ClassName = 'cmdb_ci_cluster'")[0]
    response = client.post(
        "/api/insights/ask", json={"query": f"What breaks if {cluster['Name']} fails?"}, headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "impact"
    assert body["details"]["affected_cis"] >= 0
