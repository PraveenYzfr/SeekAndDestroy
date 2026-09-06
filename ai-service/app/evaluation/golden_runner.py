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
import re
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


#: Real cluster codes: atl-03, cmh-p212, tok-p500. Same shape the conversation
#: resolver uses, and deliberately not a pattern invented here - a regex
#: describing a corpus somebody imagined has already shipped twice in this repo
#: and matched nothing either time.
_CLUSTER_IN_TEXT_RE = re.compile(r"\b[a-z]{3}-p?\d{2,3}\b", re.IGNORECASE)


def _named_a_cluster_not_asked_about(text: str, query: str) -> bool:
    """Whether the answer volunteered a cluster the question never mentioned.

    THE FOURTH STRUCTURAL SIGNAL, and it exists to stop the phrase list from
    passing a genuine defect.

    Every marker below is a string a WRONG answer can also contain. Given
    "Which cluster is APP-CARDS-API0753 running on today?" - an application with
    no hosting row - an answer reading "there is no formal hosting record, but
    incident INC-4471 places it on atl-p075" both declines in words and answers
    in substance. It named a cluster. That is not a refusal however it is
    phrased, and grading it as one turns the suite's one genuine open defect
    green, which is worse than the false failures this change set out to fix.

    NOT ASKED ABOUT is the whole point. A correct refusal echoes the subject -
    "the CMDB holds no build cost for clt-p125" names clt-p125 because the
    question did. Only a code the question never contained is the platform
    volunteering an answer.
    """
    asked = {m.lower() for m in _CLUSTER_IN_TEXT_RE.findall(query)}
    return any(m.lower() not in asked for m in _CLUSTER_IN_TEXT_RE.findall(text))


def _refused(text: str, *, intercepted: bool, investigation_type: str | None = None,
             query: str = "") -> bool:
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

    #  OVERRULES EVERY SIGNAL BELOW, which is why it sits above them.
    #
    #  Both remaining checks can fire on an answer that is wrong. A hedged wrong
    #  answer may carry no citations and Low confidence, and any phrase in the
    #  list can appear in prose that also names a cluster. When a code the
    #  question never mentioned is present, the naming is the load-bearing half.
    if query and _named_a_cluster_not_asked_about(text, query):
        return False

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
            #  THE SHAPE GROUNDED_QA_SYSTEM ACTUALLY ASKS FOR, added after this
            #  list missed it on four cases of run 44:
            #
            #      "The evidence does not contain any information about the
            #       build cost for clt-p125"
            #
            #  unanswerable-build-cost, unanswerable-vendor-contact,
            #  unknown-cluster-mars-01 and unknown-cluster-tok-p500 all refused
            #  correctly and were all graded "answered instead of declining".
            #  Four of the run's five regressions were this, and none of them
            #  was a regression.
            #
            #  Matched on "evidence does not contain" rather than the looser
            #  "does not contain", which a real answer can legitimately say
            #  ("cluster atl-03 does not contain any GPU nodes"). The prompt
            #  instructs this exact construction for the no-evidence case, so
            #  it is as close to a structural signal as prose gets.
            #
            #  STILL A FALLBACK, and this is its fourth miss - after the scope
            #  refusal, the "Request refused" report, and "I have no record of
            #  the firmware version". Each was fixed by adding a string, which
            #  is why the two structural checks above exist and why the next
            #  fix should be a third one rather than a fifth string.
            "evidence does not contain", "not contain any information",
        )
    )


