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

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "ai-service"))

GOLDEN_SET = REPO_ROOT / "ai-service" / "app" / "evaluation" / "golden_set.yaml"


def load_cases() -> list[dict]:
    import yaml

    data = yaml.safe_load(GOLDEN_SET.read_text(encoding="utf-8"))
    return data.get("cases", [])


def _refused(text: str, *, intercepted: bool) -> bool:
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
        passed = _refused(answer, intercepted=intercepted)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="run one case by id")
    parser.add_argument("--no-judge", action="store_true", help="deterministic checks only")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--fail-on-hard", action="store_true", help="exit non-zero on any hard-check failure")
    args = parser.parse_args()

    cases = load_cases()
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"no case with id {args.case!r}", file=sys.stderr)
            return 2

    results = [run_case(c, use_judge=not args.no_judge) for c in cases]

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

    if args.fail_on_hard and any(not h["passed"] for r in results for h in r["hard"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
