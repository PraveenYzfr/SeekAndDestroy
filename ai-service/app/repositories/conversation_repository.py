"""Chat conversations and their turns.

A conversation is what makes a follow-up mean anything. Without one, every
chat message was an independent investigation and "give me the options again"
had nothing to point at.

Ownership is checked here rather than assumed: :func:`get` returns the row and
the caller compares ``CreatedBy`` (see app.api.routes_investigations, which
turns a mismatch into a 403). Conversation ids are server-generated uuid4 hex
precisely so that check has something to protect - an id a caller could choose
is an id they could guess.
"""

from __future__ import annotations

import uuid

from app.models.entities import Conversation, ConversationTurn
from app.repositories.base import T, execute, fetch_all, fetch_one


def create(created_by: int) -> str:
    conversation_id = uuid.uuid4().hex
    execute(
        f"INSERT INTO {T('Conversation')} (ConversationId, CreatedBy) VALUES (:id, :created_by)",
        {"id": conversation_id, "created_by": created_by},
    )
    return conversation_id


def get(conversation_id: str) -> Conversation | None:
    row = fetch_one(
        f"SELECT * FROM {T('Conversation')} WHERE ConversationId = :id", {"id": conversation_id}
    )
    return Conversation(**row) if row else None


def touch(conversation_id: str) -> None:
    execute(
        f"UPDATE {T('Conversation')} SET LastActivityAt = SYSUTCDATETIME() WHERE ConversationId = :id",
        {"id": conversation_id},
    )


def add_turn(
    conversation_id: str, role: str, message: str, investigation_id: int | None = None
) -> None:
    """Record one turn. ``role`` is 'User' or 'Assistant' (CHECK-constrained).

    ``message`` is stored as given for user turns and as a short summary for
    assistant turns - the full report lives on the Investigation row, and a
    conversation history is for resolving references, not for re-reading
    reports.
    """
    execute(
        f"INSERT INTO {T('ConversationTurn')} (ConversationId, Role, Message, InvestigationId) "
        f"VALUES (:id, :role, :message, :investigation_id)",
        {
            "id": conversation_id, "role": role, "message": message,
            "investigation_id": investigation_id,
        },
    )
    touch(conversation_id)


def recent_turns(conversation_id: str, limit: int = 12) -> list[ConversationTurn]:
    """The last ``limit`` turns, oldest first.

    Ordered by TurnId, which is the insertion order - CreatedAt has
    millisecond resolution and two turns of one exchange can land in the same
    millisecond.
    """
    rows = fetch_all(
        f"SELECT * FROM (SELECT TOP (:limit) * FROM {T('ConversationTurn')} "
        f"WHERE ConversationId = :id ORDER BY TurnId DESC) AS recent ORDER BY TurnId ASC",
        {"id": conversation_id, "limit": limit},
    )
    return [ConversationTurn(**r) for r in rows]


def last_investigation_id(conversation_id: str) -> int | None:
    """The most recent investigation this conversation produced, if any.

    Read from Investigation rather than from the turns: a turn's
    InvestigationId is a convenience for reading history, while the
    Investigation table is where the id is authoritative.
    """
    row = fetch_one(
        f"SELECT TOP 1 InvestigationId FROM {T('Investigation')} "
        f"WHERE ConversationId = :id ORDER BY InvestigationId DESC",
        {"id": conversation_id},
    )
    return int(row["InvestigationId"]) if row else None
