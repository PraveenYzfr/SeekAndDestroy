from __future__ import annotations

from app.models.entities import Employee
from app.repositories.base import T, execute, fetch_all, fetch_one


def get_by_id(employee_id: int) -> Employee | None:
    row = fetch_one(f"SELECT * FROM {T('Employee')} WHERE EmployeeId = :id", {"id": employee_id})
    return Employee(**row) if row else None


def get_by_number(employee_number: str) -> Employee | None:
    row = fetch_one(
        f"SELECT * FROM {T('Employee')} WHERE EmployeeNumber = :n", {"n": employee_number}
    )
    return Employee(**row) if row else None


def get_by_email(email: str) -> Employee | None:
    row = fetch_one(f"SELECT * FROM {T('Employee')} WHERE Email = :e", {"e": email})
    return Employee(**row) if row else None


def get_by_login(identifier: str) -> Employee | None:
    """Resolve a sign-in identifier - employee number or email, whichever the
    person typed. Both columns are UNIQUE, so at most one row can match.
    """
    row = fetch_one(
        f"SELECT * FROM {T('Employee')} WHERE EmployeeNumber = :i OR Email = :i",
        {"i": identifier},
    )
    return Employee(**row) if row else None


def get_password_hash(employee_id: int) -> str | None:
    """The single code path that reads PasswordHash.

    Kept off the Employee model on purpose: every other query returns
    Employee, and a hash that is never loaded cannot leak through an endpoint
    that serializes one.
    """
    row = fetch_one(
        f"SELECT PasswordHash FROM {T('Employee')} WHERE EmployeeId = :id", {"id": employee_id}
    )
    return row["PasswordHash"] if row and row["PasswordHash"] else None


def set_password_hash(employee_id: int, password_hash: str) -> None:
    """Store an already-hashed credential. This module never sees plaintext -
    hashing happens in app.security.passwords before the value gets here.
    """
    execute(
        f"UPDATE {T('Employee')} SET PasswordHash = :h, PasswordUpdatedAt = SYSUTCDATETIME() "
        f"WHERE EmployeeId = :id",
        {"h": password_hash, "id": employee_id},
    )


def clear_password_hash(employee_id: int) -> None:
    execute(
        f"UPDATE {T('Employee')} SET PasswordHash = NULL, PasswordUpdatedAt = NULL WHERE EmployeeId = :id",
        {"id": employee_id},
    )


def list_active(limit: int = 200) -> list[Employee]:
    rows = fetch_all(
        f"SELECT TOP (:limit) * FROM {T('Employee')} WHERE IsActive = 1 ORDER BY DisplayName",
        {"limit": limit},
    )
    return [Employee(**r) for r in rows]
