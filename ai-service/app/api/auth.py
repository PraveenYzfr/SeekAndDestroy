"""FastAPI authentication: the ``get_current_employee`` dependency every
write endpoint now sits behind, plus the local-mode dev-token issuance
endpoint. See app.security.jwt_service for the underlying JWT design
(SAD_AUTH__MODE=local|oidc).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import structlog

from app.api.errors import ProblemDetailsError
from app.api.schemas import DevTokenRequest, LoginRequest
from app.config import get_settings
from app.repositories import employee_repository
from app.security.jwt_service import AuthenticatedEmployee, TokenError, create_local_token, validate_token
from app.security.passwords import hash_password, needs_rehash, verify_password

logger = structlog.get_logger(__name__)

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


def require_admin(
    current: AuthenticatedEmployee = Depends(get_current_employee),
) -> AuthenticatedEmployee:
    """Admin-only. Re-reads IsAdmin from the database on every request.

    Deliberately not a token claim. A claim is a snapshot taken at login: revoke
    someone's admin and their existing token keeps working until it expires, and
    under SAD_AUTH__MODE=oidc the claims come from an identity provider that
    knows nothing about this flag. One indexed primary-key lookup per admin
    request is a small price for revocation that takes effect immediately.

    403 rather than 404: the caller is authenticated and the route exists, so
    hiding it would only make a permission problem look like a broken link.
    """
    record = employee_repository.get_by_id(current.employee_id)
    if record is None or not record.IsAdmin:
        raise ProblemDetailsError(
            403,
            "Administrator access required",
            "This action changes platform configuration and is restricted to administrators.",
        )
    return current


def _token_response(employee, ttl_minutes: int) -> dict:
    return {
        # A display hint so the UI can hide an administrator-only link. NOT the
        # authorisation decision: require_admin re-reads IsAdmin from the
        # database on every request, so forging this changes what a menu looks
        # like and nothing else.
        "is_admin": bool(getattr(employee, "IsAdmin", False)),
        "access_token": create_local_token(
            employee_id=employee.EmployeeId, employee_number=employee.EmployeeNumber,
            display_name=employee.DisplayName, email=employee.Email,
        ),
        "token_type": "bearer",
        "expires_in_minutes": ttl_minutes,
        "employee_id": employee.EmployeeId,
        "employee_number": employee.EmployeeNumber,
        "display_name": employee.DisplayName,
    }


@router.post("/api/auth/login")
def login(payload: LoginRequest):
    """Username/password sign-in, issuing the same JWT every other layer in
    the platform already validates - so this adds a way to *obtain* a token
    without changing how any token is *verified*.

    Every failure returns the same 401 with the same wording. Distinguishing
    "no such user" from "wrong password" from "no password set" would turn
    this endpoint into an account enumerator: an attacker could confirm which
    employees exist without ever guessing a password.

    Unavailable in ``oidc`` mode (404, like dev-token): when a real identity
    provider issues tokens, a second local way in would be a bypass of it.
    """
    settings = get_settings().auth
    if settings.mode != "local":
        raise ProblemDetailsError(
            404, "Not found",
            "Password sign-in is disabled outside SAD_AUTH__MODE=local - tokens come from the configured identity provider.",
        )

    invalid = ProblemDetailsError(401, "Invalid credentials", "The username or password is incorrect.")

    employee = employee_repository.get_by_login(payload.username)
    if employee is None or not employee.IsActive:
        # Still hash something, so a missing user does not return measurably
        # faster than a wrong password and become a timing oracle.
        verify_password(payload.password, hash_password("timing-equalizer"))
        raise invalid

    stored = employee_repository.get_password_hash(employee.EmployeeId)
    if not verify_password(payload.password, stored):
        logger.warning("auth.login_failed", employee_id=employee.EmployeeId)
        raise invalid

    # Opportunistic upgrade: the plaintext is only ever in hand right here, so
    # this is the one moment a hash created under weaker parameters can be
    # re-derived under current policy.
    if needs_rehash(stored):
        employee_repository.set_password_hash(employee.EmployeeId, hash_password(payload.password))
        logger.info("auth.password_rehashed", employee_id=employee.EmployeeId)

    logger.info("auth.login_succeeded", employee_id=employee.EmployeeId)
    return _token_response(employee, settings.local_token_ttl_minutes)


@router.post("/api/auth/dev-token")
def issue_dev_token(payload: DevTokenRequest):
    settings = get_settings().auth
    if settings.mode != "local":
        raise ProblemDetailsError(404, "Not found", "Dev-token issuance is disabled outside SAD_AUTH__MODE=local.")
    if not settings.allow_dev_token:
        # 404 rather than 403, matching the oidc-mode refusal above: a disabled
        # back door should not announce that it exists. Logged at warning
        # because an attempt to use it on a locked-down deployment is worth
        # seeing.
        logger.warning("auth.dev_token_denied", employee_number=payload.employee_number)
        raise ProblemDetailsError(
            404, "Not found", "Dev-token issuance is disabled (SAD_AUTH__ALLOW_DEV_TOKEN=false)."
        )
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
