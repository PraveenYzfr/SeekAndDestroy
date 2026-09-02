"""Scoring a model against the platform's own answer key.

Reads sad.AgentAuditLog - the record of what every chain sent and received -
and grades it with app.evaluation.graders. Nothing here calls a model: the
calls already happened and were already paid for, so a full evaluation run
costs a table scan rather than a provider bill. Comparing two providers does
not mean re-running the estate; every historical call is already tagged with
the model that produced it.

Model drift is why this exists. Model ids here are ``*-latest`` aliases, so the
model changes underneath the service with no release and no notice. Without a
scorecard that shows up first as somebody reading a report that quotes a number
nobody computed.

Four things this does that a printed average does not:

**Cache hits are counted but never graded.** A cached answer is the same text
served again. Grading it each time turns one success into twenty and quietly
weights the score towards whatever happens to be popular. Fidelity is measured
over generated calls only, and the cached count is reported beside it.

**Denominators travel with the rates.** "100% entity fidelity" over three
mentions is not the same claim as over four hundred, and a scorecard that hides
which one it is invites the wrong conclusion.

**Latency comes from the audit row.** StartedAt and CompletedAt were already
being written; p50 and p95 cost nothing extra and are half of what "is this
model usable in production" means.

**Rows are read in batches.** An estate that has been running for a year has
more audit rows than belong in one result set.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

import structlog

from app.evaluation.graders import grade_call, was_truncated
from app.repositories.base import T, fetch_all

logger = structlog.get_logger(__name__)

#: Rows per round trip. The row cap in app.repositories.base applies per query,
#: so a year of audit rows has to be walked rather than selected.
BATCH_SIZE = 500

#: Properties a caller can hold a model to. Entity fidelity has no acceptable
#: failure rate - a cluster code that was never a candidate is not a degraded
#: answer, it is a wrong one - so a threshold on it is normally 1.0.
GRADED_PROPERTIES = ("number_fidelity", "entity_fidelity", "completeness")


@dataclass
class _Totals:
    grounded: int = 0
    total: int = 0

    def add(self, grounded: int, total: int) -> None:
        self.grounded += grounded
        self.total += total

    @property
    def rate(self) -> float | None:
        return None if self.total == 0 else self.grounded / self.total


def _percentile(values: list[int], fraction: float) -> int | None:
    """Nearest-rank percentile. No numpy dependency for two numbers, and
    nearest-rank is the honest reading of a small sample - interpolating
    between two observations invents a latency nothing recorded."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


@dataclass
class ModelScorecard:
    """One model's behaviour over the calls it made."""

    model: str
    calls: int = 0
    generated: int = 0
    cached: int = 0
    failures: int = 0
    #: Prompt was capped, so fidelity is not measurable. Reported rather than
    #: dropped: a rate computed over an unstated subset is worse than none.
    ungradeable: int = 0
    latencies_ms: list[int] = field(default_factory=list)
    totals: dict[str, _Totals] = field(default_factory=lambda: defaultdict(_Totals))
    by_schema: dict[str, dict[str, _Totals]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(_Totals))
    )
    flagged: list[dict] = field(default_factory=list)
    #: Every (call, grader) verdict from this run, for persistence. Held
    #: separately from `flagged`, which is a capped sample for reading - a
    #: stored record has to be complete or the rate it supports is not
    #: re-derivable from it.
    graded_calls: list[dict] = field(default_factory=list)

    def record(self, row: dict, *, cached: bool, schema: str) -> None:
        self.calls += 1
        self.failures += int(row["Success"] is False)
        if row["DurationMs"] is not None:
            self.latencies_ms.append(int(row["DurationMs"]))

        if cached:
            # Counted, not graded: this text was already graded when it was
            # generated, and scoring it again is double counting.
            self.cached += 1
            return

        self.generated += 1
        if was_truncated(row["InputJson"]):
            self.ungradeable += 1
            return

        for grade in grade_call(row["InputJson"], row["OutputJson"], schema):
            self.totals[grade.name].add(grade.grounded, grade.total)
            self.by_schema[schema][grade.name].add(grade.grounded, grade.total)
            # Kept per call as well as summed, so the run can be PERSISTED rather
            # than only reported. Until now every grading pass recomputed these
            # numbers, printed them and discarded them - which meant no history,
            # no way to say which call produced a bad figure, and no way to
            # compare a score against one taken before a grader changed.
            self.graded_calls.append({
                "audit_id": row["AuditId"],
                "investigation_id": row.get("InvestigationId"),
                "grader": grade.name,
                "grounded": grade.grounded,
                "total": grade.total,
                "ungrounded": list(grade.ungrounded),
            })
            if grade.ungrounded:
                self.flagged.append({
                    "audit_id": row["AuditId"], "schema": schema,
                    "property": grade.name, "ungrounded": grade.ungrounded[:8],
                })

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "calls": self.calls,
            "generated": self.generated,
            "cached": self.cached,
            "failures": self.failures,
            "ungradeable": self.ungradeable,
            "latency_p50_ms": _percentile(self.latencies_ms, 0.50),
            "latency_p95_ms": _percentile(self.latencies_ms, 0.95),
            "properties": {
                name: {
                    "rate": self.totals[name].rate,
                    # The denominator is part of the claim, not a footnote.
                    "observations": self.totals[name].total,
                }
                for name in GRADED_PROPERTIES
                if self.totals[name].total > 0
            },
            "by_schema": {
                schema: {
                    name: {"rate": totals.rate, "observations": totals.total}
                    for name, totals in props.items()
                }
                for schema, props in sorted(self.by_schema.items())
            },
            "flagged_count": len(self.flagged),
        }


