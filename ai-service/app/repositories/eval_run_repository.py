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


#: A live run completes a case every fifteen seconds or so. Fifteen MINUTES
#: without one means no case has finished in sixty heartbeats' worth of time,
#: which is dead rather than slow. Generous on purpose: reaping a run that was
#: merely stuck on one long call would destroy real work, and the cost of
#: waiting is a stale row for a few extra minutes.
STALE_AFTER_MINUTES = 15


def reap_stale_runs() -> int:
    """Close runs that were killed without ever finishing. Returns how many.

    A deploy recreated ai-service mid-suite on 2026-09-06 and left run 27 in
    status Running with no FinishedAt, permanently. The row is written at START
    by design - a crashed suite should leave evidence it was attempted - but
    nothing ever revisited it, so an aborted run and a live one differed only by
    a timestamp somebody had to interpret.

    That cost twice: partial cases can be read as a result or pinned as a
    baseline, and any deploy guard checking this table for "is an eval running?"
    would block every future deploy on a run that died an hour ago.

    MARKED, NEVER DELETED. A row that disappears is indistinguishable from a run
    nobody started, which is the exact failure this table exists to prevent.

    Falls back to StartedAt when HeartbeatAt is NULL, which is the case for a run
    that died before its first case and for every row predating migration 026.
    """
    try:
        killed = fetch_all(
            f"SELECT EvalRunId FROM {T('EvalRun')} "
            f"WHERE Status = 'Running' "
            f"  AND DATEDIFF(minute, ISNULL(HeartbeatAt, StartedAt), SYSUTCDATETIME()) >= :mins",
            {"mins": STALE_AFTER_MINUTES},
        )
        if not killed:
            return 0
        execute(
            f"UPDATE {T('EvalRun')} SET Status = 'Error', FinishedAt = SYSUTCDATETIME(), "
            f"Notes = CONCAT(ISNULL(Notes + ' | ', ''), "
            f"  'Presumed killed: no case completed for ', "
            f"  CAST(DATEDIFF(minute, ISNULL(HeartbeatAt, StartedAt), SYSUTCDATETIME()) AS VARCHAR(10)), "
            f"  ' minutes. A deploy, crash or OOM ended it. Any cases recorded are PARTIAL "
            f"and must not be read as a result or pinned as a baseline.') "
            f"WHERE Status = 'Running' "
            f"  AND DATEDIFF(minute, ISNULL(HeartbeatAt, StartedAt), SYSUTCDATETIME()) >= :mins",
            {"mins": STALE_AFTER_MINUTES},
        )
        ids = [r["EvalRunId"] for r in killed]
        logger.warning("eval_run.reaped_stale", run_ids=ids, after_minutes=STALE_AFTER_MINUTES)
        return len(ids)
    except Exception as exc:  # noqa: BLE001
        # Housekeeping must never stop a run from starting. A stale row is
        # untidy; refusing to run the suite because tidying failed is worse.
        logger.warning("eval_run.reap_failed", error=str(exc)[:200])
        return 0


def start(suite: str, *, triggered_by: str | None = None,
          baseline_run_id: int | None = None) -> int:
    """Open a run and return its id.

    Written at the START, not on completion, so a suite that crashes leaves a
    Running row rather than no evidence it was ever attempted. A run that
    vanishes on failure makes a flaky suite look like a suite nobody ran.

    Stale runs are reaped here rather than on a timer: a new run is the moment
    somebody is looking at this table, it needs no scheduler, and it cannot
    drift out of step with the code that writes the rows.
    """
    reap_stale_runs()
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


def _beat(run_id: int) -> None:
    """Mark this run as alive, and keep its tally current.

    Called from record_case because the runner ALREADY calls that once per case.
    A separate heartbeat the runner had to remember is one a future runner would
    not call, and its absence would look exactly like a dead run.

    THE TALLY IS UPDATED HERE FOR THE SAME REASON THE BEAT IS.

    CasesTotal was written only by complete(), so a run that did not complete
    reported ZERO cases however much work it had done. Run 39 was aborted after
    51 cases and read:

        run 39  Error  cases=0  passed=0        with 51 EvalCaseResult rows

    Praveen read that as a run that failed instantly. It had run for ten minutes.
    A row saying zero when the answer is fifty-one is not missing information -
    it is wrong information, and it is the shape this codebase keeps being caught
    by: absent reported as a value.

    Counted from EvalCaseResult rather than incremented, so the tally cannot
    drift from the rows it describes - a retried case or a lost UPDATE corrects
    itself on the next beat instead of accumulating an error. One extra COUNT per
    case, on a table with a hundred rows, inside an UPDATE that was happening
    anyway.

    complete() still writes the final figures. It agrees with this one, and where
    it does not, its numbers win: it is the only place that knows a run FINISHED
    rather than stopped.
    """
    try:
        execute(
            f"UPDATE r SET HeartbeatAt = SYSUTCDATETIME(), "
            f"    CasesTotal   = c.total, "
            f"    CasesPassed  = c.passed, "
            f"    CasesFailed  = c.failed, "
            f"    CasesSkipped = c.skipped "
            f"FROM {T('EvalRun')} r "
            f"CROSS APPLY ( "
            f"    SELECT COUNT(*) AS total, "
            # UPPER() rather than matching the stored casing. The rows say 'Passed'
            # and 'Failed'; SQL Server's default collation here is
            # case-insensitive so a lowercase literal happens to work, and a
            # tally that silently depends on a server collation setting is a
            # tally that reads zero on a differently-configured box.
            f"           SUM(CASE WHEN UPPER(Outcome) = 'PASSED'  THEN 1 ELSE 0 END) AS passed, "
            f"           SUM(CASE WHEN UPPER(Outcome) = 'FAILED'  THEN 1 ELSE 0 END) AS failed, "
            f"           SUM(CASE WHEN UPPER(Outcome) = 'SKIPPED' THEN 1 ELSE 0 END) AS skipped "
            f"    FROM {T('EvalCaseResult')} WHERE EvalRunId = r.EvalRunId "
            f") c "
            f"WHERE r.EvalRunId = :id AND r.Status = 'Running'",
            {"id": int(run_id)},
        )
    except Exception as exc:  # noqa: BLE001
        # A missed beat costs a stale row at worst. Failing the case it was
        # recording would cost the run.
        logger.warning("eval_run.heartbeat_failed", run_id=run_id, error=str(exc)[:200])


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
    # AFTER the insert, not before: the heartbeat means "a case completed", and
    # beating first would keep a run that fails on every case looking healthy.
    _beat(run_id)


