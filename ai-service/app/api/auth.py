"""FastAPI authentication: the ``get_current_employee`` dependency every
write endpoint now sits behind, plus the local-mode dev-token issuance
endpoint. See app.security.jwt_service for the underlying JWT design
(SAD_AUTH__MODE=local|oidc).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.errors import ProblemDetailsError
from app.api.schemas import DevTokenRequest
from app.config import get_settings
from app.repositories import employee_repository
from app.security.jwt_service import AuthenticatedEmployee, TokenError, create_local_token, validate_token

router = APIRouter(tags=["auth"])
_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_employee(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> AuthenticatedEmployee:
    if credentials is None:
        raise ProblemDetailsError(401, "Authentication required", "A Bearer token is required for this request.")
    try:
        employee = validate_token(credentials.credentials)
    except TokenError as exc:
        raise ProblemDetailsError(401, "Invalid token", str(exc)) from exc

    record = employee_repository.get_by_id(employee.employee_id)
    if record is None or not record.IsActive:
        raise ProblemDetailsError(
            401, "Unknown or inactive employee",
            "The token's employee_id does not match an active employee - it may have been deactivated since the token was issued.",
        )
    return employee


def require_matching_employee_id(current: AuthenticatedEmployee, claimed: Optional[int]) -> int:
    """Every write endpoint's authoritative employee_id is always the
    authenticated caller's, never a request-body value - but if the caller
    also supplied one and it disagrees, that's a real inconsistency worth
    rejecting loudly rather than silently overriding.
    """
    if claimed is not None and claimed != current.employee_id:
        raise ProblemDetailsError(
            403, "Employee id mismatch",
            f"The authenticated caller is employee {current.employee_id}, but the request body claims "
            f"employee {claimed}. Omit the field and let the token be authoritative.",
        )
    return current.employee_id


@router.post("/api/auth/dev-token")
def issue_dev_token(payload: DevTokenRequest):
    settings = get_settings().auth
    if settings.mode != "local":
        raise ProblemDetailsError(404, "Not found", "Dev-token issuance is disabled outside SAD_AUTH__MODE=local.")
    employee = employee_repository.get_by_number(payload.employee_number)
    if employee is None:
        raise ProblemDetailsError(404, "Employee not found", f"No employee with number {payload.employee_number!r}.")
    if not employee.IsActive:
        raise ProblemDetailsError(403, "Employee inactive", "This employee is not active.")
    token = create_local_token(
        employee_id=employee.EmployeeId, employee_number=employee.EmployeeNumber,
        display_name=employee.DisplayName, email=employee.Email,
    )
    return {
        "access_token": token, "token_type": "bearer",
        "expires_in_minutes": settings.local_token_ttl_minutes,
        "employee_id": employee.EmployeeId, "display_name": employee.DisplayName,
    }
