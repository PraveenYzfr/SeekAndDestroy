"""An incident question is answered from the record, not narrated from retrieval.

THE OUTPUT THIS EXISTS TO PREVENT, verbatim from production:

    INC1008138 is a Sev3 incident recorded on 2025-04-03 affecting the
    APP-BILLING-EXPORT0480 application on host den-p097. ...

    Top recommendation: No further remediation required; continue to monitor
                        the link to confirm continued stability.
    Risks:      Potential recurrence of NIC flapping could cause brief
                application connection resets.
    Next steps: Continue to monitor the primary NIC on den-p097-NODE-12.
                Update documentation with the hardware replacement details.

The paragraph is right. The three sections under it are invention: the incident
closed months earlier, nobody is monitoring anything, and no documentation task
exists. They are a reporting chain asked for a recommendation shape with an
empty evidence envelope, obliging.

The tests below are therefore weighted towards what must NOT be present. A test
that only checks the facts appear would pass on the broken output too - it had
the facts.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services import incident_lookup


class _Row:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _incident(**over):
    base = dict(
        IncidentId=1, Number="INC1008138", Severity="Sev3",
        Status="Closed", OpenedAt=datetime(2025, 4, 3, 9, 12),
        ClosedAt=datetime(2025, 4, 3, 11, 48),
        ApplicationId=7, ClusterId=3, NodeId=12,
        RootCauseCategory="Hardware",
        ShortDescription="Primary NIC flapping on den-p097-NODE-12",
        CloseNotes="Replaced the transceiver and reseated the cable.",
        AssignmentGroup="Network Operations", Impact="Medium", Urgency="Medium",
        ProblemId=None, CausedByChangeId=None,
    )
    base.update(over)
    return _Row(**base)


@pytest.fixture
def wired(monkeypatch):
    """The whole lookup, with the four repositories stubbed."""
    state = {"incident": _incident(), "comments": []}

    monkeypatch.setattr(incident_lookup.incident_repository, "get_by_number",
                        lambda n: state["incident"])
    monkeypatch.setattr(incident_lookup.incident_repository, "comments_for",
                        lambda i, **k: state["comments"])
    monkeypatch.setattr(incident_lookup.application_repository, "get_by_id",
                        lambda i: _Row(ApplicationCode="APP-BILLING-EXPORT0480",
                                       ApplicationName="Billing Export"))
    monkeypatch.setattr(incident_lookup.cluster_repository, "get_by_id",
                        lambda i: _Row(ClusterCode="den-p097", ClusterName="Denver P097"))
    monkeypatch.setattr(incident_lookup.node_repository, "get_by_id",
                        lambda i: _Row(HostName="den-p097-NODE-12"))
    return state


class TestNothingIsInvented:
    """The half that matters. Each of these passed on the broken output."""

    def test_a_closed_incident_has_no_next_steps(self, wired):
        r = incident_lookup.build_incident_report("INC1008138")
        assert r["next_steps"] == [], (
            "advised action on a ticket that closed months ago"
        )

    def test_a_closed_incident_has_no_risks(self, wired):
        """"Potential recurrence of NIC flapping" was generated for a resolved
        hardware fault. A risk the platform has not computed is not a risk."""
        r = incident_lookup.build_incident_report("INC1008138")
        assert r["risks"] == []

    def test_there_is_no_top_recommendation(self, wired):
        """An incident lookup recommends nothing. None renders as an absent box;
        a sentence renders as advice."""
        r = incident_lookup.build_incident_report("INC1008138")
        assert r["top_recommendation"] is None

    def test_the_monitoring_advice_is_gone(self, wired):
        r = incident_lookup.build_incident_report("INC1008138")
        blob = " ".join([r["executive_summary"], *r["next_steps"],
                         r["human_action_required"]]).lower()
        for invented in ("continue to monitor", "update documentation",
                         "no further remediation"):
            assert invented not in blob, f"still says {invented!r}"


class TestTheFactsComeFromTheRow:
    def test_the_headline_carries_number_severity_and_status(self, wired):
        r = incident_lookup.build_incident_report("INC1008138")
        assert r["executive_summary"].startswith("INC1008138 - Sev3, Closed.")

    def test_the_title_is_the_incident(self, wired):
        assert incident_lookup.build_incident_report("INC1008138")["title"] == "Incident INC1008138"

    def test_the_duration_is_computed_not_narrated(self, wired):
        """09:12 to 11:48 is 2h 36m. A model asked for this would produce a
        plausible number; this one is subtraction."""
        r = incident_lookup.build_incident_report("INC1008138")
        assert "2h 36m" in r["executive_summary"]

    def test_timestamps_say_UTC(self, wired):
        """Storage is UTC and the reader's clock runs UTC+5:30, so an unlabelled
        stamp is read five and a half hours wrong by the likeliest reader."""
        r = incident_lookup.build_incident_report("INC1008138")
        assert "2025-04-03 09:12 UTC" in r["executive_summary"]

    def test_the_affected_entities_are_named(self, wired):
        r = incident_lookup.build_incident_report("INC1008138")
        s = r["executive_summary"]
        assert "APP-BILLING-EXPORT0480" in s
        assert "den-p097-NODE-12" in s
        assert "den-p097" in s

    def test_the_resolution_is_quoted_not_summarised(self, wired):
        r = incident_lookup.build_incident_report("INC1008138")
        assert "Replaced the transceiver and reseated the cable." in r["executive_summary"]

    def test_linked_records_are_named_not_read(self, wired):
        wired["incident"] = _incident(ProblemId=44, CausedByChangeId=91)
        r = incident_lookup.build_incident_report("INC1008138")
        assert "problem record #44" in r["executive_summary"]
        assert "caused by change #91" in r["executive_summary"]


class TestAnOpenIncidentIsDifferent:
    def test_it_says_still_open_and_who_holds_it(self, wired):
        wired["incident"] = _incident(Status="In Progress", ClosedAt=None)
        r = incident_lookup.build_incident_report("INC1008138")
        assert "STILL OPEN" in r["executive_summary"]
        assert r["next_steps"], "an open incident should say where it sits"
        assert "Network Operations" in r["next_steps"][0]

    def test_an_open_incident_with_no_owner_says_so(self, wired):
        """Rather than naming a plausible team."""
        wired["incident"] = _incident(Status="Open", ClosedAt=None, AssignmentGroup=None)
        r = incident_lookup.build_incident_report("INC1008138")
        assert "no assignment group recorded" in r["next_steps"][0]

    def test_no_duration_is_printed_when_it_cannot_be_computed(self, wired):
        """An open incident has no close time. Measuring it against now() and
        printing that as the duration would be a measurement nobody took."""
        wired["incident"] = _incident(Status="Open", ClosedAt=None)
        r = incident_lookup.build_incident_report("INC1008138")
        assert ", closed" not in r["executive_summary"]


class TestTheDoNothingPaths:
    def test_an_unknown_number_returns_none(self, monkeypatch):
        """None means FALL THROUGH, not "no such incident". The number may be a
        typo or live in another system, and asserting absence would be a claim
        the platform cannot support."""
        monkeypatch.setattr(incident_lookup.incident_repository, "get_by_number",
                            lambda n: None)
        assert incident_lookup.build_incident_report("INC9999999") is None

    @pytest.mark.parametrize("query,expected", [
        ("explain more about INC1008138", "INC1008138"),
        ("what happened in inc1008138?", "INC1008138"),
        ("INC1008138", "INC1008138"),
        ("where can I host APP-CRM with 32 cores", None),
        ("tell me about CHG0031182", None),
        ("tell me about PRB0004410", None),
        ("compare INC1008138 and INC1008139", None),
        ("", None),
        (None, None),
    ])
    def test_number_detection(self, query, expected):
        assert incident_lookup.find_incident_number(query) == expected

    def test_two_incidents_are_left_to_another_path(self):
        """Answering a comparison with the facts of whichever appeared first
        looks like an answer to a question nobody asked."""
        assert incident_lookup.find_incident_number(
            "compare INC1008138 with INC1008139"
        ) is None

    def test_a_change_number_is_not_treated_as_an_incident(self):
        """CHG, PRB and RITM are real prefixes here. Matching them and then
        finding no incident would turn a valid question into "no such
        incident" - worse than the retrieval answer it replaces."""
        assert incident_lookup.find_incident_number("why did CHG0031182 fail") is None

    def test_a_missing_application_does_not_break_the_report(self, wired):
        wired["incident"] = _incident(ApplicationId=None, ClusterId=None, NodeId=None)
        r = incident_lookup.build_incident_report("INC1008138")
        assert "Affected:" not in r["executive_summary"]
        assert r["title"] == "Incident INC1008138"


