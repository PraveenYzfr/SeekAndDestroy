"""Administrator endpoints: which model runs each role.

Every route here is behind require_admin. These change what model the platform
uses and therefore what it spends, so they are not something an ordinary
authenticated employee should reach.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.agents import provider_models, roles
from app.agents.llm_factory import reset_role_model_cache, resolve_all_roles
from app.api.auth import require_admin
from app.api.errors import ProblemDetailsError
from app.repositories import llm_role_repository
from app.security.jwt_service import AuthenticatedEmployee

router = APIRouter(tags=["admin"])


class RoleAssignment(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=200)


def _fallback_for(role_name: str) -> dict:
    """What answers for this role when its primary provider fails."""
    from app.agents.llm_factory import resolve_role

    key = roles.fallback_role_name(role_name)
    resolved = resolve_role(key)
    configured = resolved.get("source") == "override"
    return {
        "role": key,
        "provider": resolved.get("provider") if configured else None,
        "model": resolved.get("model") if configured else None,
        "configured": configured,
    }


@router.get("/api/admin/model-roles")
def get_model_roles(current: AuthenticatedEmployee = Depends(require_admin)):
    """Every role, its effective model, and where that model came from.

    ``source`` is "config" or "override" - the distinction the Reset control
    acts on, and the one that tells an operator whether changing
    config/settings.py would affect this role at all.

    ``chains`` lists the functions each role routes, so the choice is traceable
    to real behaviour instead of to a label somebody has to guess the meaning of.
    """
    resolved = {r["role"]: r for r in resolve_all_roles()}
    return {
        "roles": [
            {
                "name": role.name,
                "title": role.title,
                "description": role.description,
                "chains": list(role.chains),
                **resolved.get(role.name, {}),
                # This role's own backup, sent beside it so the screen can show
                # the pair together rather than as two unrelated rows.
                #
                # Per role, not one global spare: extraction wants strict schema
                # adherence and reporting wants readable prose, so a single
                # estate-wide substitute is right for at most one of them. Being
                # wrong for the others surfaces as a quiet change in output
                # quality rather than an error.
                #
                # `configured` False means no backup was chosen. Deliberately not
                # filled in with a default - a fallback nobody selected is a model
                # nobody evaluated, and choosing one automatically turns an outage
                # into a silent change in behaviour.
                "fallback": _fallback_for(role.name),
            }
            for role in roles.ROLES
        ],
        # THIS USED TO CONTRADICT THE SCREEN IT APPEARS ON. It read "Evaluation
        # has no model to configure" while an "Evaluation judge" role was listed
        # directly above it, with its own model selector. Both halves were
        # half-true and together they were misleading - Praveen read it and asked
        # what it meant, which is the only reason it was found.
        #
        # The missing distinction is WHICH evaluation. The checks that decide
        # whether a figure was invented use no model at all. The judge is a
        # separate, narrower thing that does.
        "evaluation_note": (
            "Two different things are called evaluation here.\n\n"
            "The checks that matter use NO model. app/evaluation/graders.py proves by "
            "arithmetic that every figure in an answer traces to a value the engine "
            "computed, and that every cluster or application code was a real candidate. "
            "A model asked the same question could only agree or be wrong.\n\n"
            "The Evaluation judge role above DOES use a model, deliberately limited to "
            "the three things arithmetic cannot see - relevance, groundedness and "
            "actionability. It is never asked about numbers and its schema gives it "
            "nowhere to put a numeric verdict. Point it at a different provider from the "
            "one being judged: a model scoring its own output rates it higher.\n\n"
            "To compare two models: assign one above, run some investigations, then run "
            "scripts/evaluate.py. It grades calls that already happened from "
            "sad.AgentAuditLog, so a full run costs a table scan rather than a provider "
            "bill, and every historical call is already tagged with the model that made it."
        ),
    }


@router.get("/api/admin/model-providers")
def get_model_providers(refresh: bool = False, current: AuthenticatedEmployee = Depends(require_admin)):
    """What each provider will actually serve, asked at runtime.

    Never a hardcoded list - see app/agents/provider_models.py for the five
    times a stale name broke this estate. A provider that cannot be reached is
    returned with available=false and its reason, so the screen can say why
    instead of showing an empty dropdown.
    """
    return {"providers": provider_models.list_all(refresh=refresh)}


@router.put("/api/admin/model-roles/{role_name}")
def set_model_role(
    role_name: str,
    assignment: RoleAssignment,
    current: AuthenticatedEmployee = Depends(require_admin),
):
    """Point a role at a specific provider and model.

    Validated against the provider's live listing rather than accepted as typed:
    storing a name the provider does not serve turns a bad save into a failed
    investigation later, somewhere with no visible connection to this screen.
    """
    if role_name not in roles.ASSIGNABLE_ROLE_NAMES:
        raise ProblemDetailsError(
            404, "Unknown role", f"'{role_name}' is not a model role. Known roles: {sorted(roles.ASSIGNABLE_ROLE_NAMES)}."
        )
    if assignment.provider not in provider_models.LISTABLE:
        raise ProblemDetailsError(
            400, "Unknown provider",
            f"'{assignment.provider}' is not a supported provider. Supported: {list(provider_models.LISTABLE)}.",
        )

    listing = provider_models.list_models(assignment.provider)
    if listing["available"] and assignment.model not in listing["models"]:
        raise ProblemDetailsError(
            400, "Unknown model",
            f"{assignment.provider} does not currently serve '{assignment.model}'. "
            f"It may have been retired - reload the model list and choose again.",
        )

    llm_role_repository.set_override(
        role_name, assignment.provider, assignment.model, current.employee_number
    )
    # The cache is keyed on (provider, model), so a stale entry would keep
    # serving the previous model until the process restarted.
    reset_role_model_cache()
    return {
        "role": role_name,
        "provider": assignment.provider,
        "model": assignment.model,
        "source": "override",
        # True when the provider could not be reached to confirm the name. Said
        # plainly rather than implied, because an unverified save can still fail
        # at run time.
        "unverified": not listing["available"],
    }


@router.delete("/api/admin/model-roles/{role_name}")
def clear_model_role(role_name: str, current: AuthenticatedEmployee = Depends(require_admin)):
    """Drop the override so the role follows config/settings.py again."""
    if role_name not in roles.ASSIGNABLE_ROLE_NAMES:
        raise ProblemDetailsError(404, "Unknown role", f"'{role_name}' is not a model role.")
    removed = llm_role_repository.clear(role_name)
    reset_role_model_cache()
    # "was already the default" is a different answer from "reset", and the
    # screen should not claim to have changed something it did not.
    return {"role": role_name, "removed": removed, "source": "config"}


@router.get("/api/admin/evaluation")
def get_evaluation(limit: int = 5000, current: AuthenticatedEmployee = Depends(require_admin)):
    """The scorecard, from calls that already happened.

    WHY THIS ENDPOINT EXISTS. The Model Settings screen told an administrator to
    "run scripts/evaluate.py" - and scripts/ is not in the service image, so on
    the deployed system that instruction could not be followed by anyone. The
    capability was real and reachable only by someone with a shell on the box and
    a willingness to import app.evaluation.harness by hand. Which meant that in
    practice nobody could see whether a model change had made answers worse.

    That matters more now than it did a week ago: the point of per-role model
    selection is to move a role onto a faster or cheaper model, and the only
    thing standing between that and "fast and confidently wrong" is this
    scorecard. An acceptance gate nobody can run is not a gate.

    NO MODEL IS CALLED HERE and nothing is spent. The harness reads
    sad.AgentAuditLog and grades text that was already generated and already paid
    for, so a run costs a table scan. That is also why it is safe to expose: the
    expensive, irreversible thing already happened.

    ``limit`` bounds the scan rather than the result. An estate running for a year
    has more audit rows than belong in one response, and the default is chosen to
    cover recent behaviour rather than all history - a lifetime average hides
    exactly the regression this is meant to catch.
    """
    from app.evaluation import harness

    try:
        return harness.evaluate(limit=limit)
    except Exception as exc:  # noqa: BLE001
        # Surfaced rather than swallowed. An empty scorecard and a broken one look
        # identical to a reader, and the difference is whether the platform is
        # behaving or the measurement is.
        raise ProblemDetailsError(
            status=500,
            title="Evaluation could not be computed",
            detail=str(exc)[:500],
        ) from exc


@router.get("/api/admin/answer-evaluations")
def get_answer_evaluations(
    limit: int = 50,
    worst_only: bool = False,
    current: AuthenticatedEmployee = Depends(require_admin),
):
    """Verdicts on answers this platform actually delivered.

    DIFFERENT QUESTION FROM /api/admin/evaluation, which is why both exist.
    That one grades the audit table on demand and answers "how is this MODEL
    behaving". This one reads verdicts recorded at the time each answer was
    given and answers "was THAT answer any good" - including the judge's opinion,
    which the harness never had because the harness runs long after the fact.

    Grafana covers the rates. It cannot show the four answers that scored 2 on
    groundedness last week, because it stores aggregates and drops the
    individuals - and those individuals are what a fix starts from.

    ``worst_only`` is the review queue: judge scores at or below 3 on any
    dimension, self-judged verdicts excluded. A model grading its own work grades
    it high, so including those would fill a reviewer's queue with noise about
    the grader rather than the answers.
    """
    from app.repositories import answer_evaluation_repository as repo

    bounded = max(1, min(int(limit), 500))
    try:
        rows = repo.worst(limit=bounded) if worst_only else repo.recent(limit=bounded)
    except Exception as exc:  # noqa: BLE001
        raise ProblemDetailsError(
            status=500,
            title="Answer evaluations could not be read",
            detail=str(exc)[:500],
        ) from exc

    return {
        "count": len(rows),
        "worst_only": worst_only,
        "evaluations": [
            {
                "id": row.AnswerEvaluationId,
                "investigation_id": row.InvestigationId,
                "conversation_id": row.ConversationId,
                "question": row.Question,
                # Kept apart in the payload exactly as they are kept apart in the
                # table. A client that wants one number can compute one; a client
                # handed one number cannot recover which half moved.
                "deterministic": {
                    "number_fidelity": _as_float(row.NumberFidelity),
                    "entity_fidelity": _as_float(row.EntityFidelity),
                    "completeness": _as_float(row.Completeness),
                    "graded_calls": row.GradedCalls,
                    "ungradeable_calls": row.UngradeableCalls,
                    "ungrounded": repo.ungrounded_tokens(row),
                },
                "judge": {
                    "provider": row.JudgeProvider,
                    "model": row.JudgeModel,
                    "relevance": row.JudgeRelevance,
                    "groundedness": row.JudgeGroundedness,
                    "actionability": row.JudgeActionability,
                    "confident": row.JudgeConfident,
                    # Surfaced, never quietly dropped. A reader comparing scores
                    # has to know which of them a model gave itself.
                    "self_judged": row.JudgeSelfJudged,
                    "justification": row.JudgeJustification,
                    "error": row.JudgeError,
                },
                "created_at": row.CreatedAt,
            }
            for row in rows
        ],
    }


def _as_float(value) -> float | None:
    """Decimal to float for JSON, preserving None.

    None means NOT MEASURED and must survive the trip to the client. Coercing it
    to 0.0 here would undo the distinction the table goes to some trouble to
    keep, one layer before anybody sees it.
    """
    return None if value is None else float(value)


@router.get("/api/admin/investigations/{investigation_id}/transcript")
def get_transcript(investigation_id: int, current: AuthenticatedEmployee = Depends(require_admin)):
    """The full model exchange for one investigation, with the score beside it.

    Every call, in order: the prompt that was sent, the output that came back,
    the model that produced it, how long it took - and what each grader made of
    it. sad.AgentAuditLog has held the exchange all along; what was missing was
    a way to read it with the verdict attached.

    ADMIN ONLY, and not because scores are sensitive. The prompts are: they carry
    the evidence the engine assembled, which includes incident text and estate
    capacity. That is the same reason routes_system.ready is not exposed.

    Grades come from sad.CallEvaluation, so this shows what was ACTUALLY recorded
    rather than re-grading on read. Re-grading here would quietly answer a
    different question - "what would today's rules say" instead of "what did we
    conclude" - and the two diverged three times in one night.
    """
    from app.repositories import call_evaluation_repository

    rows = call_evaluation_repository.for_investigation(investigation_id)
    calls: dict[int, dict] = {}
    for r in rows:
        call = calls.setdefault(r["AuditId"], {
            "audit_id": r["AuditId"],
            "graph_node": r.get("GraphNode"),
            "schema": str(r.get("ToolName") or "").removeprefix("llm:"),
            "model": r.get("ModelIdentity"),
            "provider": r.get("Provider"),
            "started_at": r.get("StartedAt"),
            "completed_at": r.get("CompletedAt"),
            "success": r.get("Success"),
            "prompt": r.get("InputJson"),
            "output": r.get("OutputJson"),
            "grades": [],
        })
        call["grades"].append({
            "grader": r["Grader"],
            "grounded": r["Grounded"],
            "total": r["Total"],
            # The denominator travels with the rate, here as everywhere.
            "rate": float(r["Rate"]) if r["Rate"] is not None else None,
            "ungrounded": r.get("UngroundedJson"),
            "grader_version": r.get("GraderVersion"),
            "graded_at": r.get("CreatedAt"),
        })
    return {
        "investigation_id": investigation_id,
        "calls": list(calls.values()),
        # Stated rather than left to be inferred from an empty list. A grading
        # pass may simply not have run yet, and "no verdicts" reads as "nothing
        # was wrong" if nobody says which it is.
        "note": (
            "Verdicts appear once an evaluation run has graded these calls. "
            "Run one from Model Settings; it grades recorded calls and spends nothing."
            if not calls else ""
        ),
    }


@router.get("/api/admin/conversations/{conversation_id}/evaluation")
def get_conversation_evaluation(
    conversation_id: str, current: AuthenticatedEmployee = Depends(require_admin)
):
    """One score per grader for a whole conversation.

    Sums grounded and total across the conversation's calls rather than
    averaging the per-call rates. Those are different numbers: a call with two
    figures and a call with two hundred count equally in a mean of rates, so one
    short narration can outweigh an entire report. Summing gives the rate over
    figures actually written, which is the claim being made.

    MinVersion and MaxVersion are returned because a conversation can span a
    grader change, and a single figure covering two rule sets is not one
    measurement. When they differ, the number is a mixture and should be read as
    one.
    """
    from app.repositories import call_evaluation_repository

    rows = call_evaluation_repository.rollup_for_conversation(conversation_id)
    graders = [
        {
            "grader": r["Grader"],
            "grounded": r["Grounded"],
            "total": r["Total"],
            "calls": r["Calls"],
            "rate": (round(r["Grounded"] / r["Total"], 4) if r["Total"] else None),
            "grader_version_min": r.get("MinVersion"),
            "grader_version_max": r.get("MaxVersion"),
            "mixed_grader_versions": r.get("MinVersion") != r.get("MaxVersion"),
        }
        for r in rows
    ]
    return {"conversation_id": conversation_id, "graders": graders}


@router.get("/api/admin/conversations/{conversation_id}")
def get_conversation_detail(
    conversation_id: str, current: AuthenticatedEmployee = Depends(require_admin)
):
    """One conversation at all three levels, in one payload.

        session   one score per grader for the whole conversation
        turns     what was asked, what came back, and that exchange's score
        calls     the individual model calls behind a turn, each with its verdict

    THEY ARE NOT THE SAME NUMBER AND MUST NOT BE DERIVED FROM EACH OTHER. The
    session figure sums grounded and total across every call; averaging the turn
    rates instead would let a one-sentence reply weigh as much as a full report.
    The turn figure does the same over its own calls. Only the call level is a
    raw measurement - the two above it are aggregates, and each is computed from
    the counts rather than from the level below's rate.

    WHY THE TURN LEVEL EXISTS AT ALL. A conversation-wide score hides which
    exchange was the bad one, and that is precisely what someone opening this is
    looking for. "This conversation scored 0.91" is not actionable; "turn three
    scored 0.55 and here is the figure it invented" is.

    The assistant text is the one-line summary ConversationTurn stores - history
    exists to resolve references rather than to re-read reports, and the full
    report stays on the Investigation row. ``investigation_id`` on a turn is the
    way through to it, and to that turn's calls.
    """
    from app.repositories import call_evaluation_repository as ce

    def _rate(grounded, total):
        # None, not 0.0, when nothing was measurable. Zero is a score; "there was
        # nothing to score" is not.
        return round(grounded / total, 4) if total else None

    session = [
        {
            "grader": r["Grader"], "grounded": r["Grounded"], "total": r["Total"],
            "calls": r["Calls"], "rate": _rate(r["Grounded"], r["Total"]),
            "mixed_grader_versions": r.get("MinVersion") != r.get("MaxVersion"),
        }
        for r in ce.rollup_for_conversation(conversation_id)
    ]

    per_investigation: dict[int, list[dict]] = {}
    for r in ce.rollup_by_investigation(conversation_id):
        per_investigation.setdefault(r["InvestigationId"], []).append({
            "grader": r["Grader"], "grounded": r["Grounded"], "total": r["Total"],
            "calls": r["Calls"], "rate": _rate(r["Grounded"], r["Total"]),
        })

    turns = []
    pending_question = None
    for t in ce.turns_for_conversation(conversation_id):
        if t["Role"] == "User":
            pending_question = t["Message"]
            continue
        # An assistant turn closes the exchange the preceding user turn opened.
        turns.append({
            "turn_id": t["TurnId"],
            "asked": pending_question,
            "answered": t["Message"],
            "investigation_id": t.get("InvestigationId"),
            "at": t.get("CreatedAt"),
            "scores": per_investigation.get(t.get("InvestigationId"), []),
        })
        pending_question = None

    return {
        "conversation_id": conversation_id,
        "session": session,
        "turns": turns,
        # Said plainly rather than left to be inferred from empty lists. A turn
        # with no scores may simply not have been graded yet, and silence reads
        # as "nothing was wrong".
        "note": (
            "" if session else
            "No stored verdicts for this conversation yet. Run an evaluation from "
            "Model Settings - it grades recorded calls and spends nothing."
        ),
    }


@router.get("/api/admin/conversations")
def list_conversations(limit: int = 50, current: AuthenticatedEmployee = Depends(require_admin)):
    """Conversations to inspect, WORST FIRST.

    Sorted by number fidelity ascending, not by recency. The reason to open this
    list is to find a bad answer; ordering by time puts the newest conversation
    on top whether or not anything is wrong with it, and buries the one worth
    reading.

    Ungraded conversations sort last. They have no score - which is not the same
    as scoring zero, and putting them at the top would fill the screen with
    conversations nobody has measured.
    """
    from app.repositories import call_evaluation_repository as ce

    rows = ce.recent_conversations(limit=limit)
    return {
        "conversations": [
            {
                "conversation_id": r["ConversationId"],
                "started_at": r.get("StartedAt"),
                "last_activity_at": r.get("LastActivityAt"),
                "turns": r.get("Turns"),
                "number_fidelity": (
                    round(r["NumberGrounded"] / r["NumberTotal"], 4)
                    if r.get("NumberTotal") else None
                ),
                "figures_checked": r.get("NumberTotal") or 0,
            }
            for r in rows
        ]
    }


@router.get("/api/admin/remediation")
def get_remediation_queue(
    status: str | None = "Queued",
    limit: int = 100,
    current: AuthenticatedEmployee = Depends(require_admin),
):
    """The failures the graph used to drop, and how often each site fires.

    READ ONLY, DELIBERATELY. Nothing acts on these rows. The triage taxonomy is a
    guess until there are real failures to check it against, and an agent built
    on a guessed taxonomy would confidently mis-route fifty cases before anybody
    noticed. Praveen and 40 want to read ~50 real ones first.

    ``by_site`` is the part that answers the question that needed a log grep on a
    production box: how often does narration fail, and where. ``tasks`` is the
    individual cases behind that count - because a rate says how often and cannot
    say which answer, what the model was given, or whether it is still wrong.

    Pass status=null to see every state rather than only the open queue.
    """
    from app.repositories import remediation_repository

    try:
        return {
            "by_site": remediation_repository.counts_by_site(),
            "tasks": remediation_repository.queue(status=status or None, limit=limit),
            "note": (
                "Read-only. Nothing acts on these yet - the triage classes are a "
                "guess until there are real failures to check them against."
            ),
        }
    except Exception as exc:  # noqa: BLE001
        # Surfaced rather than returned empty. An empty queue and a broken query
        # look identical to a reader, and the difference is whether the platform
        # is behaving or the measurement is.
        raise ProblemDetailsError(
            status=500,
            title="Remediation queue could not be read",
            detail=str(exc)[:500],
        ) from exc
