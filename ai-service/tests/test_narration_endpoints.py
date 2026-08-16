"""Narration on the structured endpoints.

Three chains - explain_forecast, summarize_tradeoffs and
explain_application_right_sizing - were written, guarded, tested and called
from nowhere. The forecast and comparison screens rendered numbers with no
prose beside them while the code to write that prose sat in the tree.

Two properties matter more than the wording these produce, and both are
asserted strictly here: narration is off unless asked for (these endpoints can
return hundreds of results, and narrating each is a model call apiece), and a
narration failure costs the prose and nothing else.

The prose itself is asserted leniently on purpose. It is best-effort by
design: a quota refusal or a number-drift rejection legitimately yields no
explanation, and a test that failed in that case would be testing the
provider's availability rather than this platform's behaviour.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.api import narration
from app.main import app

client = TestClient(app)


# =============================================================================
# Off by default
# =============================================================================


def test_a_forecast_is_not_narrated_unless_asked(auth_headers):
    response = client.post("/api/forecast", json={"cluster_code": "atl-03", "horizon_days": 90}, headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert "cpu" in body and "memory" in body and "storage" in body
    assert "explanation" not in body, "narration must cost nothing when nobody asked for it"


def test_placement_returns_no_tradeoffs_unless_asked(auth_headers):
    response = client.post("/api/hosting/recommendations", json={"application_code": "APP-CRM"}, headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["tradeoffs"] is None


def test_right_sizing_returns_no_explanations_unless_asked(auth_headers):
    response = client.post("/api/right-sizing/applications", json={}, headers=auth_headers)
    assert response.status_code == 200
    assert "explanations" not in response.json()


# =============================================================================
# Which resource gets explained
# =============================================================================


class _Resource:
    def __init__(self, breaches, exhaustion, predicted):
        self.breaches_threshold_within_horizon = breaches
        self.exhaustion_date = exhaustion
        self.predicted_percent = predicted


class _Forecast:
    def __init__(self, cpu, memory, storage):
        self.cpu, self.memory, self.storage = cpu, memory, storage


def test_the_explained_resource_is_the_one_that_breaches():
    """Narrating all three resources costs three calls to say two things
    nobody asked about. The constraint is what needs explaining.
    """
    forecast = _Forecast(
        cpu=_Resource(False, None, 40.0),
        memory=_Resource(True, date(2026, 10, 1), 95.0),
        storage=_Resource(False, None, 55.0),
    )
    assert narration.binding_resource(forecast)[0] == "memory"


def test_the_earliest_exhaustion_wins_when_several_breach():
    forecast = _Forecast(
        cpu=_Resource(True, date(2026, 12, 1), 91.0),
        memory=_Resource(True, date(2026, 9, 1), 93.0),
        storage=_Resource(False, None, 20.0),
    )
    assert narration.binding_resource(forecast)[0] == "memory"


def test_the_closest_resource_wins_when_none_breaches():
    """No breach still has a constraint - the one nearest to being one."""
    forecast = _Forecast(
        cpu=_Resource(False, None, 61.0),
        memory=_Resource(False, None, 44.0),
        storage=_Resource(False, None, 12.0),
    )
    assert narration.binding_resource(forecast)[0] == "cpu"


# =============================================================================
# Failure never reaches the caller
# =============================================================================


def test_a_failing_chain_costs_the_prose_and_nothing_else():
    def explode():
        raise RuntimeError("provider refused")

    assert narration.safely("explain_forecast", explode) is None


def test_a_narration_failure_still_returns_the_numbers(monkeypatch, auth_headers):
    """The deterministic result is what the caller came for. It is already
    computed by the time narration is attempted, and losing it to a model
    outage would be the platform failing at its actual job.
    """
    import app.api.routes_forecast as routes_forecast

    def explode(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(routes_forecast, "explain_forecast", explode)
    response = client.post(
        "/api/forecast", json={"cluster_code": "atl-03", "horizon_days": 90, "explain": True}, headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["explanation"] is None
    assert body["cpu"]["predicted_percent"] is not None, "the forecast itself must survive"
    assert body["explained_resource"] in ("cpu", "memory", "storage")


# =============================================================================
# Live, against the configured provider
# =============================================================================


def test_an_explained_forecast_names_the_resource_it_explained(auth_headers):
    response = client.post(
        "/api/forecast", json={"cluster_code": "atl-03", "horizon_days": 90, "explain": True}, headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["explained_resource"] in ("cpu", "memory", "storage")
    if body["explanation"] is not None:
        assert body["explanation"]["entity_code"] == "atl-03"


def test_explained_placement_summarises_the_shortlist(auth_headers):
    response = client.post(
        "/api/hosting/quick-recommendations",
        json={"cpu_cores": 4, "memory_gb": 16, "explain": True},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    eligible = [c for c in body["candidates"] if c["eligibility_status"] == "Eligible"]
    if not eligible:
        pytest.skip("no eligible candidates to compare")
    if body["tradeoffs"] is not None:
        assert body["tradeoffs"].get("summary") or body["tradeoffs"].get("title")


def test_right_sizing_narration_is_bounded(auth_headers):
    """200 applications must never become 200 model calls."""
    response = client.post("/api/right-sizing/applications", json={"explain": True}, headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["explained_count"] <= narration.MAX_NARRATED
    assert len(body["results"]) >= body["explained_count"]
