"""An aborted run and a live one looked identical, permanently.

2026-09-06 00:55. The first golden baseline ever recorded against production
started. At 01:15 a deploy recreated ai-service; the process died at case 67 of
100 and its log went with the container. sad.EvalRun #27 sat in status Running
with no FinishedAt, and nothing would ever have revisited it.

Two costs, and the second is the one that bites tooling:

  - 67 partial cases can be read as a result, or pinned as a baseline, and the
    row gives no hint that they are partial;
  - a deploy guard asking this table "is an eval running?" would block EVERY
    future deploy on a run that died an hour ago. a2 wrote their guard against
    the host process list precisely to dodge that - which is right, and also
    leaves the table permanently wrong.
"""
from __future__ import annotations

from app.repositories import eval_run_repository as repo
from app.repositories.base import T, execute, fetch_one


def _age_run(run_id: int, minutes: int, *, beat: bool) -> None:
    """Backdate a run so the sweep sees it as old, via either column."""
    column = "HeartbeatAt" if beat else "StartedAt"
    other = "StartedAt" if beat else "HeartbeatAt"
    execute(
        f"UPDATE {T('EvalRun')} SET {column} = DATEADD(minute, -:m, SYSUTCDATETIME()), "
        f"{other} = {'DATEADD(minute, -:m, SYSUTCDATETIME())' if beat else 'NULL'} "
        f"WHERE EvalRunId = :id",
        {"m": minutes, "id": run_id},
    )


class TestAKilledRunStopsLookingAlive:
    def test_a_run_with_no_heartbeat_is_closed_as_error(self):
        dead = repo.start("golden", triggered_by="test")
        _age_run(dead, repo.STALE_AFTER_MINUTES + 5, beat=False)

        repo.reap_stale_runs()

        row = repo.get(dead)
        assert row["Status"] == "Error", "a killed run still read as Running"
        assert row["FinishedAt"] is not None

    def test_the_note_says_the_cases_are_partial(self):
        """The row is the only thing a later reader has. If it does not say the
        cases are incomplete, somebody pins 67 of 100 as a baseline."""
        dead = repo.start("golden", triggered_by="test")
        _age_run(dead, repo.STALE_AFTER_MINUTES + 5, beat=False)
        repo.reap_stale_runs()

        notes = repo.get(dead)["Notes"] or ""
        assert "PARTIAL" in notes
        assert "baseline" in notes.lower()

    def test_a_live_run_is_left_alone(self):
        """The whole point. Reaping a run that is merely slow would destroy real
        work - a full golden suite is 25 minutes of real model calls."""
        live = repo.start("golden", triggered_by="test")
        repo.record_case(live, case_id="c1", outcome="Passed")

        repo.reap_stale_runs()

        assert repo.get(live)["Status"] == "Running", "a live run was reaped"

    def test_recording_a_case_is_the_heartbeat(self):
        """No new discipline for the runner: it already calls record_case once
        per case. A separate heartbeat is one a future runner forgets, and its
        absence looks exactly like death."""
        run = repo.start("golden", triggered_by="test")
        assert repo.get(run)["HeartbeatAt"] is None

        repo.record_case(run, case_id="c1", outcome="Passed")
        assert repo.get(run)["HeartbeatAt"] is not None

    def test_a_run_that_beat_recently_survives_an_old_start(self):
        """A long suite is not a dead one. StartedAt alone would reap any run
        lasting longer than the threshold."""
        run = repo.start("golden", triggered_by="test")
        repo.record_case(run, case_id="c1", outcome="Passed")
        execute(
            f"UPDATE {T('EvalRun')} SET StartedAt = DATEADD(minute, -120, SYSUTCDATETIME()) "
            f"WHERE EvalRunId = :id", {"id": run},
        )

        repo.reap_stale_runs()

        assert repo.get(run)["Status"] == "Running", (
            "a two-hour run beating normally was killed"
        )

    def test_starting_a_run_reaps_the_previous_corpse(self):
        """Reaped where somebody is already looking at the table, so it needs no
        scheduler and cannot drift out of step with the code writing the rows."""
        dead = repo.start("golden", triggered_by="test")
        _age_run(dead, repo.STALE_AFTER_MINUTES + 5, beat=False)

        repo.start("golden", triggered_by="test")

        assert repo.get(dead)["Status"] == "Error"
