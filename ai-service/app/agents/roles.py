"""The model roles this platform actually has, and what each one does.

Derived from the call sites in app.agents.chains, not invented. Every chain
function in this codebase is listed against exactly one role below, so "which
model wrote this?" always has an answer and no chain can quietly fall outside
the routing.

WHY SIX ROLES AND NOT TEN
-------------------------
There are ten chain functions but four of them are the same job - turning a
scored candidate, a right-sizing result or a forecast into a sentence. Giving
each its own dropdown would ask the operator to make four decisions where the
useful question is one: "which model narrates?". The grouping is by what the
model is asked to *do*, because that is what makes one model better than
another at it.

WHY THESE ARE NOT TIERS
-----------------------
AutoCoder routes by cost tier - cheap, costly, coding. That works there because
the tiers map to how much a mistake costs. Here the interesting differences are
about capability: extraction rewards strict schema adherence, grounded QA
rewards staying inside its evidence, narration rewards readable prose. A
"cheap/expensive" axis cannot express "this model is careful with numbers".
"""

from __future__ import annotations

from dataclasses import dataclass

#: EVALUATION USES TWO GRADERS, AND THE DETERMINISTIC ONE IS STILL THE BACKSTOP.
#:
#: app.evaluation.graders is pure functions over (prose, evidence) with no model
#: at all: number_fidelity and entity_fidelity can prove that every figure in a
#: sentence came from the evidence. No judge can prove that, and no judge is
#: needed to - it is arithmetic over the two texts.
#:
#: app.evaluation.judge adds what arithmetic cannot see: whether the answer
#: actually addresses the question, whether it admits uncertainty instead of
#: filling gaps, whether a human would act on it. Those are judgements, and a
#: model is the only practical way to make them at volume.
#:
#: They are kept separate on purpose. The judge never scores numbers - the
#: deterministic graders already do, exactly, and a judge that disagreed with
#: number_fidelity would be wrong by construction. So a judge failure can always
#: be checked against something that cannot hallucinate.


@dataclass(frozen=True)
class ModelRole:
    name: str
    title: str
    #: What this model is asked to do, in the operator's terms rather than ours.
    description: str
    #: The chain functions routed here. Shown in the admin screen so the choice
    #: is traceable to real behaviour rather than to a label.
    chains: tuple[str, ...]


#: Suffix naming a role's backup. "extraction.fallback" is where extraction goes
#: when its own provider fails.
#:
#: A suffix rather than a column, so no migration and no second table: the
#: override store is keyed by role name and a fallback IS a role assignment - the
#: same provider, the same model, chosen the same way, used under a different
#: condition.
#:
#: Per role rather than one global backup, because the roles do not fail alike.
#: Extraction wants strict schema adherence and reporting wants readable prose;
#: a single estate-wide substitute is the right answer for at most one of them,
#: and being wrong for the others shows up as a quiet change in output quality
#: rather than an error.
FALLBACK_SUFFIX = ".fallback"


def fallback_role_name(role_name: str) -> str:
    """The override key holding this role's backup."""
    return f"{role_name}{FALLBACK_SUFFIX}"


def is_fallback_role(role_name: str) -> bool:
    return role_name.endswith(FALLBACK_SUFFIX)


def primary_role_name(role_name: str) -> str:
    """The role a fallback backs, or the name unchanged."""
    return role_name[: -len(FALLBACK_SUFFIX)] if is_fallback_role(role_name) else role_name


#: REMOVED: "planning".
#:
#: It named parse_investigation_plan, and 2bd4311 stopped calling that - the plan
#: it produced averaged 8.2s per investigation and nothing downstream read it.
#: The role outlived the work, so Model Settings kept offering a model dropdown
#: and a fallback slot for a chain that no longer runs.
#:
#: A dropdown that does nothing is worse than an absent one. An operator setting
#: it would see no effect, find no calls under it in AgentAuditLog, and have no
#: way to tell that from a broken configuration - so it is gone rather than
#: hidden or disabled.
#:
#: Existing sad.LlmRoleOverride rows naming "planning" are left in place. They are
#: inert, and deleting somebody's saved configuration to tidy up a table is a
#: worse trade than leaving a dead row nothing reads. If the planning step ever
#: returns, this entry comes back and those rows start meaning something again.
ROLES: tuple[ModelRole, ...] = (
    ModelRole(
        name="extraction",
        title="Requirement extraction",
        description=(
            "Reads free text and produces a structured requirement - CPU, memory, "
            "environment, tier. Rewards strict schema adherence over fluency; a model "
            "that improvises here fails the whole investigation."
        ),
        chains=("extract_hosting_requirement", "extract_capacity_requirement"),
    ),
    ModelRole(
        name="narration",
        title="Narration",
        description=(
            "Explains a candidate, a right-sizing result or a forecast that Python has "
            "already decided. The numbers are given to it; it must not invent any."
        ),
        chains=(
            "explain_candidate",
            "explain_cluster_right_sizing",
            "explain_application_right_sizing",
            "explain_forecast",
        ),
    ),
    ModelRole(
        name="summarization",
        title="Trade-off summary",
        description="Compares scored candidates and states the trade-off between them.",
        chains=("summarize_tradeoffs",),
    ),
    ModelRole(
        name="grounded_qa",
        title="Grounded Q&A",
        description=(
            "Answers questions from retrieved evidence only. Rewards a model that says "
            "it does not know over one that fills the gap."
        ),
        # answer_rejection_question shares this role deliberately. It is the
        # same job under a tighter word budget - answer from retrieved
        # evidence, say so when the evidence does not cover it - so the model
        # that suits one suits the other, and splitting it would mean two
        # settings to keep in step for one behaviour.
        chains=("answer_grounded_question", "answer_rejection_question"),
    ),
    ModelRole(
        name="judge",
        title="Evaluation judge",
        description=(
            "Grades other models' answers on the dimensions arithmetic cannot see - "
            "does it answer the question, does it admit what it does not know. It never "
            "scores numbers; number_fidelity already does that deterministically. "
            "Costly tier deliberately: a cheap judge is worse than no judge."
        ),
        chains=("judge_answer",),
    ),
    ModelRole(
        name="reporting",
        title="Final report",
        description=(
            "Writes the investigation report. Its output is checked against the evidence "
            "by assert_no_number_drift before it is trusted."
        ),
        chains=("generate_final_report",),
    ),
)

ROLE_NAMES: frozenset[str] = frozenset(r.name for r in ROLES)

#: What the admin API will accept. Every role, plus its fallback key - the screen
#: sets both through the same endpoint, so both have to be valid names.
ASSIGNABLE_ROLE_NAMES: frozenset[str] = ROLE_NAMES | frozenset(
    fallback_role_name(r.name) for r in ROLES
)

#: Chain function -> role. Used to attribute a recorded call to a role in the
#: audit log, so scripts/evaluate.py can score a model per role rather than only
#: per model.
CHAIN_TO_ROLE: dict[str, str] = {chain: role.name for role in ROLES for chain in role.chains}


def get(name: str) -> ModelRole | None:
    for role in ROLES:
        if role.name == name:
            return role
    return None
