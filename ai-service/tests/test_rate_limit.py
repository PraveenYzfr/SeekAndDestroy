"""Two ways in that were left open.

The daily call budget caps what the platform spends in a day; nothing capped
how fast one caller could spend it. An unattended loop against
/api/investigations exhausts the whole day's budget in about ninety seconds
and every other engineer gets "budget exhausted" until midnight.

And POST /api/auth/dev-token issued a valid token for any active employee
number with no credential check at all. Disabling it meant switching the whole
service to oidc mode - not an option for a deployment that uses local
username/password sign-in and simply wants the back door shut.
docker/docker-compose.vm.yml has been setting SAD_AUTH__ALLOW_DEV_TOKEN=false
for a while; until now nothing read it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.rate_limit import TokenBucketLimiter
from app.config import get_settings
from app.main import app

client = TestClient(app)


# =============================================================================
# The bucket
# =============================================================================


def test_a_caller_gets_its_allowance_then_is_refused():
    limiter = TokenBucketLimiter(capacity=3, per_seconds=60)
    assert [limiter.allow("e:1")[0] for _ in range(3)] == [True, True, True]

    allowed, retry_after = limiter.allow("e:1")
    assert allowed is False
    assert retry_after > 0, "a refusal has to say when to come back"


def test_one_caller_cannot_spend_another_caller_s_allowance():
    """The point of keying on the employee. A shared bucket means one
    engineer's loop locks out the whole team, which is the outage the limiter
    was supposed to prevent."""
    limiter = TokenBucketLimiter(capacity=2, per_seconds=60)
    limiter.allow("e:1")
    limiter.allow("e:1")
    assert limiter.allow("e:1")[0] is False
    assert limiter.allow("e:2")[0] is True


def test_the_allowance_refills_over_time():
    """A token bucket, not a fixed window: a window lets a caller spend the
    whole allowance at the end of one and the whole of the next at the start
    of the following, which is the burst it was meant to prevent at twice the
    size."""
    limiter = TokenBucketLimiter(capacity=2, per_seconds=2.0)
    limiter.allow("e:1")
    limiter.allow("e:1")
    assert limiter.allow("e:1")[0] is False

    # Advance the clock rather than sleeping through it.
    limiter._buckets["e:1"].updated_at -= 1.5
    assert limiter.allow("e:1")[0] is True


def test_zero_capacity_disables_the_limiter():
    limiter = TokenBucketLimiter(capacity=0, per_seconds=60)
    assert all(limiter.allow("e:1")[0] for _ in range(100))


# =============================================================================
# The dev-token back door
# =============================================================================


def test_the_dev_token_endpoint_can_be_shut_without_switching_to_oidc(monkeypatch):
    """The gap: the only way to disable it was SAD_AUTH__MODE=oidc, which a
    deployment using local password sign-in cannot do."""
    settings = get_settings()
    monkeypatch.setattr(settings.auth, "allow_dev_token", False)

    response = client.post("/api/auth/dev-token", json={"employee_number": "E1001"})
    # 404, not 403: a disabled back door should not announce that it exists.
    assert response.status_code == 404
    assert "access_token" not in response.json()


def test_it_still_issues_tokens_when_allowed(monkeypatch):
    settings = get_settings()
    if settings.auth.mode != "local":
        pytest.skip("dev-token is disabled outside local mode by design")
    monkeypatch.setattr(settings.auth, "allow_dev_token", True)

    response = client.post("/api/auth/dev-token", json={"employee_number": "E1001"})
    assert response.status_code == 200
    assert response.json()["access_token"]


def test_the_compose_override_now_reaches_something():
    """docker/docker-compose.vm.yml sets SAD_AUTH__ALLOW_DEV_TOKEN=false. Until
    this setting existed that line was inert - the deployment that most needed
    the back door shut was the one where the switch did nothing."""
    from app.config.settings import AuthSettings

    assert "allow_dev_token" in AuthSettings.model_fields
