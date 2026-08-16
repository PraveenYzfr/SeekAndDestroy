"""Scoring a model against the platform's own answer key.

Reads sad.AgentAuditLog - the record of what every chain sent and received -
and grades it with app.evaluation.graders. Nothing here calls a model: the
calls already happened and were already paid for, so a full evaluation run
costs one table scan.

That is the point of grading from the audit log rather than from a live run:
switching provider and re-running the estate to compare two models costs real
money and real time, while every historical call is already sitting in a table
tagged with the model that produced it.

Model drift is the reason this exists. Model ids in this platform are
``*-latest`` aliases, so the model changes underneath the service with no
release and no notice. Without a scorecard, that shows up first as somebody
reading a report that quotes a number nobody computed.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

import structlog

from app.evaluation.graders import grade_call, was_truncated
from app.repositories.base import T, fetch_all

logger = structlog.get_logger(__name__)


@dataclass
class ModelScorecard:
    """One model's behaviour over the calls it made."""

    model: str
    calls: int = 0
    failures: int = 0
    cache_hits: int = 0
    #: Rows whose prompt was capped, so fidelity could not be measured. Counted
    #: and reported rather than quietly dropped - a scorecard computed over an
    #: unstated subset is worse than no scorecard.
    ungradeable: int = 0
    _totals: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    flagged: list[dict] = field(default_factory=list)

    def record(self, grades, *, failed: bool, cached: bool, truncated: bool,
               audit_id: int, schema: str) -> None:
        self.calls += 1
        self.failures += int(failed)
        self.cache_hits += int(cached)
        self.ungradeable += int(truncated)
        for grade in grades:
            bucket = self._totals[grade.name]
            bucket[0] += grade.grounded
            bucket[1] += grade.total
            if grade.ungrounded:
                self.flagged.append({
                    "audit_id": audit_id, "schema": schema,
                    "property": grade.name, "ungrounded": grade.ungrounded[:8],
                })

    def rate(self, name: str) -> float | None:
        grounded, total = self._totals.get(name, [0, 0])
        return None if total == 0 else grounded / total

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "calls": self.calls,
            "failures": self.failures,
            "cache_hits": self.cache_hits,
            "ungradeable": self.ungradeable,
            "number_fidelity": self.rate("number_fidelity"),
            "entity_fidelity": self.rate("entity_fidelity"),
            "completeness": self.rate("completeness"),
            "flagged_count": len(self.flagged),
        }


def _model_of(input_json: str | None) -> tuple[str, bool]:
    """The model identity and cache flag recorded on the row.

    Rows written before auditing carried these fields, or by anything that
    logs a bare prompt, report as "unknown" rather than being dropped - a
    missing label is itself worth seeing in the scorecard.
    """
    if not input_json:
        return "unknown", False
    try:
        payload = json.loads(input_json)
    except (ValueError, TypeError):
        return "unknown", False
    return str(payload.get("model") or "unknown"), bool(payload.get("cache_hit"))


def evaluate(*, investigation_id: int | None = None, limit: int = 2000) -> dict:
    """Grade recorded model calls, grouped by the model that made them.

    ``investigation_id`` narrows to a single investigation, which is the form
    used when asking "is this particular report trustworthy?" rather than "how
    is this model behaving overall?".
    """
    where = "WHERE ToolName LIKE 'llm:%'"
    params: dict = {"limit": limit}
    if investigation_id is not None:
        where += " AND InvestigationId = :investigation_id"
        params["investigation_id"] = investigation_id

    rows = fetch_all(
        f"SELECT TOP (:limit) AuditId, ToolName, InputJson, OutputJson, Success "
        f"FROM {T('AgentAuditLog')} {where} ORDER BY AuditId DESC",
        params,
        max_rows=limit,
    )

    scorecards: dict[str, ModelScorecard] = {}
    for row in rows:
        model, cached = _model_of(row["InputJson"])
        schema = str(row["ToolName"] or "").removeprefix("llm:")
        card = scorecards.setdefault(model, ModelScorecard(model=model))
        card.record(
            grade_call(row["InputJson"], row["OutputJson"], schema),
            failed=row["Success"] is False,
            cached=cached,
            truncated=was_truncated(row["InputJson"]),
            audit_id=row["AuditId"],
            schema=schema,
        )

    logger.info("evaluation.completed", rows=len(rows), models=len(scorecards))
    return {
        "calls_graded": len(rows),
        "models": [c.as_dict() for c in sorted(scorecards.values(), key=lambda c: -c.calls)],
        # Capped: a scorecard is for reading, and the first few examples of a
        # property failing say as much as four hundred of them.
        "flagged": [f for c in scorecards.values() for f in c.flagged][:50],
    }
