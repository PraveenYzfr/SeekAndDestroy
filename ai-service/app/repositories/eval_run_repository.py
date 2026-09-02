"""Evaluation runs, their case results, and the comparison between two of them.

WRITES HERE ARE NOT BEST-EFFORT, and that is the opposite of
answer_evaluation_repository next door.

That one grades an answer already handed to a user, so a failed write is
swallowed: losing a comment must never turn a completed investigation into an
error. This one IS the deliverable. An evaluation run whose result was not
stored did not happen, and reporting "passed" for a run nobody can produce
evidence of is worse than reporting a failure. So these raise.

The distinction is worth stating because the two files look alike and the
swallowing in the other one already hid a real failure for a whole deploy.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import structlog

from app.repositories.base import T, execute, execute_insert, fetch_all, fetch_one

logger = structlog.get_logger(__name__)


def current_models() -> dict[str, str]:
    """Provider/model per role, right now.

    Read once at the START of a run and stored on the row. A role repointed
    halfway through a suite produces a run whose configuration cannot be stated,
    and a result that cannot be stated should not be recorded as though it can.
    """
    from app.agents.llm_factory import resolve_all_roles

    try:
        return {
            r["role"]: f"{r['provider']}/{r['model']}" for r in resolve_all_roles()
        }
    except Exception as exc:  # noqa: BLE001
        # Recorded as unknown rather than omitted. An absent ModelsJson and one
        # saying "we could not read this" are different claims about the run.
        logger.warning("eval_run.models_unavailable", error=str(exc)[:200])
        return {"_error": str(exc)[:200]}


def current_git_sha() -> str | None:
    """The commit under test. None when it cannot be determined - a wrong sha is
    worse than no sha, because it names an experiment that was never run."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip()[:40] or None if out.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def start(suite: str, *, triggered_by: str | None = None,
          baseline_run_id: int | None = None) -> int:
    """Open a run and return its id.

    Written at the START, not on completion, so a suite that crashes leaves a
    Running row rather than no evidence it was ever attempted. A run that
    vanishes on failure makes a flaky suite look like a suite nobody ran.
    """
    return execute_insert(
        T("EvalRun"), "EvalRunId",
        {
            "Suite": suite,
            "Status": "Running",
            "GitSha": current_git_sha(),
            "ModelsJson": json.dumps(current_models()),
            "BaselineRunId": baseline_run_id,
            "TriggeredBy": triggered_by,
        },
    )


def record_case(run_id: int, *, case_id: str, outcome: str, kind: str | None = None,
                hard_checks: Any = None, judge: dict | None = None,
                answer_excerpt: str | None = None, error: str | None = None,
                duration_ms: int | None = None) -> None:
    judge = judge or {}
    execute(
        f"INSERT INTO {T('EvalCaseResult')} "
        f"(EvalRunId, CaseId, CaseKind, Outcome, HardChecksJson, JudgeRelevance, "
        f" JudgeGroundedness, JudgeActionability, JudgeSelfJudged, AnswerExcerpt, "
        f" ErrorMessage, DurationMs) "
        f"VALUES (:run, :case, :kind, :outcome, :hard, :rel, :gro, :act, :self, "
        f"        :excerpt, :error, :ms)",
        {
            "run": run_id, "case": case_id[:80], "kind": (kind or None),
            "outcome": outcome,
            "hard": json.dumps(hard_checks) if hard_checks is not None else None,
            "rel": judge.get("relevance"), "gro": judge.get("groundedness"),
            "act": judge.get("actionability"), "self": judge.get("self_judged"),
            "excerpt": (answer_excerpt or "")[:2000] or None,
            "error": (error or "")[:500] or None, "ms": duration_ms,
        },
    )


def finish(run_id: int, *, status: str, totals: dict, hard_rate: float | None = None,
           judge_mean: float | None = None, judge_excluded: int = 0,
           notes: str | None = None) -> None:
    """Close a run with the verdict reached AT THE TIME.

    The verdict is stored, never re-derived. Thresholds change - they changed
    five times in one night while the graders were being fixed - and recomputing
    an old run under today's rules produces a verdict nobody ever acted on.
    """
    execute(
        f"UPDATE {T('EvalRun')} SET Status = :status, FinishedAt = SYSUTCDATETIME(), "
        f"CasesTotal = :total, CasesPassed = :passed, CasesFailed = :failed, "
        f"CasesSkipped = :skipped, HardCheckRate = :hard, JudgeMeanScore = :judge, "
        f"JudgeExcluded = :excluded, Notes = :notes WHERE EvalRunId = :id",
        {
            "id": run_id, "status": status,
            "total": totals.get("total", 0), "passed": totals.get("passed", 0),
            "failed": totals.get("failed", 0), "skipped": totals.get("skipped", 0),
            "hard": hard_rate, "judge": judge_mean, "excluded": judge_excluded,
            "notes": (notes or "")[:2000] or None,
        },
    )


