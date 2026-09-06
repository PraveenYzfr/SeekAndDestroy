"""A suite running on production has to be visible while it runs.

WHAT WENT WRONG
---------------
`--record` was opt-in. `record = args.record or bool(args.baseline)`, so the
plain invocation everybody actually types -

    docker exec -w /app docker-ai-service-1 python -m app.evaluation.golden_runner

- wrote NO row in sad.EvalRun. Not a partial row, not a Running row: nothing.

The deploy guard in scripts/deploy-app.sh asks that table whether a suite is in
flight. So on a box with a hundred cases executing it saw an idle machine and
let the deploy through. Run 27 died at case 67 that way, and run 40 after it.

Measured on prod: sixty seconds into a run, with cases plainly executing, the
latest EvalRunId was still 39.

WHY IT SURVIVED SO LONG
-----------------------
Both ends looked healthy. The runner prints its results identically whether or
not it records, and the guard correctly reported nothing running - because from
where it stood, nothing was. The comment in deploy-app.sh says the row is
written BEFORE the first case, which is true of the code path and says nothing
about whether that path is REACHED.

An invariant that holds only when somebody remembers a flag is not an
invariant. That is the general shape here, and it is the same one as a deploy
guard keyed on a process name written in another file: a promise held by
convention, with nothing failing when it breaks.

These tests assert the DEFAULT, because the default is the whole fix. Nothing
here checks that recording works - that is covered elsewhere - only that it
happens without being asked for.
"""

from __future__ import annotations

from app.evaluation import golden_runner


def _record_for(argv: list[str]) -> bool:
    """The decision under test, taken from the real parser.

    Reimplementing the expression here would test a copy of it, which is the
    failure mode this file exists for. Parsing the actual argv is what makes a
    future edit to either the flags or the expression visible.
    """
    parser = golden_runner.build_parser()
    args = parser.parse_args(argv)
    return golden_runner.recording_wanted(args)


class TestAFullRunRecordsItself:
    def test_the_bare_invocation_records(self):
        """The one assertion that would have saved runs 27 and 40. This is the
        command in the runbook and the one every operator types."""
        assert _record_for([]) is True

    def test_flags_that_are_not_about_recording_do_not_disable_it(self):
        """A run is no less real for being quiet or machine-read. --json in
        particular is what CI would use, and CI is exactly when nobody is
        watching the box."""
        for argv in (["--json"], ["--no-judge"], ["--fail-on-hard"],
                     ["--triggered-by", "ci"], ["--json", "--no-judge"]):
            assert _record_for(argv) is True, f"{argv} silently stopped recording"


class TestTheOptOutsAreDeliberate:
    def test_a_single_case_run_does_not_record(self):
        """Debugging one case takes seconds and does not need a place in
        history. A run history full of one-case rows makes the baseline harder
        to find, which is a real cost paid for no signal."""
        assert _record_for(["--case", "hosting-app-aml-svc0648"]) is False

    def test_a_single_case_can_still_be_recorded_on_request(self):
        """--record survives as the override, for the case where that one case
        is the thing being proved."""
        assert _record_for(["--case", "x", "--record"]) is True

    def test_no_record_turns_a_full_run_off(self):
        """The explicit escape hatch. Someone rehearsing the suite against a
        scratch database should be able to say so."""
        assert _record_for(["--no-record"]) is False

    def test_a_baseline_still_forces_recording_on(self):
        """A comparison needs both runs stored. Asking for one and throwing it
        away is not something anybody means."""
        assert _record_for(["--baseline", "last-passing"]) is True
        assert _record_for(["--baseline", "39"]) is True

    def test_a_baseline_beats_no_record_rather_than_silently_losing_the_run(self):
        """Contradictory flags. Recording wins, because the alternative is a
        --fail-on-regression gate comparing against a run that was never
        stored - which fails open, at the moment it is being trusted."""
        assert _record_for(["--baseline", "last-passing", "--no-record"]) is True


class TestTheGuardsPremiseHolds:
    def test_the_row_is_opened_before_the_case_loop(self):
        """deploy-app.sh refuses a deploy on a Running row and its comment says
        the row exists before case one. That sentence was true of the code and
        false in practice, because the branch holding it was not entered. This
        pins the ORDER, which is the half the comment is right about."""
        with open(golden_runner.__file__, encoding="utf-8") as handle:
            lines = handle.read().splitlines()

        start_line = next(i for i, ln in enumerate(lines) if "repo.start(" in ln)
        loop_line = next(i for i, ln in enumerate(lines) if ln.strip() == "for case in cases:")
        assert start_line < loop_line, (
            "repo.start() must run before the case loop - a suite that crashes "
            "on case one has to leave evidence it was attempted"
        )
