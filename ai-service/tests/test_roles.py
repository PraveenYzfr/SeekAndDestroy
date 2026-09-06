"""app.api.auth.require_role - a real regression test for a function that
had zero callers anywhere in the codebase until app.api.routes_insights
wired it up.

WHY THIS EXISTS
----------------
require_role read record.Role via getattr(record, "Role", None), where
record is an app.models.entities.Employee built from a SELECT * row.
Employee never declared a Role field, so Pydantic silently dropped the
column (its default is to ignore unrecognised keys, not to raise), and
getattr always fell through to None - ranking every employee, including
Administrators, below even "Viewer". The function has therefore returned
403 unconditionally since the day it was written, and nothing caught it
because nothing called it: not a test, not an endpoint, nothing.

Fixed by adding Role: Optional[str] = None to Employee. This test exists so
the next person who touches either the Employee model or require_role gets
a real, fast signal if the same class of bug returns - a Pydantic model
silently dropping a column FastAPI's own dependency then reads as "no
permission", which looks exactly like an intentional 403 rather than a bug.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.auth import require_role
from app.main import app
from app.repositories import employee_repository
from app.security.jwt_service import create_local_token

client = TestClient(app)


def test_employee_model_actually_exposes_role():
    """The specific gap: Employee(**row) must not drop the Role column that
    SELECT * already returns - this is what silently broke require_role."""
    employee = employee_repository.get_by_id(1)  # E1001, seeded Administrator
    assert employee is not None
    assert employee.Role == "Administrator"


def _token_for(employee_number: str) -> dict:
    employee = employee_repository.get_by_number(employee_number)
    assert employee is not None, f"seed fixture missing: {employee_number}"
    token = create_local_token(
        employee_id=employee.EmployeeId, employee_number=employee.EmployeeNumber,
        display_name=employee.DisplayName, email=employee.Email,
    )
    return {"Authorization": f"Bearer {token}"}


def test_require_role_allows_a_role_at_or_above_the_minimum():
    """E1001 is seeded Administrator - outranks every minimum, including
    the highest. This is the exact case that was silently broken: an
    Administrator getting 403'd by a Viewer-level gate looks like a
    misconfiguration, not a missing model field."""
    headers = _token_for("E1001")
    response = client.post("/api/insights/ask", json={"query": "How healthy is our CMDB?"}, headers=headers)
    assert response.status_code == 200


def test_require_role_rejects_a_role_below_the_minimum():
    """A real dependency, exercised directly rather than through one
    endpoint's route - proves the rejection path works generically, not
    only for whatever minimum routes_insights.py happens to use today."""
    from app.security.jwt_service import validate_token

    headers = _token_for("E1002")  # seeded Engineer
    token = headers["Authorization"].split(" ", 1)[1]
    current = validate_token(token)

    dependency = require_role("Administrator")
    import pytest

    from app.api.errors import ProblemDetailsError

    with pytest.raises(ProblemDetailsError) as exc_info:
        dependency(current=current)
    assert exc_info.value.status == 403


def test_require_role_unknown_role_string_raises_at_definition_time():
    import pytest

    with pytest.raises(ValueError):
        require_role("SuperAdmin")
