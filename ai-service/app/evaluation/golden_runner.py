"""Run the golden set and grade it, deterministically and with the judge.

    python scripts/eval_golden.py                  # run everything
    python scripts/eval_golden.py --case rejection-reason
    python scripts/eval_golden.py --no-judge       # deterministic only, no cost
    python scripts/eval_golden.py --json

As a gate in CI - exits non-zero when a hard check fails:

    python scripts/eval_golden.py --fail-on-hard

TWO GRADERS, DELIBERATELY NOT BLENDED
-------------------------------------
Hard checks are properties of the text - did it refuse, did it name the entity,
did it avoid the forbidden phrase. They are decided by string matching, and they
are the gate. A model is never asked to decide whether it refused.

The judge scores relevance, groundedness and actionability - things no rule can
see. It is reported beside the hard checks, never averaged into them, because a
1-5 opinion and a pass/fail property are not the same kind of fact and a single
blended number would hide which one failed.

WHY THIS COSTS MONEY AND scripts/evaluate.py DOES NOT
-----------------------------------------------------
evaluate.py grades calls that already happened, from sad.AgentAuditLog - a table
scan. This one *makes* calls: every case runs the real pipeline and then the
judge grades the output. Use --no-judge to halve that, and expect a real bill on
the full set.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: PACKAGE-RELATIVE, so this runs wherever the package does.
#:
#: It used to be REPO_ROOT / "ai-service" / "app" / "evaluation" / ... with
#: REPO_ROOT derived from the script's own location under scripts/. In a
#: container that resolved to /app/ai-service/app/evaluation/, which does not
#: exist: the image is built from ai-service/ as its context, so it ships app/ at
#: the root and has no scripts/ directory at all.
#:
#: So the suite could not run inside the deployed image, and the workaround was
#: to docker cp the script in and symlink /app/ai-service/app/evaluation ->
#: /app/app/evaluation on every deploy - scaffolding that the next container
#: recreate wiped, which is why "run the golden suite on prod" sat unstarted for
#: days without ever being the thing that blocked anyone.
#:
#: The yaml sits beside this file in the package and always has. Asking the
#: package where it is removes the repo layout from the question entirely.
_HERE = Path(__file__).resolve().parent

GOLDEN_SET = _HERE / "golden_set.yaml"

#: Only when running from a source checkout, where app/ is not yet importable.
#: Inside the image the package IS the working directory and this is a no-op.
_REPO_AI_SERVICE = _HERE.parents[1]
if str(_REPO_AI_SERVICE) not in sys.path:
    sys.path.insert(0, str(_REPO_AI_SERVICE))


def load_cases() -> list[dict]:
    import yaml

    data = yaml.safe_load(GOLDEN_SET.read_text(encoding="utf-8"))
    return data.get("cases", [])


def _refused(text: str, *, intercepted: bool, investigation_type: str | None = None) -> bool:
    """Whether the platform declined rather than answered.

    Structural first. When quick_reply intercepts a query, the platform has by
    definition refused to investigate it - no Investigation row, no model call,
    no graph. That is a fact about which code path ran, not an opinion about
    wording, and it cannot drift when somebody rewrites the refusal text.

    Phrase matching is the fallback, for refusals the graph itself produces -
    "the application is not in the CMDB" comes out of a full run. Those
    phrasings live in this repo rather than being generated, so matching them is
    reasonable, but it is second choice because it breaks silently when the copy
    changes. It already did: this list missed the scope refusal entirely because
    that text was rewritten to introduce the agent.
    """
    if intercepted:
        return True

    # THIRD STRUCTURAL SIGNAL, and the one that caught a false failure on the
    # first hundred-case run. "Delete the cluster atl-03" was refused perfectly:
    #
    #     title: "Request refused"
    #     executive_summary: "SeekAndDestroy produces recommendations only -
    #                         it never executes infrastructure changes."
    #
    # and this function reported it as ANSWERED, because none of the phrases
    # below appear in that wording. The gate would have failed the platform for
    # doing exactly the right thing - and with six adversarial cases in the set,
    # for doing it six times.
    #
    # classify_investigation_type returns REFUSED for these before any model is
    # called, so the type is a fact about which code path ran. That cannot drift
    # when somebody rewrites the copy, which the phrase list below demonstrably
    # does - its own docstring already records it missing the scope refusal for
    # the same reason.
    if (investigation_type or "").lower() in {"refused", "rejected"}:
        return True

    # Second structural signal, for refusals the graph itself produces.
    # GroundedAnswer carries citations and a confidence; an answer with no
    # citations and Low confidence has declined, whatever words it used. That is
    # a property of the schema rather than of the copy, so it survives a rewrite
    # of the wording - which the phrase list below did not: it missed "I have no
    # record of the firmware version" and reported a correct refusal as a
    # failure.
    try:
        parsed = json.loads(text[: text.index("]") + 1]) if text.lstrip().startswith("[") else None
    except (ValueError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict) and "citations" in item:
                if not item.get("citations") and str(item.get("confidence", "")).lower() == "low":
                    return True

    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "i don't have", "i do not have", "not enough grounded", "no information",
            "not found in", "cannot answer", "i do not answer", "not in the cmdb",
            "unable to", "no such", "does not exist", "no record", "no matching",
            "cannot state", "not available",
        )
    )


def run_case(case: dict, *, use_judge: bool) -> dict:
    from app.agents.llm_factory import resolve_role
    from app.graph.nodes import quick_reply

    query = case["query"]
    result: dict = {"id": case["id"], "kind": case.get("kind"), "query": query, "hard": [], "judge": None}

    # quick_reply intercepts input that should never reach the graph. When it
    # answers, that IS the answer - running the graph anyway would grade a
    # pipeline the platform deliberately did not use.
    answer = quick_reply(query)
    intercepted = answer is not None
    result["intercepted"] = intercepted
    evidence: object = {"note": "answered without running the graph"}
    author = {"provider": None, "model": None}

    if answer is None:
        try:
            from app.graph.graph import get_compiled_graph
            from app.graph.state import new_state

            # A thread_id the checkpointer can key on. Derived from the case id
            # rather than a counter, so re-running one case resumes its own
            # thread instead of colliding with whatever ran in that slot last
            # time - and so a golden-set run never lands on a real
            # investigation's thread.
            config = {"configurable": {"thread_id": f"golden-{case['id']}"}}
            state = get_compiled_graph().invoke(new_state(query, created_by=1), config=config)
            # Assemble what a caller actually receives, errors included. Reading
            # only the explanations made "APP-DOESNOTEXIST is not in the CMDB"
            # look like an empty answer - the platform had refused correctly and
            # the runner could not see it. A grader that cannot see a refusal
            # will report every correct refusal as a failure.
            parts: list[str] = []
            for err in state.get("errors") or []:
                parts.append(str(err))
            explanations = state.get("recommendation_explanations") or []
            if explanations:
                parts.append(json.dumps(explanations))
            report = state.get("final_report")
            if report:
                parts.append(report if isinstance(report, str) else json.dumps(report))
            answer = chr(10).join(parts)
            # Recorded so the refusal check can use it. A refusal is a
            # property of which path ran, not of the words chosen.
            result["investigation_type"] = state.get("investigation_type") or case.get("kind")
            evidence = state.get("retrieved_context") or state.get("candidate_scores") or {}
            resolved = resolve_role("grounded_qa")
            author = {"provider": resolved["provider"], "model": resolved["model"]}
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["hard"].append({"check": "ran", "passed": False, "detail": str(exc)[:200]})
            return result

    result["answer"] = answer[:2000]

    # --- hard checks: properties of the text, decided by string matching -----
    if case.get("must_refuse"):
        passed = _refused(
            answer, intercepted=intercepted,
            investigation_type=result.get("investigation_type"),
        )
        result["hard"].append(
            {"check": "must_refuse", "passed": passed,
             "detail": "" if passed else "answered instead of declining"}
        )
    for token in case.get("must_contain") or []:
        passed = token.lower() in answer.lower()
        result["hard"].append({"check": f"contains:{token}", "passed": passed, "detail": ""})
    for token in case.get("must_not_contain") or []:
        passed = token.lower() not in answer.lower()
        result["hard"].append({"check": f"excludes:{token}", "passed": passed, "detail": ""})

    # --- judge: what the hard checks cannot see -----------------------------
    if use_judge and answer:
        from app.evaluation.judge import judge_answer

        verdict = judge_answer(
            query, answer, evidence,
            author_provider=author["provider"], author_model=author["model"],
        )
        result["judge"] = {
            "model": f"{verdict.judge_provider}/{verdict.judge_model}",
            "self_judged": verdict.self_judged,
            "usable": verdict.usable,
            "error": verdict.error,
            "scores": None
            if verdict.verdict is None
            else {
                "relevance": verdict.verdict.relevance.score,
                "groundedness": verdict.verdict.groundedness.score,
                "actionability": verdict.verdict.actionability.score,
                "mean": verdict.verdict.mean_score,
            },
        }
    return result


def build_parser() -> argparse.ArgumentParser:
    """Split out of main() so the flags can be exercised without running a suite.

    Whether a run records itself decides whether the deploy guard can see it,
    which is not something to leave to a test that reimplements the expression
    and then agrees with itself.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="run one case by id")
    parser.add_argument("--no-judge", action="store_true", help="deterministic checks only")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--fail-on-hard", action="store_true", help="exit non-zero on any hard-check failure")
    parser.add_argument("--record", action="store_true",
                        help="store this run in sad.EvalRun - already the default for a "
                             "full-suite run; use this to record a --case run as well")
    parser.add_argument("--no-record", action="store_true",
                        help="do not store this run; a full-suite run is recorded by default")
    parser.add_argument("--baseline", metavar="RUN_ID|last-passing",
                        help="compare against a previous run; implies --record")
    parser.add_argument("--fail-on-regression", action="store_true",
                        help="exit non-zero when a case that passed in the baseline now fails")
    parser.add_argument("--triggered-by", default=None, help="who or what started this run")
    #  WHY THIS RUN IS THE BASELINE, in the row itself.
    #
    #  Notes already carries the machine verdict ("Passed - ..."), which says
    #  what happened and never why the run was made. A baseline is re-pinned
    #  when the RULER changed, and that fact lives nowhere else: on 2026-09-06
    #  three separate changes altered how number_fidelity is computed - a
    #  U+2011 dash fix, a not-applicable guard, and an absence column that
    #  changed what a NULL means - so any comparison spanning that day reads
    #  three changed rulers as a regression.
    #
    #  Appended to the verdict rather than replacing it, because both matter
    #  and the verdict is what --fail-on-regression is judged against.
    parser.add_argument("--note", default=None,
                        help="why this run was made - appended to the recorded verdict")
    return parser


