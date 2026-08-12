from __future__ import annotations

from app.models.entities import Investigation
from app.repositories.base import T, execute, execute_insert, fetch_all, fetch_one


def create(
    query: str, investigation_type: str, created_by: int, conversation_id: str | None = None
) -> int:
    """``conversation_id`` links this investigation to the chat that produced
    it, so a later "give me the options again" can find it. NULL is the normal
    case for investigations started outside the chat.
    """
    return execute_insert(
        T("Investigation"),
        "InvestigationId",
        {
            "Query": query,
            "InvestigationType": investigation_type,
            "Status": "Created",
            "CreatedBy": created_by,
            "ConversationId": conversation_id,
        },
    )


def get_by_id(investigation_id: int) -> Investigation | None:
    row = fetch_one(
        f"SELECT * FROM {T('Investigation')} WHERE InvestigationId = :id", {"id": investigation_id}
    )
    return Investigation(**row) if row else None


def update_status(investigation_id: int, status: str) -> None:
    execute(
        f"UPDATE {T('Investigation')} SET Status = :status WHERE InvestigationId = :id",
        {"status": status, "id": investigation_id},
    )


def mark_completed(investigation_id: int, status: str = "Completed") -> None:
    execute(
        f"UPDATE {T('Investigation')} SET Status = :status, CompletedAt = SYSUTCDATETIME() "
        f"WHERE InvestigationId = :id",
        {"status": status, "id": investigation_id},
    )


def list_recent(limit: int = 100) -> list[Investigation]:
    rows = fetch_all(
        f"SELECT TOP (:limit) * FROM {T('Investigation')} ORDER BY StartedAt DESC", {"limit": limit}
    )
    return [Investigation(**r) for r in rows]
