"""Real resiliency, computed from the CI graph.

WHAT WAS WRONG WITH THE OLD NUMBER
----------------------------------
scoring.subscores.resiliency_subscore is an availability tier plus five points
per node above the minimum. That is a count of things, not a measure of
independence. An application on four VMs scores four-way redundant whether those
VMs sit on four separate hosts in four zones or all four on one hypervisor
mounting one NFS export. The second one dies to a single failure and scores
identically to the first.

The CI graph makes the real figure computable: walk UP from the application and
count DISTINCT parents at each failure-domain class.

THE MINIMUM, NOT THE MEAN
-------------------------
An application on eight hosts, four switches and one storage volume is ONE-way
redundant. It does not matter how good the other two numbers are; the volume
takes it down on its own. So the headline figure is the minimum across evaluated
domains and the useful output is WHICH domain is the minimum, because that names
the thing to fix.

Averaging would produce 4.3 for that application and hide the volume entirely.
That is the same failure as the old node count, one level up.

ABSENT IS NOT ZERO
------------------
This is the trap this module is most careful about, and it has two faces.

  A domain with no rows - no storage CIs seeded yet - is NOT evaluated. It is not
  a zero. Scoring it as zero would make every application in the estate a single
  point of failure the moment a class table is empty, which is a statement about
  our seed data rather than about the estate.

  An application with no placement edge at all - and there are 87 of them right
  now - is UNKNOWN, not fragile. Reporting "0 hosts, single point of failure" for
  an application nobody has recorded a placement for is a confident answer to a
  question we cannot answer.

Both cases return a profile whose `redundancy` is None. Callers must branch on
that rather than comparing it to a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.repositories import ci_graph_repository as graph
from app.scoring.subscores import clamp_d, round2

#: Failure domains, in the order a human reads them - smallest blast radius
#: first. Each is (label, CI class).
#:
#: Storage array sits BELOW volume deliberately: two volumes on one array are
#: distinct at the volume level and a single point one level up, and a traversal
#: that stops at volumes reports two-way redundancy for a single-array topology.
DOMAINS: tuple[tuple[str, str], ...] = (
    ("physical host", graph.CLASS_SERVER),
    ("storage volume", graph.CLASS_STORAGE_VOLUME),
    ("storage array", graph.CLASS_STORAGE_ARRAY),
    ("network device", graph.CLASS_NETWORK),
    ("cluster", graph.CLASS_CLUSTER),
    ("zone", graph.CLASS_ZONE),
    ("data centre", graph.CLASS_DATACENTER),
)


@dataclass(frozen=True)
class FailureDomain:
    label: str
    class_name: str
    #: Distinct CIs of this class supporting the application.
    count: int
    members: tuple[str, ...] = ()
    #: How these CIs were reached. "upward" is the real answer; "cluster members"
    #: is the sibling fallback described in ci_graph_repository.cluster_members
    #: and is recorded because the two mean subtly different things.
    derivation: str = "upward"


@dataclass(frozen=True)
class ResiliencyProfile:
    application_ci_id: int | None
    application_name: str
    domains: dict[str, FailureDomain] = field(default_factory=dict)
    #: Domains with no data at all. Absent, not zero - see the module docstring.
    not_evaluated: tuple[str, ...] = ()
    #: Minimum across evaluated domains, or None when nothing could be evaluated.
    redundancy: int | None = None
    #: The domain that is the minimum. The actionable half of the answer.
    weakest: str | None = None
    #: True when the application has no placement recorded at all.
    unplaced: bool = False
    #: True when the graph walk hit its depth ceiling, so the support set may be
    #: incomplete. Matters because every count here can only be UNDER-stated by
    #: truncation, and an understated count manufactures a single point of
    #: failure that does not exist. Suppresses the SPOF claim rather than the
    #: whole profile - the numbers are still worth showing, just not worth
    #: asserting on.
    truncated: bool = False

    @property
    def is_single_point_of_failure(self) -> bool:
        """Exactly one CI in some evaluated failure domain.

        False when redundancy is unknown. An application we cannot evaluate is
        not thereby fragile, and saying so would be inventing a finding.
        """
        if self.truncated:
            # Truncation can only lower a count, never raise it, so a "1" here
            # may be an artefact of the ceiling rather than the topology.
            return False
        return self.redundancy == 1

    def to_rule_input(self) -> dict:
        """Flatten to the plain dict the eligibility rules consume.

        app.rules.eligibility is deliberately database-free - every rule takes
        data and returns a verdict - so the graph work happens here and the rule
        receives the conclusion. Same posture as change_risk.

        weakest_members is included because RULE-012 has to answer a question
        the count alone cannot: whether the candidate cluster is ALREADY the
        single thing this application depends on, or would be a second one.
        """
        weakest = self.domains.get(self.weakest) if self.weakest else None
        return {
            "redundancy": self.redundancy,
            "weakest": self.weakest,
            "weakest_members": list(weakest.members) if weakest else [],
            "truncated": self.truncated,
            "unplaced": self.unplaced,
            "single_point_of_failure": self.is_single_point_of_failure,
            "domains": {k: v.count for k, v in self.domains.items()},
        }

    def summary(self) -> str:
        if self.unplaced:
            return f"{self.application_name} has no recorded placement - resiliency cannot be assessed."
        if self.redundancy is None:
            return f"{self.application_name} has no evaluable failure domains."
        parts = ", ".join(
            f"{d.count} {d.label}{'s' if d.count != 1 else ''}" for d in self.domains.values()
        )
        if self.truncated:
            return (
                f"{self.application_name} has at least {self.redundancy}-way redundancy "
                f"({parts}), but the graph walk reached its depth limit - the real figure "
                f"may be higher."
            )
        if self.is_single_point_of_failure:
            return (
                f"{self.application_name} depends on a single {self.weakest} "
                f"({parts}). A single failure takes it down."
            )
        return f"{self.application_name} is {self.redundancy}-way redundant at its weakest point ({parts})."


def profile_for_application(application_code: str) -> ResiliencyProfile:
    """Build the resiliency profile for one application, from the graph."""
    ci = graph.ci_for_application(application_code)
    if ci is None:
        return ResiliencyProfile(
            application_ci_id=None, application_name=application_code, unplaced=True
        )

    walk = graph.support_graph(ci.ci_id)
    support = walk.nodes
    if not support:
        return ResiliencyProfile(
            application_ci_id=ci.ci_id, application_name=application_code, unplaced=True
        )

    by_class: dict[str, list] = {}
    for node in support:
        by_class.setdefault(node.class_name, []).append(node)

    # The sibling fallback: hosts are members of the application's cluster rather
    # than ancestors of the application, so an upward walk never reaches them.
    # Only used when the upward walk found no hosts of its own - once VM
    # instances sit between application and host, the real path wins.
    host_derivation = "upward"
    if graph.CLASS_SERVER not in by_class:
        cluster_ids = [n.ci_id for n in by_class.get(graph.CLASS_CLUSTER, [])]
        members = graph.cluster_members(cluster_ids)
        if members:
            by_class[graph.CLASS_SERVER] = members
            host_derivation = "cluster members"

    domains: dict[str, FailureDomain] = {}
    not_evaluated: list[str] = []
    for label, class_name in DOMAINS:
        nodes = by_class.get(class_name, [])
        if not nodes:
            not_evaluated.append(label)
            continue
        domains[label] = FailureDomain(
            label=label,
            class_name=class_name,
            count=len({n.ci_id for n in nodes}),
            members=tuple(sorted(n.name for n in nodes))[:20],
            derivation=host_derivation if class_name == graph.CLASS_SERVER else "upward",
        )

    if not domains:
        return ResiliencyProfile(
            application_ci_id=ci.ci_id,
            application_name=application_code,
            not_evaluated=tuple(not_evaluated),
            unplaced=True,
        )

    weakest_domain = min(domains.values(), key=lambda d: d.count)
    return ResiliencyProfile(
        application_ci_id=ci.ci_id,
        application_name=application_code,
        domains=domains,
        not_evaluated=tuple(not_evaluated),
        redundancy=weakest_domain.count,
        weakest=weakest_domain.label,
        truncated=walk.hit_ceiling,
    )


# =============================================================================
# Scoring
# =============================================================================

#: What each availability tier is expected to survive. Tier-1 is meant to lose a
#: whole site; Tier-3 is meant to lose a component.
TIER_EXPECTED_REDUNDANCY = {"Tier-1": 3, "Tier-2": 2, "Tier-3": 1}

_BASE = Decimal("100")
#: Points lost per level of redundancy below what the tier claims. Steep on
#: purpose: a Tier-1 application with no redundancy is not a slightly worse
#: Tier-1, it is a mislabelled one.
_SHORTFALL_PENALTY = Decimal("30")


def graph_resiliency_subscore(profile: ResiliencyProfile, availability_tier: str) -> Decimal | None:
    """Score the profile against what its tier claims to provide.

    Returns None when redundancy is unknown. The caller decides what to do with
    that - falling back to the node-count sub-score is reasonable, inventing a
    number is not.
    """
    if profile.redundancy is None:
        return None
    expected = TIER_EXPECTED_REDUNDANCY.get(availability_tier, 1)
    shortfall = max(0, expected - profile.redundancy)
    return round2(clamp_d(_BASE - Decimal(shortfall) * _SHORTFALL_PENALTY))