def get(run_id: int) -> dict | None:
    return fetch_one(f"SELECT * FROM {T('EvalRun')} WHERE EvalRunId = :id", {"id": run_id})


def recent(suite: str | None = None, limit: int = 20) -> list[dict]:
    where = "WHERE Suite = :suite " if suite else ""
    params: dict[str, Any] = {"limit": int(limit)}
    if suite:
        params["suite"] = suite
    return fetch_all(
        f"SELECT TOP (:limit) * FROM {T('EvalRun')} {where}ORDER BY EvalRunId DESC", params
    )


def last_passing(suite: str) -> dict | None:
    """The most recent run of ``suite`` that passed.

    Offered as a CANDIDATE baseline, never applied automatically. A gate that
    compares each run to its predecessor permits unlimited drift in small steps:
    every run passes against a slightly worse one and no single comparison ever
    fails. Choosing the bar has to be an act somebody performs.
    """
    rows = fetch_all(
        f"SELECT TOP (1) * FROM {T('EvalRun')} WHERE Suite = :suite AND Status = 'Passed' "
        f"ORDER BY EvalRunId DESC",
        {"suite": suite},
    )
    return rows[0] if rows else None


def cases(run_id: int) -> list[dict]:
    return fetch_all(
        f"SELECT * FROM {T('EvalCaseResult')} WHERE EvalRunId = :id ORDER BY CaseId",
        {"id": run_id},
    )


def compare(run_id: int, baseline_run_id: int) -> dict:
    """Case-by-case difference between two runs.

    Joined on CaseId, which is why case ids in golden_set.yaml are stable
    identifiers rather than descriptions - renaming one silently breaks every
    comparison that spans the change, and it breaks it by making the case look
    new rather than by failing.

    Cases present in only one run are reported as added/removed rather than
    counted as changes. A case that did not exist in the baseline cannot have
    regressed, and treating it as a regression is how a growing suite starts
    failing its own gate.
    """
    now = {c["CaseId"]: c for c in cases(run_id)}
    before = {c["CaseId"]: c for c in cases(baseline_run_id)}

    regressed, fixed, unchanged = [], [], []
    for case_id in sorted(set(now) & set(before)):
        a, b = before[case_id], now[case_id]
        if a["Outcome"] == "Passed" and b["Outcome"] == "Failed":
            regressed.append(case_id)
        elif a["Outcome"] == "Failed" and b["Outcome"] == "Passed":
            fixed.append(case_id)
        else:
            unchanged.append(case_id)

    return {
        "regressed": regressed,
        "fixed": fixed,
        "unchanged": unchanged,
        "added": sorted(set(now) - set(before)),
        "removed": sorted(set(before) - set(now)),
    }


def verdict_for(comparison: dict, *, hard_failures: int,
                grader_failures: int = 0, judge_failures: int = 0) -> tuple[str, str]:
    """The status and the reason, decided in one place.

    Failed and Regressed are kept distinct on purpose. Failed means a rule that
    stands on its own was broken - a hard check, a floor - and is a bug.
    Regressed means this run is worse than the baseline while still inside every
    absolute limit, which may be a trade somebody chose. Collapsing them into
    one status forces the same response to both.
    """
    if hard_failures:
        return "Failed", f"{hard_failures} hard check(s) failed"
    # Deterministic graders, at the thresholds in app.evaluation.thresholds:
    # number and entity fidelity at 100%, completeness below 85%. A fabricated
    # figure is a wrong answer, not a degraded one, so it fails a run outright.
    if grader_failures:
        return "Failed", f"{grader_failures} case(s) failed a deterministic grader"
    # Judge failures are counted and REPORTED, never fatal. One model's opinion
    # of another's work is grounds for a retry, not for stopping a deploy - and
    # the case it catches (figures correct, answer useless) is one the graders
    # cannot see at all. See the 2x2 in thresholds.combine.
    if comparison.get("regressed"):
        names = ", ".join(comparison["regressed"][:5])
        return "Regressed", f"{len(comparison['regressed'])} case(s) worse than baseline: {names}"
    if judge_failures:
        return "Passed", (
            f"no hard failures and nothing worse than baseline; "
            f"{judge_failures} case(s) queued for remediation on judge score"
        )
    return "Passed", "no hard failures and nothing worse than baseline"
