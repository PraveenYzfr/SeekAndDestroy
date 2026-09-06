"""Task -> role -> tier -> model.

Three layers, each answering a different question:

    role    what the model is being asked to do        (narration, planning...)
    tier    how much that job is worth paying for      (cheap, costly)
    model   which provider and model serve that tier   (deepseek-v4-flash...)

Roles map to tiers, tiers map to models. Changing one model for a whole class of
work is then a single edit, not six - which is the difference between an
operator actually running a comparison and meaning to.

WHY A TIER LAYER AT ALL
-----------------------
Without it, "move everything cheap onto Groq for an hour" means editing every
role individually and remembering to put them all back. With it, one variable
moves the cheap slot and every role sitting in that slot follows.

RESOLUTION ORDER, HIGHEST FIRST
-------------------------------
    1. force-single      SAD_LLM__FORCE_SINGLE - one provider for everything.
                         An escape hatch for an incident, not a configuration.
    2. per-role override the admin screen. Names a specific model for one role.
    3. tier slot         SAD_LLM__CHEAP_* / SAD_LLM__COSTLY_*
    4. base config       SAD_LLM__PROVIDER / SAD_LLM__MODEL

Each layer is narrower than the one below it, so the more specific instruction
always wins and every level is individually inspectable - the admin screen shows
which one produced the answer, rather than only the answer.
"""

from __future__ import annotations

from dataclasses import dataclass

CHEAP = "cheap"
COSTLY = "costly"
TIERS = (CHEAP, COSTLY)


#: Which tier each role sits in by default.
#:
#: The split is by what a mistake costs, not by how often the role runs:
#:
#:   cheap   narration, summarization - the numbers are already decided by
#:           Python and validated by assert_no_number_drift, so the model is
#:           writing prose around figures it cannot change. A weaker model
#:           produces a clumsier sentence, not a wrong answer.
#:
#:   costly  extraction, grounded_qa, reporting, judging - each can be
#:           wrong in a way nothing downstream catches. Extraction feeds the
#:           whole investigation; grounded_qa must be willing to say it does not
#:           know; reporting is the artefact a human reads and acts on; the
#:           judge grades everything else and a cheap judge is worse than none.
DEFAULT_ROLE_TIERS: dict[str, str] = {
    "narration": CHEAP,
    "summarization": CHEAP,
    "extraction": COSTLY,
    "grounded_qa": COSTLY,
    "reporting": COSTLY,
    "judge": COSTLY,
}


@dataclass(frozen=True)
class Resolution:
    """Where a role's model came from, not just what it is."""

    role: str
    tier: str
    provider: str
    model: str
    #: force_single | override | tier | config - the layer that decided.
    source: str
    updated_by: str | None = None
    updated_at: object | None = None

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "tier": self.tier,
            "provider": self.provider,
            "model": self.model,
            "source": self.source,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at,
        }


def tier_for(role_name: str, overrides: dict[str, str] | None = None) -> str:
    """Which tier a role runs in.

    ``overrides`` is the parsed SAD_LLM__ROLE_TIERS, so an operator can move a
    single role between tiers without a code change - the equivalent of
    AutoCoder's AUTOCODER_CODING_TIER=costly, generalised to every role.
    """
    if overrides and role_name in overrides:
        return overrides[role_name]
    return DEFAULT_ROLE_TIERS.get(role_name, COSTLY)


def parse_role_tiers(raw: str) -> dict[str, str]:
    """``"narration=costly,grounded_qa=cheap"`` -> ``{...}``.

    Unknown tiers are dropped rather than raising: a typo in an environment
    variable should cost that one role its override, not stop the service from
    starting. The dropped entry is visible on the admin screen, because the
    role's source will read "tier" instead of what the operator expected.
    """
    parsed: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        role, _, tier = part.partition("=")
        tier = tier.strip().lower()
        if tier in TIERS:
            parsed[role.strip()] = tier
    return parsed