def _spend_over_run(run_id: int) -> dict:
    """What this run cost, summed from the calls it actually made.

    SUMMED, NEVER RE-PRICED. Cost is computed at call time from sad.ModelPrice
    and copied onto the audit row; re-deriving it here would let a price change
    rewrite what last month's run cost.

    UnpricedCalls is the reason this returns a dict rather than a number.
    A run whose models had no price in force produces a cost that is a FLOOR,
    not a total - and a small confident wrong figure is far harder to catch than
    a zero. On 2026-09-06 deepseek was 37 of 63 live calls with none of them
    priced; a run over that estate would have stored a plausible number missing
    most of its own traffic.

    Never raises. A run's VERDICT must not be lost because the accounting query
    failed - the scores are the point and the spend is commentary on them.
    """
    empty = {"cost": None, "tin": None, "tout": None, "unpriced": 0}
    try:
        row = fetch_one(
            f"SELECT SUM(a.CostUsd) AS CostUsd, "
            f"       SUM(a.PromptTokens) AS TokensIn, "
            f"       SUM(a.CompletionTokens) AS TokensOut, "
            f"       SUM(CASE WHEN a.CostUsd IS NULL THEN 1 ELSE 0 END) AS Unpriced "
            f"  FROM {T('AgentAuditLog')} a "
            f"  JOIN {T('EvalRun')} r ON r.EvalRunId = :id "
            f" WHERE a.StartedAt >= r.StartedAt "
            f"   AND a.StartedAt <= ISNULL(r.FinishedAt, SYSUTCDATETIME())",
            {"id": int(run_id)},
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("eval_run.spend_read_failed", run_id=run_id, error=str(exc)[:200])
        return empty
    if not row:
        return empty
    return {
        "cost": row.get("CostUsd"), "tin": row.get("TokensIn"),
        "tout": row.get("TokensOut"), "unpriced": int(row.get("Unpriced") or 0),
    }


def finish(run_id: int, *, status: str, totals: dict, hard_rate: float | None = None,
           judge_mean: float | None = None, judge_excluded: int = 0,
           notes: str | None = None) -> None:
    """Close a run with the verdict reached AT THE TIME.

    The verdict is stored, never re-derived. Thresholds change - they changed
    five times in one night while the graders were being fixed - and recomputing
    an old run under today's rules produces a verdict nobody ever acted on.

    SPEND IS RECORDED HERE TOO, for the same reason and with the same rule.
    Without it this table could show a run was BETTER and never whether it was
    cheaper or slower - and for a model swap that is most of the decision.
    "Worse but a third of the price" and "worse for no saving" are different
    answers and the table could not tell them apart.
    """
    spend = _spend_over_run(run_id)
    execute(
        f"UPDATE {T('EvalRun')} SET Status = :status, FinishedAt = SYSUTCDATETIME(), "
        f"CasesTotal = :total, CasesPassed = :passed, CasesFailed = :failed, "
        f"CasesSkipped = :skipped, HardCheckRate = :hard, JudgeMeanScore = :judge, "
        f"JudgeExcluded = :excluded, Notes = :notes, "
        f"CostUsd = :cost, TokensIn = :tin, TokensOut = :tout, "
        f"UnpricedCalls = :unpriced, "
        # Wall clock, not the sum of the calls. A run that parallelises and one
        # that does not can burn identical model time and take very different
        # amounts of somebody's afternoon - and the eval gate is something a
        # person waits for.
        f"DurationMs = DATEDIFF(millisecond, StartedAt, SYSUTCDATETIME()) "
        f"WHERE EvalRunId = :id",
        {
            "id": run_id, "status": status,
            "total": totals.get("total", 0), "passed": totals.get("passed", 0),
            "failed": totals.get("failed", 0), "skipped": totals.get("skipped", 0),
            "hard": hard_rate, "judge": judge_mean, "excluded": judge_excluded,
            "notes": (notes or "")[:2000] or None,
            "cost": spend["cost"], "tin": spend["tin"], "tout": spend["tout"],
            "unpriced": spend["unpriced"],
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