def recording_wanted(args: argparse.Namespace) -> bool:
    # A comparison needs both runs stored, so asking for one turns recording on
    # rather than failing with an argument error. Nobody asks to compare against
    # a baseline and also wants this run thrown away.
    #
    # A FULL-SUITE RUN RECORDS ITSELF, and that is not a convenience.
    #
    # --record used to be opt-in, so `python -m app.evaluation.golden_runner`
    # wrote no EvalRun row at all - and everything that answers "is a suite
    # running" reads that table. The deploy guard in scripts/deploy-app.sh asks
    # it by name. So a hundred cases could be executing on prod while the guard
    # saw an idle box and let the deploy through. That is what killed run 27 at
    # case 67 and run 40, and it read as a flaky suite rather than as a guard
    # looking at the wrong thing, because a run nobody recorded leaves nothing
    # behind to argue with.
    #
    # The guard's comment says "the row is written BEFORE the first case". True,
    # and it was reached only when somebody remembered a flag. An invariant that
    # holds subject to an optional argument is not an invariant, and the failure
    # is invisible from both ends: the runner prints its results either way, and
    # the guard reports nothing running because nothing is - as far as it can see.
    #
    # A SINGLE CASE STAYS UNRECORDED. It is debugging, it takes seconds, and a
    # history filled with one-case runs makes the baseline harder to find rather
    # than the record more complete. --record forces it on when that one case is
    # worth keeping; --no-record turns a full run off for the same reason in
    # reverse. The default now matches what the run costs and who is watching it.
    return bool(args.record or args.baseline or not (args.no_record or args.case))


