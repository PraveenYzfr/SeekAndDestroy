"""Shared pytest fixtures.

These tests run against the live seeded database (database/schema.sql +
database/seed.sql applied to PraveenDB) rather than a mocked/in-memory DB -
this project's seed data IS the fixture set, deliberately engineered (see
scripts/generate_seed.py::SCENARIOS) so every rule/scoring/right-sizing/
forecasting behavior has a real, known-answer example to assert against.
Run `sqlcmd -S LAPTOP-R6U8H616 -d PraveenDB -E -C -i database\\schema.sql`
and `...seed.sql` before running this suite (see docs/setup.md).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pytest


@pytest.fixture(scope="session")
def scenarios():
    import generate_seed

    return generate_seed.SCENARIOS


@pytest.fixture(autouse=True, scope="session")
def _quiet_logging():
    import logging

    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))


@pytest.fixture(scope="session")
def auth_employee_id() -> int:
    """The real, active Employee row every auth-related test authenticates
    as (E1001 in the deterministic seed data)."""
    return 1


@pytest.fixture(scope="session")
def auth_headers(auth_employee_id) -> dict:
    """A valid Authorization header for API tests - obtained the same way a
    real client would, via POST /api/auth/dev-token (SAD_AUTH__MODE=local is
    the test-suite default)."""
    from app.security.jwt_service import create_local_token

    token = create_local_token(
        employee_id=auth_employee_id, employee_number="E1001", display_name="Praveen Yadav",
        email="praveen.yadav@seekanddestroy.example",
    )
    return {"Authorization": f"Bearer {token}"}
