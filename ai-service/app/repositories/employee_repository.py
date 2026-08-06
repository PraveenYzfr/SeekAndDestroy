from __future__ import annotations

from app.models.entities import Employee
from app.repositories.base import T, fetch_all, fetch_one


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


def list_active(limit: int = 200) -> list[Employee]:
    rows = fetch_all(
        f"SELECT TOP (:limit) * FROM {T('Employee')} WHERE IsActive = 1 ORDER BY DisplayName",
        {"limit": limit},
    )
    return [Employee(**r) for r in rows]
