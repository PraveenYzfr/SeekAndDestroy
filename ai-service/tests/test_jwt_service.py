"""Shared JWT issuance/validation (SAD_AUTH__MODE=local|oidc) - the identity
layer every write path in the platform now sits behind. See
app.security.jwt_service's module docstring for the design.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest

import app.security.jwt_service as jwt_service
from app.config import get_settings
from app.security.jwt_service import (
    AuthenticatedEmployee,
    TokenError,
    create_local_token,
    validate_token,
)


def _set_auth(**overrides):
    settings = get_settings().auth
    original = {k: getattr(settings, k) for k in overrides}
    for k, v in overrides.items():
        setattr(settings, k, v)
    return original


def _restore_auth(original: dict):
    settings = get_settings().auth
    for k, v in original.items():
        setattr(settings, k, v)
    jwt_service.reset_jwks_client_cache()


# ---------------------------------------------------------------------------
# local mode
# ---------------------------------------------------------------------------


def test_local_token_roundtrips():
    original = _set_auth(mode="local", local_signing_key="test-key-123")
    try:
        token = create_local_token(employee_id=42, employee_number="E042", display_name="Ada Lovelace", email="ada@x.com")
        employee = validate_token(token)
        assert employee == AuthenticatedEmployee(
            employee_id=42, employee_number="E042", display_name="Ada Lovelace", email="ada@x.com"
        )
    finally:
        _restore_auth(original)


def test_create_local_token_refuses_in_oidc_mode():
    original = _set_auth(mode="oidc")
    try:
        with pytest.raises(TokenError, match="SAD_AUTH__MODE=local"):
            create_local_token(employee_id=1, employee_number="E001", display_name="X", email="x@x.com")
    finally:
        _restore_auth(original)


def test_validate_token_rejects_expired_token():
    original = _set_auth(mode="local", local_signing_key="test-key-123")
    try:
        now = datetime.now(timezone.utc)
        expired_claims = {"employee_id": 1, "iat": now - timedelta(minutes=10), "exp": now - timedelta(minutes=1)}
        token = pyjwt.encode(expired_claims, "test-key-123", algorithm="HS256")
        with pytest.raises(TokenError):
            validate_token(token)
    finally:
        _restore_auth(original)


def test_validate_token_rejects_tampered_signature():
    original = _set_auth(mode="local", local_signing_key="test-key-123")
    try:
        token = create_local_token(employee_id=1, employee_number="E001", display_name="X", email="x@x.com")
        forged = pyjwt.encode(pyjwt.decode(token, "test-key-123", algorithms=["HS256"]), "wrong-key", algorithm="HS256")
        with pytest.raises(TokenError):
            validate_token(forged)
    finally:
        _restore_auth(original)


def test_validate_token_rejects_missing_employee_id_claim():
    original = _set_auth(mode="local", local_signing_key="test-key-123")
    try:
        token = pyjwt.encode({"sub": "no-employee-id-here"}, "test-key-123", algorithm="HS256")
        with pytest.raises(TokenError, match="employee_id"):
            validate_token(token)
    finally:
        _restore_auth(original)


# ---------------------------------------------------------------------------
# oidc mode (real RS256 crypto, JWKS network lookup stubbed)
# ---------------------------------------------------------------------------


def test_oidc_mode_validates_real_rs256_token(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    now = datetime.now(timezone.utc)
    claims = {
        "employee_id": 7, "employee_number": "E007", "name": "Jane Doe", "email": "jane@example.com",
        "aud": "seekanddestroy-api", "iat": now, "exp": now + timedelta(minutes=5),
    }
    token = pyjwt.encode(claims, private_key, algorithm="RS256")

    class _StubSigningKey:
        key = public_key

    class _StubJwksClient:
        def get_signing_key_from_jwt(self, tok):
            return _StubSigningKey()

    monkeypatch.setattr(jwt_service, "_get_jwks_client", lambda: _StubJwksClient())
    original = _set_auth(mode="oidc", oidc_audience="seekanddestroy-api")
    try:
        employee = validate_token(token)
        assert employee.employee_id == 7
        assert employee.email == "jane@example.com"
    finally:
        _restore_auth(original)


def test_oidc_mode_rejects_wrong_audience(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    now = datetime.now(timezone.utc)
    token = pyjwt.encode(
        {"employee_id": 7, "aud": "some-other-api", "iat": now, "exp": now + timedelta(minutes=5)},
        private_key, algorithm="RS256",
    )

    class _StubSigningKey:
        key = public_key

    class _StubJwksClient:
        def get_signing_key_from_jwt(self, tok):
            return _StubSigningKey()

    monkeypatch.setattr(jwt_service, "_get_jwks_client", lambda: _StubJwksClient())
    original = _set_auth(mode="oidc", oidc_audience="seekanddestroy-api")
    try:
        with pytest.raises(TokenError):
            validate_token(token)
    finally:
        _restore_auth(original)
