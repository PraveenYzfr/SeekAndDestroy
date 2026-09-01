"""Per-role model overrides. Absent means "use the configured default".

The distinction between an absent row and a stored row is the whole point: it is
what lets the admin screen show "from config" versus "overridden", and what the
Reset control writes. Storing the default explicitly would erase it - a later
change to config/settings.py would then be silently ignored for every role
somebody had ever touched.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.repositories.base import T, execute, fetch_all, fetch_one


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get(role_name: str) -> dict | None:
    return fetch_one(
        f"SELECT RoleName, Provider, Model, UpdatedBy, UpdatedAt "
        f"FROM {T('LlmRoleOverride')} WHERE RoleName = :role",
        {"role": role_name},
    )


def list_all() -> list[dict]:
    return fetch_all(f"SELECT * FROM {T('LlmRoleOverride')} ORDER BY RoleName")


def as_map() -> dict[str, dict]:
    """Every override in one query.

    Resolution reads all roles at once when an investigation starts, so this is
    one round trip rather than six - and, more importantly, one consistent
    snapshot. Six separate reads could straddle an edit and produce a run whose
    roles came from two different configurations.
    """
    return {row["RoleName"]: row for row in list_all()}


def set_override(role_name: str, provider: str, model: str, updated_by: str | None) -> None:
    """UPDATE then INSERT rather than MERGE - see index_run_repository.claim for
    the same choice and the same reason."""
    updated = execute(
        f"UPDATE {T('LlmRoleOverride')} SET Provider = :provider, Model = :model, "
        f"UpdatedBy = :by, UpdatedAt = :now WHERE RoleName = :role",
        {"role": role_name, "provider": provider, "model": model, "by": updated_by, "now": _now()},
    )
    if updated == 0:
        execute(
            f"INSERT INTO {T('LlmRoleOverride')} (RoleName, Provider, Model, UpdatedBy, UpdatedAt) "
            f"VALUES (:role, :provider, :model, :by, :now)",
            {"role": role_name, "provider": provider, "model": model, "by": updated_by, "now": _now()},
        )


def clear(role_name: str) -> bool:
    """Remove an override so the role falls back to config. Returns whether a
    row was actually removed, so the caller can tell "reset" from "was already
    the default" instead of reporting success either way."""
    return execute(f"DELETE FROM {T('LlmRoleOverride')} WHERE RoleName = :role", {"role": role_name}) > 0