def _answer_from(state: dict) -> str:
    """What a caller actually receives, errors included.

    Reading only the explanations made "APP-DOESNOTEXIST is not in the CMDB"
    look like an empty answer - the platform had refused correctly and the
    runner could not see it. A grader that cannot see a refusal reports every
    correct refusal as a failure.

    One function because the state is assembled twice: once as invoke() returns
    it, and again after the suite resumes a paused review. Two copies of this
    would drift, and the half that drifted would be the one nobody reads.
    """
    parts: list[str] = []
    for err in state.get("errors") or []:
        parts.append(str(err))
    explanations = state.get("recommendation_explanations") or []
    if explanations:
        parts.append(json.dumps(explanations))
    report = state.get("final_report")
    if report:
        parts.append(report if isinstance(report, str) else json.dumps(report))
    return chr(10).join(parts)


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
            answer = _answer_from(state)
            # Recorded so the refusal check can use it. A refusal is a
            # property of which path ran, not of the words chosen.
            result["investigation_type"] = state.get("investigation_type") or case.get("kind")
            #  A PAUSE IS NOT A FAILURE, and only the state can tell them apart.
            #
            #  invoke() returns when the graph interrupts for human review, and
            #  the state it returns looks identical to a crash from out here:
            #  no report, no explanations, no errors. __interrupt__ is the one
            #  thing that distinguishes "stopped because it was designed to"
            #  from "stopped because it broke", so it is read here rather than
            #  inferred from the emptiness downstream.
            result["paused"] = "__interrupt__" in state
            result["candidates_scored"] = len(state.get("candidate_scores") or [])

            #  THE SUITE HAS TO PLAY THE REVIEWER, or a whole investigation
            #  type is never graded.
            #
            #  Hosting stops for human review by design. Left there, the three
            #  hosting cases in the golden set are permanently ungradeable -
            #  honest, and it silently removes the platform's main journey from
            #  the only suite that measures quality. Reporting "not measured"
            #  for the thing under test is not neutrality, it is a gap that
            #  reads as coverage.
            #
            #  So the runner approves the top-ranked candidate, which is the
            #  option the review screen offers by default, and grades the report
            #  that produces. It is graded as a REVIEWED answer and the result
            #  says so, because a report that exists only because the suite
            #  approved it must not be mistaken for one a person signed off.
            #
            #  Bounded deliberately: only on an interrupt, only when candidates
            #  were actually scored, and a failure to resume leaves the case
            #  ungradeable rather than inventing an outcome for it.
            if result["paused"] and result["candidates_scored"]:
                try:
                    from langgraph.types import Command

                    top = (state.get("candidate_scores") or [{}])[0]
                    state = get_compiled_graph().invoke(
                        Command(resume={
                            "decision": "Approved",
                            "reviewer_employee_id": 1,
                            "comments": "auto-approved by the golden suite to grade the report",
                            "selected_cluster_code": top.get("cluster_code"),
                            "selected_host_name": top.get("host_name"),
                        }),
                        config=config,
                    )
                    result["auto_reviewed"] = True
                    answer = _answer_from(state)
                    evidence = state.get("retrieved_context") or state.get("candidate_scores") or {}
                except Exception as exc:  # noqa: BLE001
                    #  Swallowed on purpose. A suite that cannot play the
                    #  reviewer has failed to MEASURE the case, which is not the
                    #  same as the case being wrong - the empty-answer branch
                    #  below reports it as ungradeable and says why.
                    result["resume_error"] = f"{type(exc).__name__}: {exc}"[:200]
            evidence = state.get("retrieved_context") or state.get("candidate_scores") or {}
            resolved = resolve_role("grounded_qa")
            author = {"provider": resolved["provider"], "model": resolved["model"]}
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["hard"].append({"check": "ran", "passed": False, "detail": str(exc)[:200]})
            return result

    result["answer"] = answer[:2000]

    #  AN EMPTY ANSWER IS NOT A BADLY WRITTEN ONE, and grading it produces
    #  confident nonsense in both directions at once.
    #
    #  Measured on run 44, three cases, identical shape:
    #
    #      hosting-app-aml-svc0648   Failed   answer len: 0
    #        FAIL   contains:APP-AML-SVC0648
    #        PASS   excludes:I don't have enough
    #        PASS   excludes:no information
    #        PASS   excludes:cannot answer
    #
    #  Three green checks on nothing. An empty string contains no hedge, so
    #  every must_not_contain passes VACUOUSLY - the more forbidden phrases a
    #  case lists, the healthier a total absence of output looks. That is the
    #  defect this codebase keeps meeting: something unmeasurable reported as a
    #  measurement, and here it reports in the reassuring direction.
    #
    #  WHY THOSE THREE ARE EMPTY, and it is not a failure. The graph resolved
    #  the application, scored seven candidates, and stopped at the human-review
    #  interrupt exactly as designed. No reviewer has decided, so there is no
    #  final_report, no explanations and no errors - and run_case builds the
    #  graded string from precisely those three. The runner could not grade any
    #  investigation that pauses for review, and reported the pause as a quality
    #  failure. Plan section 11 called this "a real hosted application is not
    #  named in its own answer"; it is named, and there is no answer to name it
    #  in. Two people looked at retrieval and narration for a defect in neither.
    #
    #  Skipped, not Failed. finish() already counts Skipped separately, and a
    #  case that could not be graded must not sit in the same bucket as one that
    #  was graded and found wanting - the whole point of the run is to tell
    #  those apart.
    if not answer.strip():
        why = (
            "paused at the human-review interrupt with "
            f"{result.get('candidates_scored', 0)} candidates scored - no reviewer has "
            "decided, so there is no report to grade"
            if result.get("paused")
            else "the graph returned no errors, no explanations and no report"
        )
        result["error"] = f"ungradeable: {why}"
        return result

    # --- hard checks: properties of the text, decided by string matching -----
    if case.get("must_refuse"):
        passed = _refused(
            answer, intercepted=intercepted,
            investigation_type=result.get("investigation_type"),
            query=query,
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
                #  A GATE WITH NO BASELINE MUST NOT PASS.
                #
                #  This printed a note and carried on. With --fail-on-regression
                #  that is a regression gate comparing against nothing, and
                #  comparing against nothing never finds a regression - so the
                #  gate reports success at precisely the moment it is doing no
                #  work. It fails OPEN, silently, in CI, where nobody reads
                #  stderr on a green run.
                #
                #  Not hypothetical: sad.EvalRun holds no row with
                #  Status='Passed' at all, so `--baseline last-passing` has
                #  never once resolved. Every run made with it was ungated.
                #
                #  Without the gate the note is enough - an exploratory run that
                #  wanted a comparison and could not have one is still a useful
                #  run, and refusing it would help nobody.
                print("  no previous passing run to use as a baseline", file=sys.stderr)
                if args.fail_on_regression:
                    print(
                        "  refusing to run: --fail-on-regression with --baseline last-passing, "
                        "and no run has ever passed. Nothing to compare against is not the same "
                        "as nothing to report. Name a baseline explicitly (--baseline <run id>) "
                        "or drop --fail-on-regression.",
                        file=sys.stderr,
                    )
                    return 2
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
