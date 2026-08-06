"""Shared identity check for MCP write tools - see app.security.jwt_service
for the underlying JWT validation (SAD_AUTH__MODE=local|oidc), the same
mechanism shared with the FastAPI service and the .NET gateway so all three
trust the exact same tokens.
"""

from __future__ import annotations

from app.repositories import employee_repository
from app.security.jwt_service import TokenError, validate_token


def authenticate(access_token: str, claimed_employee_id: int | None = None) -> tuple[int | None, dict | None]:
    """Validates ``access_token`` and returns ``(employee_id, None)`` on
    success, or ``(None, {"error": ...})`` on failure - callers should
    ``return error`` from the second element immediately, matching this
    module's existing error-dict convention (these tools return an error
    dict rather than raising).

    If ``claimed_employee_id`` is also given (a tool's own
    ``reviewer_employee_id``/``requested_by_employee_id`` parameter) and it
    disagrees with the token, that's rejected too - the token is always
    authoritative, never a value the caller merely asserts.
    """
    if not access_token:
        return None, {"error": "access_token is required - this action cannot be anonymous"}
    try:
        employee = validate_token(access_token)
    except TokenError as exc:
        return None, {"error": f"invalid access_token: {exc}"}

    record = employee_repository.get_by_id(employee.employee_id)
    if record is None or not record.IsActive:
        return None, {"error": "the access_token's employee_id does not match an active employee"}

    if claimed_employee_id is not None and claimed_employee_id != employee.employee_id:
        return None, {
            "error": f"access_token identifies employee {employee.employee_id}, but the request claims "
            f"employee {claimed_employee_id} - omit the field and let the token be authoritative."
        }
    return employee.employee_id, None
