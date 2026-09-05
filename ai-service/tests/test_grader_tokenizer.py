"""Figures a model claimed, versus digits that live inside an identifier.

number_fidelity tokenises PROSE and asks whether each figure traces to the
evidence. Everything that is not a measurement therefore has to be removed
first, and this file is the record of what was not.

Two defects, both found by reading what the grader actually flagged in
production rather than by reasoning about the regex:

    inv 125  fidelity 0.000  ["10.4","185.2","10.4","185.4","4.50","19.50",...]
    inv 127  fidelity 0.410  ["2026","-24","35","2026","-25","26",...]

    "node msp-p194-NODE-02 (10.4.185.2) has 4.50 CPU cores"
        -> ['10.4', '185.2', '4.50']          an IP became two figures
    "Sev1 incident opened 2026-06-24T07:35:00"
        -> ['2026', '-06', '-24', '07', '35', '00']    six from one timestamp

The IP had no pattern at all. The timestamp had one that stopped at the "T":
each date branch ended in ``\\b``, and between "4" and "T" both characters are
word characters, so there is no boundary and the match was refused. The plain
date "2026-06-24" matched; the ISO form never did, and every incident in this
estate carries an ISO form.

WHAT THIS DID NOT FIX, stated because measuring it is the only reason it is
known: re-grading eleven historical answers moved the mean from 0.224 to 0.215.
The artefacts had been inflating the NUMERATOR as well - dates and IPs that
happened to match an evidence value counted as grounded - so removing them
exposed rather than repaired the residual. That residual is unexplained and is
not what these tests claim to close.

A third suspected defect - numeric suffixes on long application codes such as
APP-RISK-WORKER1135 - turned out not to exist. It is tested below anyway, so
nobody "fixes" a pattern that already works.
"""

from __future__ import annotations

import pytest

from app.evaluation import graders


class TestAnAddressIsNotAMeasurement:
    def test_an_ip_contributes_no_figures(self):
        assert graders._numbers_in("the node answers on 10.4.185.2") == []

    def test_a_real_figure_beside_an_ip_survives(self):
        """The fix must remove the address without taking the sentence's actual
        measurement with it - inv 125 quoted both in one line."""
        assert graders._numbers_in(
            "node msp-p194-NODE-02 (10.4.185.2) has 4.50 CPU cores"
        ) == ["4.50"]

    def test_a_decimal_that_is_not_an_address_is_still_a_figure(self):
        """Three dotted groups is a version or a typo, not a host. The pattern
        requires four so it cannot quietly swallow arbitrary decimals."""
        assert graders._numbers_in("utilisation reached 88.96 percent") == ["88.96"]


class TestATimestampIsNotAMeasurement:
    @pytest.mark.parametrize(
        "text",
        [
            "opened 2026-06-24T07:35:00, status Closed",
            "opened 2026-06-24T07:35:00.123Z",
            "opened 2026-06-24 07:35",
            "opened 2026-06-24 and closed 2026-06-25",
            "raised 01-07-2026",
            "the window ran 20:29 to 19:34",
        ],
    )
    def test_no_figures_survive_a_timestamp(self, text):
        assert graders._numbers_in(text) == []

    def test_the_iso_form_is_the_one_that_regressed(self):
        """The specific case: the plain date always matched, the ISO form never
        did, and the difference is a word boundary that cannot exist before a
        letter."""
        assert graders._DATE_RE.search("2026-06-24") is not None
        assert graders._DATE_RE.search("2026-06-24T07:35:00") is not None

    def test_a_ratio_is_not_a_clock(self):
        """The bare-time branch is the risky one - it must not swallow a
        genuine ratio. 75:25 is a read/write split, not twenty-five past
        seventy-five, and the minute group is constrained to [0-5]\\d so the
        hour cannot be 75."""
        assert graders._numbers_in("a 75:25 read/write split") == ["75", "25"]


class TestWhatAlreadyWorkedAndMustKeepWorking:
    """Each of these was a real defect once, fixed before this change, and each
    is one careless edit to the strip pipeline away from returning."""

    @pytest.mark.parametrize(
        "text",
        [
            "application APP-RISK-WORKER1135 was affected",   # suspected, not real
            "incident INC1009430 was resolved",
            "cluster den-p096 was recommended",
            "Den-p096 is recommended for this request",       # capitalised
            "RULE-011 blocked the placement",
            "a Tier-1 workload with a Sev2 incident",
        ],
    )
    def test_identifiers_contribute_no_figures(self, text):
        assert graders._numbers_in(text) == []

    def test_a_measurement_still_reaches_the_grader(self):
        assert graders._numbers_in("cluster den-p096 has 917.67 GB free") == ["917.67"]


class TestAnEvidenceObjectThatCanGroundNothing:
    """Structured is not the same as populated.

    evidence_is_structured checks SHAPE - a dict of short strings passes it. A
    Question-path answer is handed a placement-shaped envelope with every
    placement field empty, so it passes the shape check and then grounds
    nothing: measured on production, inv 104 yielded {104.0} and inv 125
    yielded {125.0} - the investigation's own id and nothing else.

    Absent is not zero, so that reports NOT APPLICABLE rather than a score.
    Widening the grounding set to read the retrieved documents those figures
    came from is the one repair not available: work notes are attacker-writable
    and that exclusion is the injection defence.
    """

    def test_an_empty_placement_envelope_is_not_applicable(self):
        evidence = {
            "investigation_id": 125,
            "top_candidates": [],
            "forecast_results": {},
            "capacity_calculations": {},
            "decision": None,
            "confidence": "Medium",
        }
        result = graders.number_fidelity("the cluster has 4.50 CPU cores", evidence)
        assert not result.applies, "an envelope grounding only its own id must not score"
        assert result.total == 0

    def test_the_investigation_id_alone_never_counts_as_grounding(self):
        """125 appearing in the prose because it is the investigation number
        must not make the evidence look usable."""
        evidence = {"investigation_id": 125, "top_candidates": []}
        assert not graders.number_fidelity("investigation 125 found 4.50 cores", evidence).applies

    def test_a_populated_envelope_is_still_graded(self):
        """The gate must not swallow the case the grader exists for."""
        evidence = {
            "investigation_id": 7,
            "top_candidates": [{"cluster_code": "den-p096", "overall_score": 88.96}],
        }
        result = graders.number_fidelity("den-p096 scored 88.96 overall", evidence)
        assert result.applies
        assert result.grounded == result.total

    def test_a_fabricated_figure_in_a_populated_envelope_is_still_caught(self):
        evidence = {
            "investigation_id": 7,
            "top_candidates": [{"cluster_code": "den-p096", "overall_score": 88.96}],
        }
        result = graders.number_fidelity("den-p096 scored 91.80 overall", evidence)
        assert result.applies
        assert "91.80" in result.ungrounded
