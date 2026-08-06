"""JWT issuance and validation - the identity layer every write path in the
platform now sits behind (``SAD_AUTH__MODE``).

``local`` (default): a self-contained HMAC-signed token, issued by this
service's own ``POST /api/auth/dev-token`` endpoint against a real, active
``Employee`` row - not a real login flow (there is no password store in this
CMDB), but not a rubber stamp either: the employee must actually exist.
Meant for local development and demos with zero external identity provider,
mirroring how ``SAD_LLM__PROVIDER=mock`` needs no external dependency either.

``oidc``: standard JWKS-based RS256 validation against a real external
identity provider (Azure AD/Entra, Okta, ...). This service never issues
tokens in this mode - ``/api/auth/dev-token`` returns 404.

Every caller (FastAPI dependency, MCP tool, the .NET gateway's own
equivalent JwtBearer config) ends up trusting the same claims:
``employee_id`` is the one that matters - it replaces every previously
client-supplied ``reviewerEmployeeId``/``createdByEmployeeId`` value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
import structlog
from jwt import PyJWKClient

from app.config import get_settings

logger = structlog.get_logger(__name__)


class TokenError(Exception):
    """Any invalid/expired/malformed/wrong-audience token - callers should
    treat this uniformly as 401 and never leak *why* to the client."""


@dataclass(frozen=True)
class AuthenticatedEmployee:
    employee_id: int
    employee_number: str
    display_name: str
    email: str


def create_local_token(*, employee_id: int, employee_number: str, display_name: str, email: str) -> str:
    settings = get_settings().auth
    if settings.mode != "local":
        raise TokenError("dev-token issuance is only available when SAD_AUTH__MODE=local")
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(employee_id),
        "employee_id": employee_id,
        "employee_number": employee_number,
        "name": display_name,
        "email": email,
        "iat": now,
        "exp": now + timedelta(minutes=settings.local_token_ttl_minutes),
        "iss": "seekanddestroy-local",
    }
    return jwt.encode(claims, settings.local_signing_key, algorithm=settings.algorithm_local)


_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        settings = get_settings().auth
        jwks_url = settings.oidc_jwks_url or f"{settings.oidc_authority.rstrip('/')}/.well-known/jwks.json"
        _jwks_client = PyJWKClient(jwks_url)
    return _jwks_client


def reset_jwks_client_cache() -> None:
    global _jwks_client
    _jwks_client = None


def validate_token(token: str) -> AuthenticatedEmployee:
    settings = get_settings().auth
    try:
        if settings.mode == "oidc":
            signing_key = _get_jwks_client().get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, signing_key, algorithms=["RS256"], audience=settings.oidc_audience)
        else:
            claims = jwt.decode(token, settings.local_signing_key, algorithms=[settings.algorithm_local])
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc

    employee_id = claims.get("employee_id")
    if employee_id is None:
        raise TokenError("token is missing a required employee_id claim")
    try:
        employee_id = int(employee_id)
    except (TypeError, ValueError) as exc:
        raise TokenError("employee_id claim is not a valid integer") from exc

    return AuthenticatedEmployee(
        employee_id=employee_id,
        employee_number=str(claims.get("employee_number", "")),
        display_name=str(claims.get("name", "")),
        email=str(claims.get("email", "")),
    )