def _model_of(input_json: str | None) -> tuple[str, bool]:
    """The model identity and cache flag recorded on the row.

    A row that does not carry them reports as "unknown" rather than being
    dropped - a missing label is itself worth seeing in the scorecard.
    """
    if not input_json:
        return "unknown", False
    try:
        payload = json.loads(input_json)
    except (ValueError, TypeError):
        return "unknown", False
    return str(payload.get("model") or "unknown"), bool(payload.get("cache_hit"))


def _scan(investigation_id: int | None, limit: int):
    """Walk llm:* audit rows newest first, in batches.

    Keyset pagination on AuditId rather than OFFSET: the table only ever grows
    at the head, and an OFFSET scan re-reads everything it has already skipped.
    """
    seen = 0
    before_id: int | None = None

    while seen < limit:
        where = ["ToolName LIKE 'llm:%'"]
        params: dict = {"batch": min(BATCH_SIZE, limit - seen)}
        if investigation_id is not None:
            where.append("InvestigationId = :investigation_id")
            params["investigation_id"] = investigation_id
        if before_id is not None:
            where.append("AuditId < :before_id")
            params["before_id"] = before_id

        rows = fetch_all(
            f"SELECT TOP (:batch) AuditId, InvestigationId, ToolName, InputJson, OutputJson, Success, "
            f"DATEDIFF(millisecond, StartedAt, CompletedAt) AS DurationMs "
            f"FROM {T('AgentAuditLog')} WHERE {' AND '.join(where)} ORDER BY AuditId DESC",
            params,
            max_rows=BATCH_SIZE,
        )
        if not rows:
            return
        for row in rows:
            yield row
        seen += len(rows)
        before_id = rows[-1]["AuditId"]


def evaluate(*, investigation_id: int | None = None, limit: int = 20_000) -> dict:
    """Grade recorded model calls, grouped by the model that made them.

    ``investigation_id`` narrows to one investigation - the form used when
    asking "is this particular report trustworthy?" rather than "how is this
    model behaving?".
    """
    scorecards: dict[str, ModelScorecard] = {}
    total_rows = 0

    for row in _scan(investigation_id, limit):
        total_rows += 1
        model, cached = _model_of(row["InputJson"])
        schema = str(row["ToolName"] or "").removeprefix("llm:")
        card = scorecards.setdefault(model, ModelScorecard(model=model))
        card.record(row, cached=cached, schema=schema)

    # PERSIST BEFORE RETURNING. Every previous grading pass computed these
    # numbers and threw them away, so there was no history, no way to attribute a
    # bad figure to the call that produced it, and no way to compare a score
    # against one taken before the graders changed.
    #
    # Best-effort: storing a verdict must never fail the run that produced it -
    # the same rule the answer-level table follows. But the count is REPORTED, so
    # "graded 40, stored 0" is visible rather than a silence that reads as
    # success. That exact silence hid a missing INSERT grant for hours.
    stored = 0
    try:
        from app.repositories import call_evaluation_repository

        stored = call_evaluation_repository.record_many(
            [g for c in scorecards.values() for g in c.graded_calls]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("evaluation.persist_failed", error=str(exc)[:300])

    logger.info("evaluation.completed", rows=total_rows, models=len(scorecards), stored=stored)
    return {
        "stored_verdicts": stored,
        "calls_seen": total_rows,
        "models": [c.as_dict() for c in sorted(scorecards.values(), key=lambda c: -c.calls)],
        # Capped: a scorecard is for reading, and the first few examples of a
        # property failing say as much as four hundred of them.
        "flagged": [f for c in scorecards.values() for f in c.flagged][:50],
    }


def check_thresholds(result: dict, thresholds: dict[str, float], *, min_observations: int = 20) -> list[str]:
    """Which models fall below the bar. Empty means the run passes.

    ``min_observations`` keeps a thin sample from failing a build: three
    numbers at 66% is not evidence of a regression, and a gate that cries wolf
    gets switched off. Thin samples are reported as skipped rather than passed,
    so nobody reads silence as a clean bill of health.
    """
    failures = []
    for card in result["models"]:
        for name, floor in thresholds.items():
            measured = card["properties"].get(name)
            if measured is None:
                continue
            if measured["observations"] < min_observations:
                failures.append(
                    f"SKIPPED {card['model']} {name}: only {measured['observations']} observations "
                    f"(need {min_observations})"
                )
                continue
            if measured["rate"] < floor:
                failures.append(
                    f"FAILED {card['model']} {name}: {measured['rate']:.1%} below {floor:.1%} "
                    f"over {measured['observations']} observations"
                )
    return failures
