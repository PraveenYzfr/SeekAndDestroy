"""An answer that does not exist must not be graded as though it does.

WHAT RUN 44 REPORTED
--------------------
    hosting-app-aml-svc0648   Failed   answer len: 0
      FAIL   contains:APP-AML-SVC0648
      PASS   excludes:I don't have enough
      PASS   excludes:no information
      PASS   excludes:cannot answer

Three green checks on nothing. An empty string contains no hedge, so every
must_not_contain passes VACUOUSLY - and the more forbidden phrases a case
lists, the healthier a total absence of output looks. Identical on
hosting-app-aml-svc0655 and hosting-app-archive-export1089.

WHY THE ANSWER WAS EMPTY, and it was not a failure
--------------------------------------------------
The graph resolved the application, scored seven candidates, and stopped at the
human-review interrupt exactly as designed. No reviewer had decided, so there
was no final_report - and run_case builds the graded string from errors,
recommendation_explanations and final_report, all three of which are empty at
an interrupt.

    THE RUNNER COULD NOT GRADE ANY INVESTIGATION THAT PAUSES FOR REVIEW,
    and reported the pause as a quality failure.

Plan section 11 recorded this as "a real hosted application is not named in its
own answer". Both halves wrong: the requirement resolved the application
correctly, and there was no answer for it to be named in. Two people went
looking at retrieval and narration for a defect in neither.

TWO SEPARATE FIXES, and they are not alternatives
-------------------------------------------------
An empty answer is now ungradeable rather than graded - which finish() counts
as Skipped, because a case that could not be measured must not sit in the same
bucket as one that was measured and found wanting.

And the suite now plays the reviewer, approving the top-ranked candidate so the
report exists to grade. Reporting "not measured" for hosting would be honest,
and would silently remove the platform's main journey from the only suite that
measures quality.
"""

from __future__ import annotations

import sys
import types

from app.evaluation import golden_runner


class TestAnEmptyAnswerIsNotGraded:
    def test_no_check_passes_vacuously_on_an_empty_answer(self, monkeypatch):
        """The whole defect in one assertion. Before this, both excludes below
        reported PASS against a zero-length string."""
        result = _run_with_state(monkeypatch, {"investigation_type": "Hosting"})

        assert not any(h["passed"] for h in result["hard"]), (
            "a check passed against an answer that does not exist"
        )

    def test_it_is_reported_as_ungradeable_rather_than_failed(self, monkeypatch):
        """error is what the runner reads to count a case Skipped. A case the
        suite could not measure and a case the platform got wrong are different
        facts, and collapsing them is what makes a suite unactionable."""
        result = _run_with_state(monkeypatch, {"investigation_type": "Hosting"})
        assert "ungradeable" in (result.get("error") or "")

    def test_a_pause_says_it_paused_rather_than_that_it_produced_nothing(self, monkeypatch):
        """The two look identical from outside run_case and need different
        responses: one is a suite limitation, the other is a broken graph."""
        result = _run_with_state(
            monkeypatch,
            {"investigation_type": "Hosting", "__interrupt__": object(), "candidate_scores": []},
        )
        assert "paused" in result["error"]

    def test_an_empty_state_with_no_interrupt_says_so_instead(self, monkeypatch):
        """Not every empty answer is a pause, and calling one the other would
        send the reader to the wrong place."""
        result = _run_with_state(monkeypatch, {"investigation_type": "Question"})
        assert "paused" not in result["error"]
        assert "no errors, no explanations and no report" in result["error"]