def main() -> int:
    args = build_parser().parse_args()
    record = recording_wanted(args)

    cases = load_cases()
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"no case with id {args.case!r}", file=sys.stderr)
            return 2

    repo = None
    run_id = None
    baseline_id = None
    if record:
        import time as _time

        from app.repositories import eval_run_repository as repo

        if args.baseline == "last-passing":
            previous = repo.last_passing("golden")
            baseline_id = previous["EvalRunId"] if previous else None
            if baseline_id is None:
                print("  no previous passing run to use as a baseline", file=sys.stderr)
        elif args.baseline:
            baseline_id = int(args.baseline)

        # Opened BEFORE the cases run, so a suite that crashes leaves a Running
        # row rather than no evidence it was attempted. A run that vanishes on
        # failure makes a flaky suite look like a suite nobody ran.
        run_id = repo.start("golden", triggered_by=args.triggered_by, baseline_run_id=baseline_id)
        print(f"  eval run #{run_id}" + (f", baseline #{baseline_id}" if baseline_id else ""))

    results = []
    for case in cases:
        started = _time.perf_counter() if record else 0.0
        result = run_case(case, use_judge=not args.no_judge)
        results.append(result)
        if not record:
            continue
        hard = result.get("hard") or []
        # A case that errored is SKIPPED, not failed. It tells you nothing about
        # quality, and counting it as a failure reports a measurement that was
        # never taken - the same distinction the graders make between "did not
        # apply" and "scored zero".
        if result.get("error"):
            outcome = "Skipped"
        elif any(not h["passed"] for h in hard):
            outcome = "Failed"
        else:
            outcome = "Passed"
        scores = (result.get("judge") or {}).get("scores") or {}
        repo.record_case(
            run_id, case_id=result["id"], kind=result.get("kind"), outcome=outcome,
            hard_checks=hard, answer_excerpt=result.get("answer"),
            error=result.get("error"),
            judge={
                "relevance": scores.get("relevance"),
                "groundedness": scores.get("groundedness"),
                "actionability": scores.get("actionability"),
                "self_judged": (result.get("judge") or {}).get("self_judged"),
            },
            duration_ms=int((_time.perf_counter() - started) * 1000),
        )

    if args.json:
        print(json.dumps({"results": results}, indent=2, default=str))
    else:
        print()
        for r in results:
            hard = r["hard"]
            failed = [h for h in hard if not h["passed"]]
            mark = "FAIL" if failed else "ok  "
            judge = r.get("judge") or {}
            scores = judge.get("scores")
            judge_text = ""
            if scores:
                judge_text = f"  judge {scores['mean']:.1f}/5"
                if judge.get("self_judged"):
                    # Said on every line, not in a footnote. A self-judged score
                    # that looks like an independent one is worse than no score.
                    judge_text += " (SELF-JUDGED, excluded)"
            elif judge.get("error"):
                judge_text = "  judge unavailable"
            print(f"  {mark} {r['id']:<26}{judge_text}")
            for h in failed:
                print(f"         {h['check']}: {h['detail'] or 'failed'}")

        usable = [r for r in results if (r.get("judge") or {}).get("usable")]
        total_hard = sum(len(r["hard"]) for r in results)
        failed_hard = sum(1 for r in results for h in r["hard"] if not h["passed"])
        print()
        print(f"  hard checks : {total_hard - failed_hard}/{total_hard} passed")
        if usable:
            mean = sum(r["judge"]["scores"]["mean"] for r in usable) / len(usable)
            print(f"  judge       : {mean:.2f}/5 over {len(usable)} independently judged case(s)")
        excluded = [r for r in results if (r.get("judge") or {}).get("self_judged")]
        if excluded:
            print(
                f"  excluded    : {len(excluded)} case(s) judged by the model that wrote them.\n"
                f"                Point the judge role at a different provider on the\n"
                f"                Model Settings screen to make these count."
            )
        print()

    hard_failures = sum(1 for r in results for h in r["hard"] if not h["passed"])

    comparison = {}
    status = "Passed"
    if record:
        outcomes = [
            "Skipped" if r.get("error")
            else ("Failed" if any(not h["passed"] for h in r["hard"]) else "Passed")
            for r in results
        ]
        usable = [r for r in results if (r.get("judge") or {}).get("usable")]
        excluded = sum(1 for r in results if (r.get("judge") or {}).get("self_judged"))
        total_hard = sum(len(r["hard"]) for r in results)

        if baseline_id:
            comparison = repo.compare(run_id, baseline_id)
        status, reason = repo.verdict_for(comparison, hard_failures=hard_failures)

        repo.finish(
            run_id, status=status,
            totals={
                "total": len(results),
                "passed": outcomes.count("Passed"),
                "failed": outcomes.count("Failed"),
                "skipped": outcomes.count("Skipped"),
            },
            hard_rate=(round((total_hard - hard_failures) / total_hard, 4) if total_hard else None),
            judge_mean=(round(sum(r["judge"]["scores"]["mean"] for r in usable) / len(usable), 2)
                        if usable else None),
            judge_excluded=excluded,
            notes=(f"{reason} | {args.note}" if args.note else reason),
        )
        print()
        print(f"  run #{run_id}: {status} - {reason}")
        if comparison:
            for label in ("regressed", "fixed", "added", "removed"):
                if comparison.get(label):
                    print(f"    {label:<10} {', '.join(comparison[label])}")

    # Two gates, deliberately separate. --fail-on-hard is an absolute floor: a
    # rule was broken and that is a bug. --fail-on-regression is relative: this
    # run is worse than a baseline while still inside every absolute limit, which
    # may be a trade somebody chose to make. Collapsing them into one flag would
    # force the same response to both.
    if args.fail_on_hard and hard_failures:
        return 1
    if args.fail_on_regression and comparison.get("regressed"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
