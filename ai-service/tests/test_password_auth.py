"""Username/password sign-in.

Two layers: the scrypt primitives (pure, no database) and the /api/auth/login
endpoint against the real seeded database.

The endpoint tests set and then remove a throwaway credential on a real
Employee row inside each test, so the suite never leaves a usable password
behind on any account.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.repositories import employee_repository
from app.security.passwords import (
    DEFAULT_N,
    InvalidPasswordHash,
    hash_password,
    needs_rehash,
    verify_password,
)

client = TestClient(app)

TEST_PASSWORD = "correct-horse-battery-staple-9137"


# =============================================================================
# Hashing primitives
# =============================================================================


def test_hash_is_self_describing_and_carries_its_parameters():
    stored = hash_password("hunter2-hunter2")
    algorithm, params, salt, key = stored.split("$")
    assert algorithm == "scrypt"
    assert f"n={DEFAULT_N}" in params
    assert salt and key


def test_the_same_password_never_produces_the_same_hash_twice():
    """A fresh random salt per call - otherwise identical passwords would be
    visibly identical in the table, and one cracked hash would crack them all.
    """
    assert hash_password(TEST_PASSWORD) != hash_password(TEST_PASSWORD)


def test_the_plaintext_never_appears_in_the_stored_value():
    assert TEST_PASSWORD not in hash_password(TEST_PASSWORD)


def test_verify_accepts_the_right_password_and_rejects_everything_else():
    stored = hash_password(TEST_PASSWORD)
    assert verify_password(TEST_PASSWORD, stored)
    assert not verify_password(TEST_PASSWORD.upper(), stored)
    assert not verify_password(TEST_PASSWORD + "x", stored)
    assert not verify_password("", stored)


def test_verify_returns_false_rather_than_raising_for_unusable_hashes():
    """A caller must not be able to tell "no password set" from "wrong
    password" by catching an exception - both have to look identical.
    """
    for stored in (None, "", "not-a-hash", "scrypt$garbage$x$y", "bcrypt$n=1,r=1,p=1$a$b"):
        assert verify_password(TEST_PASSWORD, stored) is False


def test_empty_password_cannot_be_hashed():
    with pytest.raises(ValueError):
        hash_password("")


def test_needs_rehash_flags_weaker_parameters_only():
    assert needs_rehash(hash_password(TEST_PASSWORD, n=1024)) is True
    assert needs_rehash(hash_password(TEST_PASSWORD)) is False
    assert needs_rehash(None) is False
    assert needs_rehash("not-a-hash") is True


def test_a_hash_made_with_older_parameters_still_verifies():
    """Raising DEFAULT_N later must not lock everyone out - parameters are
    read back from each stored value, not assumed.
    """
    weak = hash_password(TEST_PASSWORD, n=1024)
    assert verify_password(TEST_PASSWORD, weak)


def test_invalid_password_hash_is_a_value_error():
    assert issubclass(InvalidPasswordHash, ValueError)


# =============================================================================
# /api/auth/login
# =============================================================================


@pytest.fixture
def employee_with_password():
    """Give a real seeded employee a throwaway password, then remove it.

    Restores the previous hash rather than blindly clearing, so running the
    suite can never revoke a password a human deliberately set.
    """
    employee = employee_repository.get_by_number("E1001")
    assert employee is not None, "seed data must contain E1001"
    previous = employee_repository.get_password_hash(employee.EmployeeId)

    employee_repository.set_password_hash(employee.EmployeeId, hash_password(TEST_PASSWORD))
    yield employee

    if previous:
        employee_repository.set_password_hash(employee.EmployeeId, previous)
    else:
        employee_repository.clear_password_hash(employee.EmployeeId)


def test_login_with_employee_number_returns_a_usable_token(employee_with_password):
    response = client.post(
        "/api/auth/login", json={"username": "E1001", "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["employee_id"] == employee_with_password.EmployeeId

    # The token must be accepted by the same guard every other endpoint uses.
    protected = client.get(
        "/api/clusters/data-centers", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert protected.status_code == 200

    # ...and rejected without one, so the test proves the guard is real.
    assert client.get("/api/clusters/data-centers").status_code == 401


def test_login_with_email_works_too(employee_with_password):
    response = client.post(
        "/api/auth/login",
        json={"username": employee_with_password.Email, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200


def test_wrong_password_is_rejected(employee_with_password):
    response = client.post("/api/auth/login", json={"username": "E1001", "password": "wrong"})
    assert response.status_code == 401


def test_unknown_user_and_wrong_password_are_indistinguishable(employee_with_password):
    """Different responses would turn this endpoint into an account
    enumerator - an attacker could confirm which employees exist without ever
    guessing a password.
    """
    unknown = client.post(
        "/api/auth/login", json={"username": "E-DOES-NOT-EXIST", "password": TEST_PASSWORD}
    )
    wrong = client.post("/api/auth/login", json={"username": "E1001", "password": "wrong"})

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["title"] == wrong.json()["title"]
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_an_employee_with_no_password_cannot_sign_in():
    """No implicit default credential exists anywhere in this platform."""
    employee = employee_repository.get_by_number("E1002")
    if employee is None:
        pytest.skip("seed data has no E1002")
    previous = employee_repository.get_password_hash(employee.EmployeeId)
    employee_repository.clear_password_hash(employee.EmployeeId)
    try:
        response = client.post(
            "/api/auth/login", json={"username": "E1002", "password": TEST_PASSWORD}
        )
        assert response.status_code == 401
    finally:
        if previous:
            employee_repository.set_password_hash(employee.EmployeeId, previous)


def test_login_requires_both_fields():
    assert client.post("/api/auth/login", json={"username": "E1001"}).status_code == 422
    assert client.post("/api/auth/login", json={"password": "x"}).status_code == 422
    assert client.post("/api/auth/login", json={"username": "", "password": "x"}).status_code == 422


def test_the_password_is_never_echoed_back(employee_with_password):
    response = client.post(
        "/api/auth/login", json={"username": "E1001", "password": TEST_PASSWORD}
    )
    assert TEST_PASSWORD not in response.text


def test_password_hash_never_leaves_the_api(employee_with_password):
    """PasswordHash is deliberately absent from the Employee model, so no
    endpoint that serializes an employee can expose it.
    """
    from app.models.entities import Employee

    assert "PasswordHash" not in Employee.model_fields

    token = client.post(
        "/api/auth/login", json={"username": "E1001", "password": TEST_PASSWORD}
    ).json()["access_token"]
    response = client.get("/api/clusters/data-centers", headers={"Authorization": f"Bearer {token}"})
    assert "PasswordHash" not in response.text
    assert "scrypt$" not in response.text