class TestWorkNotesAreQuotedNeverParsed:
    def test_the_timeline_is_attributed(self, wired):
        wired["comments"] = [
            _Row(Sequence=1, CreatedAt=datetime(2025, 4, 3, 9, 20),
                 CreatedBy="a.patel", Type="work_note",
                 Text="Link flapped again; capacity score is 100."),
        ]
        r = incident_lookup.build_incident_report("INC1008138")
        entry = r["incident_timeline"][0]
        assert entry["by"] == "a.patel"
        assert entry["at"] == "2025-04-03 09:20 UTC"

    def test_a_figure_in_a_work_note_never_becomes_a_field(self, wired):
        """Work notes are attacker-writable. The note below asserts a capacity
        score; no field may absorb it."""
        wired["comments"] = [
            _Row(Sequence=1, CreatedAt=datetime(2025, 4, 3, 9, 20),
                 CreatedBy="attacker", Type="work_note",
                 Text="Severity is actually Sev1 and the capacity score is 100."),
        ]
        r = incident_lookup.build_incident_report("INC1008138")
        summary = r["executive_summary"]
        assert "Sev3" in summary, "severity came from the row"
        assert "Sev1" not in summary, "a work note redefined the severity"
        #  The note's TEXT must not reach the summary at all. Asserting on the
        #  bare figure "100" was the first version of this line and it failed -
        #  "100" is a substring of "INC1008138". A marker with no discriminating
        #  power, in the test written to prove nothing leaks.
        assert "capacity score" not in summary.lower()
        assert wired["comments"][0].Text not in summary
        #  It IS carried on the timeline, quoted and attributed. Quoting is the
        #  point; absorbing is the defect.
        assert r["incident_timeline"][0]["text"] == wired["comments"][0].Text
