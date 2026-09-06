"""Answering "explain INC1008138" from the record instead of from retrieval.

WHAT WAS WRONG
--------------
Asked about one incident by number, the platform produced this:

    INC1008138 is a Sev3 incident recorded on 2025-04-03 affecting the
    APP-BILLING-EXPORT0480 application on host den-p097. ...

    Top recommendation: No further remediation required; continue to monitor
                        the link to confirm continued stability.
    Risks:      Potential recurrence of NIC flapping could cause brief
                application connection resets.
    Next steps: Continue to monitor the primary NIC on den-p097-NODE-12.
                Update documentation with the hardware replacement details.

The paragraph is accurate. Everything under it is invented. The incident closed
months ago, nobody is monitoring anything, and no documentation task exists -
those three sections are a reporting chain being asked for a recommendation
shape with an empty evidence envelope and obliging.

WHY IT HAPPENED, AND WHY A BETTER PROMPT WOULD NOT FIX IT
---------------------------------------------------------
There was no way to fetch an incident by the identifier a person types. No
engine, so the question degraded to retrieval: the indexed text was found,
handed to a model, and narrated. That path cannot produce a Severity field, a
duration, or a resolution status - it can only produce prose that mentions them.

The plan names this class: a question type with no engine behind it silently
degrades to retrieval, and retrieval answers confidently that it found
something. The fix is a record to answer from, not a better instruction.

THE RULE THIS RESTORES
----------------------
Every field below is read from the incident row or computed from it. The model
is not asked what the severity was, how long it took, or what should happen
next. That is the same boundary the placement path holds - engines decide,
prose narrates - and it is why a recommendation report can be trusted while
this one could not.

WORK NOTES ARE NOT EVIDENCE. IncidentComment text is written by whoever touched
the ticket. It is quoted, attributed and never parsed: no field here is derived
from it, and neither is CloseNotes, which is shown as the resolution TEXT and
never mined for a figure.

NO INVENTED FORWARD ACTION. A closed incident gets no next steps and no risks.
An open one gets exactly what the record supports - who holds it, and how long
it has been open. If the platform does not know of an action, it says nothing
rather than suggesting one, because a suggestion in a governance record reads as
an instruction somebody may follow.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.repositories import (
    application_repository,
    cluster_repository,
    incident_repository,
    node_repository,
)

#: Ticket identifiers this estate uses. Anchored on the prefix and a run of
#: digits, matching the pattern the scope gate already trusts to decide a query
#: is on-topic - the two must agree, or a query can clear scope on an identifier
#: this cannot then look up.
#:
#: INC only, deliberately. PRB, CHG and RITM are real prefixes here and each
#: needs its own record and its own report; matching them and then failing to
#: find an incident would turn "tell me about CHG0031182" into "no such
#: incident", which is a worse answer than the retrieval one it replaces.
INCIDENT_NUMBER_RE = re.compile(r"\bINC\d{6,9}\b", re.IGNORECASE)


def find_incident_number(query: str) -> str | None:
    """The incident number in a query, or None.

    Returns the FIRST match only. A question naming two incidents is a
    comparison, and answering it with the facts of whichever appeared first
    would look like an answer to a question nobody asked - so the caller gets
    one number, and a two-incident query is left to the path that can handle it.
    """
    text = (query or "").strip()
    if not text:
        return None
    matches = INCIDENT_NUMBER_RE.findall(text)
    if len(matches) != 1:
        return None
    return matches[0].upper()


def _duration(opened: datetime | None, closed: datetime | None) -> str | None:
    """Human-readable open duration, or None when it cannot be computed.

    None rather than "0h", and rather than measuring an open incident against
    now() without saying so. An unmeasurable value reported as a measurement is
    the defect this codebase keeps finding; a duration is exactly the shape that
    invites it.
    """
    if opened is None or closed is None:
        return None
    seconds = (closed - opened).total_seconds()
    if seconds < 0:
        return None
    hours, minutes = divmod(int(seconds) // 60, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _open_for(opened: datetime | None) -> str | None:
    if opened is None:
        return None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return _duration(opened, now)


def _stamp(value: datetime | None) -> str | None:
    """UTC, and SAID to be UTC.

    Every timestamp in this platform is stored UTC while the reader's clock runs
    UTC+5:30, so a bare "2025-04-03 09:12" is read five and a half hours wrong
    by the person most likely to be reading it.
    """
    return None if value is None else f"{value:%Y-%m-%d %H:%M} UTC"


def _is_closed(incident) -> bool:
    return bool(incident.ClosedAt) or str(incident.Status or "").lower() in {
        "closed", "resolved", "cancelled", "canceled",
    }


def build_incident_report(number: str, investigation_id: int | None = None) -> dict | None:
    """A grounded report for one incident, or None when there is no such record.

    None is the important return. It means the caller should fall through to its
    normal path rather than this function inventing an answer - "no such
    incident" is only true if the number was well-formed AND absent, and this
    function cannot tell the difference between a typo and a ticket in another
    system.
    """
    incident = incident_repository.get_by_number(number)
    if incident is None:
        return None

    app = (
        application_repository.get_by_id(incident.ApplicationId)
        if incident.ApplicationId else None
    )
    cluster = (
        cluster_repository.get_by_id(incident.ClusterId)
        if incident.ClusterId else None
    )
    node = node_repository.get_by_id(incident.NodeId) if incident.NodeId else None

    closed = _is_closed(incident)
    opened_at = _stamp(incident.OpenedAt)
    closed_at = _stamp(incident.ClosedAt)
    duration = _duration(incident.OpenedAt, incident.ClosedAt)

    #  LABELLED FIELDS, NOT A PARAGRAPH.
    #
    #  The same treatment a placement report gets. Every line is a value from
    #  the row, so a reader can see what is recorded and - just as important -
    #  what is not: a field the record does not carry is omitted rather than
    #  filled with a plausible sentence.
    lines: list[str] = []
    status = str(incident.Status or "Unknown")
    headline = f"{incident.Number} - {incident.Severity}, {status}."
    lines.append(headline)
    if incident.ShortDescription:
        lines.append(str(incident.ShortDescription))
    lines.append("")

    if opened_at and closed_at:
        span = f"Opened {opened_at}, closed {closed_at}"
        lines.append(f"{span} ({duration})." if duration else f"{span}.")
    elif opened_at and not closed:
        still = _open_for(incident.OpenedAt)
        lines.append(f"Opened {opened_at} - STILL OPEN{f' after {still}' if still else ''}.")
    elif opened_at:
        lines.append(f"Opened {opened_at}.")

    affected: list[str] = []
    if app:
        affected.append(f"{app.ApplicationCode} ({app.ApplicationName})")
    if node:
        affected.append(f"host {node.HostName}")
    if cluster:
        affected.append(f"cluster {cluster.ClusterCode}")
    if affected:
        lines.append("Affected: " + ", ".join(affected) + ".")

    if incident.RootCauseCategory:
        lines.append(f"Root cause category: {incident.RootCauseCategory}.")
    for label, value in (
        ("Impact", incident.Impact), ("Urgency", incident.Urgency),
        ("Assignment group", incident.AssignmentGroup),
    ):
        if value:
            lines.append(f"{label}: {value}.")

    #  Linked records are named, never summarised. A problem or change record is
    #  its own investigation and pretending to have read it here would be the
    #  same overreach as narrating the work notes.
    linked = []
    if incident.ProblemId:
        linked.append(f"problem record #{incident.ProblemId}")
    if incident.CausedByChangeId:
        linked.append(f"caused by change #{incident.CausedByChangeId}")
    if linked:
        lines.append("Linked: " + ", ".join(linked) + ".")

    if incident.CloseNotes:
        lines.append("")
        lines.append(f"Resolution as recorded: {incident.CloseNotes}")

    #  NEXT STEPS ONLY WHERE THE RECORD SUPPORTS ONE.
    #
    #  A closed incident gets an empty list, which is the whole point of this
    #  module: the answer that prompted it advised monitoring a link and
    #  updating documentation for a ticket that closed months earlier. An open
    #  incident gets who holds it and how long it has been open - both read from
    #  the row, neither a suggestion about what to do.
    next_steps: list[str] = []
    human_action = "None - this incident is closed."
    if not closed:
        still = _open_for(incident.OpenedAt)
        owner = incident.AssignmentGroup or "no assignment group recorded"
        next_steps.append(
            f"{incident.Number} is still {status}"
            + (f" after {still}" if still else "")
            + f"; it sits with {owner}."
        )
        human_action = f"This incident is still {status} - it sits with {owner}."

    comments = incident_repository.comments_for(incident.IncidentId)

    return {
        "investigation_id": investigation_id,
        "title": f"Incident {incident.Number}",
        "executive_summary": "\n".join(lines).strip(),
        #  None, not a sentence. An incident lookup makes no recommendation, and
        #  the UI hides this box when it is empty - which is the correct
        #  rendering of "the platform is not recommending anything here".
        "top_recommendation": None,
        "alternatives_considered": [],
        #  EMPTY BY DESIGN. "Potential recurrence of NIC flapping" was generated
        #  for a resolved hardware fault. A risk this platform has not computed
        #  is not a risk it should print.
        "risks": [],
        "next_steps": next_steps,
        "human_action_required": human_action,
        #  Not rendered by the report card; carried so the answer path can quote
        #  the timeline and so a caller can see it was read.
        "incident_timeline": [
            {
                "sequence": c.Sequence,
                "at": _stamp(c.CreatedAt),
                "by": c.CreatedBy or "unknown",
                "type": c.Type,
                "text": c.Text,
            }
            for c in comments
        ],
    }