class TestTheSuitePlaysTheReviewer:
    def test_a_paused_hosting_investigation_is_resumed_and_graded(self, monkeypatch):
        """The three hosting cases exist to grade a hosting recommendation. If
        the pause simply ends the case, the suite reports on everything except
        the thing the platform is for."""
        result = _run_with_state(
            monkeypatch,
            {
                "investigation_type": "Hosting",
                "__interrupt__": object(),
                "candidate_scores": [{"cluster_code": "atl-03"}],
            },
            resume_state={
                "investigation_type": "Hosting",
                "final_report": {"title": "Hosting investigation for: APP-AML-SVC0648"},
            },
            case={"id": "c", "query": "q", "must_contain": ["APP-AML-SVC0648"]},
        )
        assert result.get("auto_reviewed") is True
        assert "ungradeable" not in (result.get("error") or "")
        assert all(h["passed"] for h in result["hard"])

    def test_it_approves_the_top_ranked_candidate(self, monkeypatch):
        """Approving without naming an option leaves every recommendation
        PendingReview - three approved placements for one workload is the
        absence of a decision, not a decision."""
        seen: dict = {}
        _run_with_state(
            monkeypatch,
            {
                "investigation_type": "Hosting",
                "__interrupt__": object(),
                "candidate_scores": [{"cluster_code": "atl-03"}, {"cluster_code": "cmh-p212"}],
            },
            resume_state={"final_report": {"title": "t"}},
            capture=seen,
        )
        assert seen["resume"]["selected_cluster_code"] == "atl-03"
        assert seen["resume"]["decision"] == "Approved"

    def test_it_does_not_resume_when_nothing_was_scored(self, monkeypatch):
        """An interrupt with no candidates is not a review waiting to happen,
        and approving an empty shortlist would manufacture an outcome."""
        result = _run_with_state(
            monkeypatch,
            {"investigation_type": "Hosting", "__interrupt__": object(), "candidate_scores": []},
        )
        assert result.get("auto_reviewed") is not True
        assert "ungradeable" in result["error"]

    def test_a_failed_resume_leaves_the_case_ungradeable_not_wrong(self, monkeypatch):
        """A suite that cannot play the reviewer has failed to MEASURE the
        case. That is not the same as the case being wrong, and recording it as
        a failure would be the original defect wearing the other hat."""
        result = _run_with_state(
            monkeypatch,
            {
                "investigation_type": "Hosting",
                "__interrupt__": object(),
                "candidate_scores": [{"cluster_code": "atl-03"}],
            },
            resume_raises=RuntimeError("checkpointer gone"),
        )
        assert "ungradeable" in result["error"]
        assert "checkpointer gone" in result["resume_error"]


class TestTheRefusalWordingThatCostFourCases:
    """Run 44 counted five regressions. Four were correct refusals the phrase
    list did not recognise, all in the construction GROUNDED_QA_SYSTEM asks for.
    """

    def test_the_wording_the_prompt_instructs_is_recognised(self):
        assert golden_runner._refused(
            "The evidence does not contain any information about the build cost for clt-p125.",
            intercepted=False,
            investigation_type="Question",
        )

    def test_the_one_genuine_defect_must_not_turn_green(self):
        """THE TEST THIS CHANGE IS BUILT AROUND.

        unhosted-app-cards-api0753 is the suite's one real open defect: it named
        atl-p075 for an application with no ApplicationHosting row, citing an
        incident. All five must_refuse cases go through the same function, so a
        phrase list loose enough to fix the four false failures is loose enough
        to pass this one - and a grading change that turns a genuine defect
        green has gone too far, whatever it fixed on the way.

        The answer below declines in words and answers in substance. It is not a
        refusal.
        """
        assert not golden_runner._refused(
            "There is no formal hosting record for APP-CARDS-API0753 in the CMDB, and the "
            "evidence does not contain a definitive placement - but incident INC-4471 "
            "indicates it runs on atl-p075.",
            intercepted=False,
            investigation_type="Question",
            query="Which cluster is APP-CARDS-API0753 running on today?",
        )

    def test_a_refusal_may_still_echo_the_cluster_it_was_asked_about(self):
        """The other half, and without it the check would fail every refusal
        that names its own subject. "What did clt-p125 cost to build?" is
        declined by saying so about clt-p125 - the code is in the question, so
        repeating it volunteers nothing."""
        assert golden_runner._refused(
            "The evidence does not contain any information about the build cost for clt-p125.",
            intercepted=False,
            investigation_type="Question",
            query="What did clt-p125 cost to build?",
        )

    def test_a_real_answer_using_similar_words_is_not_read_as_a_refusal(self):
        """Matched on "evidence does not contain", not the looser "does not
        contain" - a genuine answer can say that about the estate, and reading
        it as a refusal would hide a case that answered when it should have
        declined."""
        assert not golden_runner._refused(
            "Cluster atl-03 does not contain any GPU nodes; the GPU clusters are cmh-p212 "
            "and den-p119.",
            intercepted=False,
            investigation_type="Question",
        )


