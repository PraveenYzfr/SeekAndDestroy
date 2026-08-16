"""Which investigation and which graph node the current work belongs to.

The audit row for a model call has to name the investigation and the node that
made it, but the chains in app.agents deliberately know about neither - they
take already-computed evidence and return a parsed object, which is what makes
them testable with MockChatModel alone. Threading two identifiers through ten
chain signatures to satisfy the logger would trade that away.

A context variable keeps the wiring at the edges: app.graph.graph sets it once
per node, app.agents.structured reads it, and nothing in between changes shape.
ContextVar rather than a module global because FastAPI serves requests
concurrently - a global would let one investigation's node name land on
another investigation's audit row.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, NamedTuple, Optional


class AuditScope(NamedTuple):
    investigation_id: Optional[int]
    graph_node: Optional[str]


_EMPTY = AuditScope(None, None)

_scope: ContextVar[AuditScope] = ContextVar("sad_audit_scope", default=_EMPTY)


def current() -> AuditScope:
    """The active scope, or an empty one. Never raises: a model call made
    outside any graph node (an API endpoint calling a chain directly) is still
    audited, just without a node name."""
    return _scope.get()


@contextmanager
def graph_node(investigation_id: Optional[int], node_name: str) -> Iterator[None]:
    token = _scope.set(AuditScope(investigation_id, node_name))
    try:
        yield
    finally:
        # reset() rather than set(_EMPTY): nested scopes must restore the
        # outer one, not clear it.
        _scope.reset(token)
