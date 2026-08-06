from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_endpoint_checks_database_and_vector_store():
    response = client.get("/api/ready")
    assert response.status_code in (200, 503)
    body = response.json()
    assert "database" in body["checks"]
    assert "vector_store" in body["checks"]


def test_hosting_recommendations_endpoint_returns_ranked_candidates(auth_headers):
    response = client.post("/api/hosting/recommendations", json={"application_code": "APP-CRM"}, headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body["candidates"]) > 0
    assert all("eligibility_status" in c for c in body["candidates"])


def test_hosting_recommendations_endpoint_respects_configurable_top_n(auth_headers):
    # top_n is a per-request field, not a fixed constant - an infra engineer
    # can ask for the top 5, top 10, or any other count of eligible candidates.
    response = client.post(
        "/api/hosting/recommendations", json={"application_code": "APP-CRM", "top_n": 5}, headers=auth_headers
    )
    assert response.status_code == 200
    body = response.json()
    eligible = [c for c in body["candidates"] if c["eligibility_status"] == "Eligible"]
    assert len(eligible) <= 5


def test_hosting_recommendations_404_for_unknown_application(auth_headers):
    response = client.post(
        "/api/hosting/recommendations", json={"application_code": "APP-DOES-NOT-EXIST"}, headers=auth_headers
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_forecast_endpoint_rejects_unsupported_horizon(auth_headers):
    response = client.post(
        "/api/forecast", json={"cluster_code": "atl-03", "horizon_days": 45}, headers=auth_headers
    )
    assert response.status_code == 400


def test_protected_endpoint_without_token_is_rejected():
    response = client.post("/api/hosting/recommendations", json={"application_code": "APP-CRM"})
    assert response.status_code == 401


def test_recommendation_decision_rejects_mismatched_reviewer_id(auth_headers, auth_employee_id):
    other_employee_id = auth_employee_id + 1
    response = client.post(
        "/api/recommendations/1/decision",
        json={"decision": "Approve", "reviewer_employee_id": other_employee_id},
        headers=auth_headers,
    )
    # The token is authoritative; a body value that disagrees with it is rejected
    # outright rather than silently overridden - decisions can't be spoofed as
    # someone else even by an otherwise-authenticated caller.
    assert response.status_code == 403