class TestABaselineThatCannotResolveDoesNotSilentlyGate:
    """sad.EvalRun holds no row with Status='Passed' at all, so
    `--baseline last-passing` has never resolved. Comparing against nothing
    never finds a regression, so the gate reported success exactly when it was
    doing no work - failing open, in CI, on a green run nobody reads.
    """

    def test_fail_on_regression_with_no_passing_run_refuses(self, monkeypatch, capsys):
        repo = types.ModuleType("app.repositories.eval_run_repository")
        repo.last_passing = lambda suite: None
        _install_repo(monkeypatch, repo)
        monkeypatch.setattr(golden_runner, "load_cases", lambda: [{"id": "c", "query": "q"}])
        monkeypatch.setattr(sys, "argv", [
            "golden_runner", "--baseline", "last-passing", "--fail-on-regression",
        ])

        assert golden_runner.main() == 2
        assert "refusing to run" in capsys.readouterr().err

    def test_the_note_alone_is_kept_when_there_is_no_gate(self, monkeypatch, capsys):
        """An exploratory run that wanted a comparison and could not have one is
        still a useful run. Refusing it would help nobody - so this must NOT
        return 2, and must still say why there is no baseline."""
        repo = types.ModuleType("app.repositories.eval_run_repository")
        repo.last_passing = lambda suite: None
        repo.start = lambda *a, **k: 99
        repo.compare = lambda *a, **k: {}
        repo.verdict_for = lambda *a, **k: ("Passed", "nothing to compare")
        repo.finish = lambda *a, **k: None
        _install_repo(monkeypatch, repo)
        monkeypatch.setattr(golden_runner, "load_cases", lambda: [])
        monkeypatch.setattr(sys, "argv", ["golden_runner", "--baseline", "last-passing"])

        golden_runner.main()
        assert "no previous passing run" in capsys.readouterr().err


# --- helpers ----------------------------------------------------------------


def _install_repo(monkeypatch, stub):
    """Replace the eval-run repository everywhere the runner can reach it.

    sys.modules alone is not enough: the runner does `from app.repositories
    import eval_run_repository`, which reads the ATTRIBUTE off the already
    imported package rather than consulting sys.modules. Patching only the
    latter let the real repository through, and these tests wrote rows into a
    live EvalRun table - a test suite quietly recording fake evaluation runs
    being a neat miniature of the defect this file is about.
    """
    import app.repositories as pkg

    monkeypatch.setitem(sys.modules, "app.repositories.eval_run_repository", stub)
    monkeypatch.setattr(pkg, "eval_run_repository", stub, raising=False)


def _run_with_state(monkeypatch, state, *, resume_state=None, case=None,
                    capture=None, resume_raises=None):
    """Drive run_case against a stubbed graph.

    The graph is stubbed rather than run because this file is about what the
    RUNNER does with a state, not about what produces one. Running the real
    graph would need a database, a vector store and three model providers to
    assert something none of them decide.
    """
    calls = {"n": 0}

    class _Compiled:
        def invoke(self, arg, config=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return state
            if resume_raises:
                raise resume_raises
            if capture is not None:
                capture["resume"] = arg.resume
            return resume_state or {}

    graph_mod = types.ModuleType("app.graph.graph")
    graph_mod.get_compiled_graph = lambda: _Compiled()
    monkeypatch.setitem(sys.modules, "app.graph.graph", graph_mod)

    state_mod = types.ModuleType("app.graph.state")
    state_mod.new_state = lambda query, created_by=1: {}
    monkeypatch.setitem(sys.modules, "app.graph.state", state_mod)

    nodes_mod = types.ModuleType("app.graph.nodes")
    nodes_mod.quick_reply = lambda q: None
    monkeypatch.setitem(sys.modules, "app.graph.nodes", nodes_mod)

    factory = types.ModuleType("app.agents.llm_factory")
    factory.resolve_role = lambda role: {"provider": "p", "model": "m"}
    monkeypatch.setitem(sys.modules, "app.agents.llm_factory", factory)

    langgraph_types = types.ModuleType("langgraph.types")

    class _Command:
        def __init__(self, resume=None):
            self.resume = resume

    langgraph_types.Command = _Command
    monkeypatch.setitem(sys.modules, "langgraph.types", langgraph_types)

    return golden_runner.run_case(
        case or {"id": "c", "query": "q", "must_not_contain": ["no information", "cannot answer"]},
        use_judge=False,
    )
