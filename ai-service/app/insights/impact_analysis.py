"""Impact analysis: blast radius from a failing CI.

Thin summary layer over app.repositories.ci_graph_repository.blast_radius,
which owns the actual cycle-safe traversal (visited-path guard, explicit
MAXRECURSION, the Walk.hit_ceiling truncation signal - see that module's
docstring for why all three are load-bearing). This module used to carry its
own copy of that SQL; it was deleted once ef's ci_graph_repository landed
with an equivalent (and, on the ceiling-honesty point, more correct) guard -
one copy of cycle-detection logic this critical is enough, not two that can
drift apart.

This module's only job now is turning a Walk into the shape a narrator or an
API route actually wants: an affected count, an observed depth, and an
honest "this may be a floor, not the true number" flag - never a bare N when
the walk was truncated (GUARDRAILS-equivalent for this feature).
"""

from __future__ import annotations

from app.repositories import ci_graph_repository
from app.repositories.base import T, fetch_one


class UnknownCiError(ValueError):
    """Raised when a CI name/code could not be resolved to a CiId - refuses
    rather than silently walking from nothing and reporting a zero blast
    radius that looks like a real finding.

    PUBLIC, and this one is a judgement rather than an obvious call. The message
    echoes back the name the CALLER typed and nothing else, so it discloses no
    estate data - but "no CI named X" is still an existence oracle, and someone
    could probe names to learn what exists.

    Judged acceptable because the endpoint is authenticated and a SUCCESSFUL
    answer is a far stronger oracle than a refusal: anyone who can ask can
    already confirm existence by getting a blast radius back. Suppressing only
    the refusal would cost a caller the ability to spot their own typo while
    leaving the stronger signal untouched.

    If this endpoint is ever exposed unauthenticated, revisit this - the
    reasoning above depends entirely on that.
    """

    public_detail = True


def resolve_ci_id(name: str, class_name: str | None = None) -> int | None:
    """A CI's Name is not guaranteed unique across classes (see
    app.insights.cmdb_health.duplicates_by_class) - pass class_name whenever
    it is known (a cluster code and a server hostname could coincidentally
    collide) rather than accepting whichever row SQL happens to return first.

    General-purpose across every CI class, unlike
    ci_graph_repository.ci_for_application (application CIs only) - kept
    here rather than asking ef to widen theirs, since "resolve any CI by
    name" and "find the CI for this application code" are different enough
    questions to earn separate functions.
    """
    if class_name:
        row = fetch_one(
            f"SELECT CiId FROM {T('ConfigurationItem')} WHERE Name = :name AND ClassName = :cls",
            {"name": name, "cls": class_name},
        )
    else:
        row = fetch_one(f"SELECT TOP (1) CiId FROM {T('ConfigurationItem')} WHERE Name = :name", {"name": name})
    return row["CiId"] if row else None


def blast_radius(start_ci_id: int, *, max_depth: int = ci_graph_repository.DEFAULT_MAX_DEPTH) -> dict:
    """Every CI reachable from start_ci_id walking parent -> child (what
    dies if start_ci_id fails - see ci_graph_repository's module docstring
    on why this is not interchangeable with support_graph's direction).

    Deliberately does NOT pass edge_types: blast_radius's own default
    (None, meaning every relationship type including Depends on::Used by) is
    correct for impact - when a provider dies, its dependents are genuinely
    affected - whereas support_graph's SUPPORT_EDGES default exists for a
    different question (resiliency) and would silently drop exactly the
    edges impact analysis needs if passed here by habit.

    affected_cis is exact when hit_depth_ceiling is False. When True, it is
    a LOWER BOUND - the true blast radius is at least that large - and any
    narration built on this must say "at least N", never state it as the
    number.
    """
    walk = ci_graph_repository.blast_radius(start_ci_id, max_depth=max_depth)
    return {
        "start_ci_id": start_ci_id,
        "affected_cis": len(walk),
        "max_depth": walk.observed_depth,
        "hit_depth_ceiling": walk.hit_ceiling,
        "affected_cis_is_lower_bound": walk.hit_ceiling,
    }


def blast_radius_for_name(
    name: str, class_name: str | None = None, *, max_depth: int = ci_graph_repository.DEFAULT_MAX_DEPTH
) -> dict:
    """blast_radius, addressed by CI name instead of raw id - the form a
    question like "what breaks if atl-03 dies" actually arrives in."""
    ci_id = resolve_ci_id(name, class_name)
    if ci_id is None:
        raise UnknownCiError(f"No CI named {name!r}" + (f" of class {class_name!r}" if class_name else "") + " found.")
    result = blast_radius(ci_id, max_depth=max_depth)
    result["start_name"] = name
    return result
