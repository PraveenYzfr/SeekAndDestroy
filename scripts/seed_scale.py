"""Fill the estate: ~1,200 applications, packed onto clusters to real targets.

THE PROBLEM THIS SOLVES
-----------------------
The seeded estate had 40 applications, 256 clusters and 40 hosting rows - one
per application, all Production. So 246 clusters hosted nothing, and 123 of them
(every Staging, Test and Development cluster) could never host anything, because
RULE-001 forbids placing a Production workload on non-Production infrastructure
and every application was Production.

That makes the scoring engine measure nothing. `capacity` carries weight 0.30,
the heaviest of the seven dimensions, and it was comparing 256 near-empty boxes:
every candidate scored near-perfect headroom, so the dimension could not
discriminate and placement fell through to static attributes - platform and
node count. "Best cluster for APP-ANALYTICS" was decided by compatibility,
because nothing else had signal in it.

PACKING, NOT SPRINKLING
-----------------------
Applications are not scattered randomly. Each cluster is given a *target
utilisation* drawn from a deliberate distribution, and applications are packed
into it until that target is reached. Two reasons this matters more than it
looks:

1. It produces clusters that are genuinely stressed and clusters that are
   genuinely idle, which is what right-sizing and placement both need to have
   anything to say. Random assignment produces a uniform middle where every
   cluster looks like every other cluster.

2. The stress figure it yields is the input to everything downstream. Incidents
   concentrate on stressed clusters; changes fail on stressed clusters. If
   allocation were random, those correlations would be decoration - a reviewer
   asking "why does this cluster have more incidents" would get no answer.

DR IS MODELLED WITH IsPrimary, NOT AN ENVIRONMENT
-------------------------------------------------
The schema allows Production, Staging, Test and Development - there is no DR
value, and adding one would be wrong: a DR cluster runs production workloads
under production rules, so RULE-001 would then refuse to place a production
application on it.

Instead a DR standby is a second Production hosting row, in a different data
centre, with IsPrimary = 0. The column already existed and did nothing, because
with one hosting row per application there was never anything to be primary
over.

This matters beyond tidiness. A DR cluster sits at low utilisation *by design*,
and a right-sizing engine that cannot tell it apart from a genuinely wasteful
cluster will recommend reclaiming the estate's failover capacity - the most
dangerous recommendation an infrastructure tool can make.
"""

from __future__ import annotations

from dataclasses import dataclass

#: How many applications the estate should end up with. An LOB with 256 clusters
#: carries roughly this many; 40 was the number that made the ratio absurd.
TARGET_APPLICATIONS = 1200

#: Target utilisation bands, as (share of clusters, low, high). The shape is the
#: point: a real estate is not uniformly loaded. It has a stressed minority that
#: causes most of the incidents, a broad healthy middle, and a genuine tail of
#: waste - and right-sizing needs all three to exist or it has nothing to find.
#: Clusters carrying one of these tags are hand-designed fixtures. Their
#: util_profile is not decoration - it IS the property the tests and the demo
#: assert. clt-13 is tagged FORECAST because it must be seen to run out of
#: capacity inside a quarter; a cluster that no longer breaches is not a
#: forecast cluster, it is just a cluster.
#:
#: Both places below consult this. Packing reads the designed profile instead of
#: drawing a band, and derive_utilisation_profiles() then leaves the profile
#: alone - so the estate stays self-consistent (allocated agrees with measured)
#: without the generator overwriting the eight scenarios it was asked to build.
DESIGNATED_TAGS = frozenset({
    "OVERPROVISIONED", "NEAR_CPU", "NEAR_MEM", "HIGH_COST_LOW_UTIL",
    "SUITABLE_NEW", "LOW_RESILIENCY", "COMPLIANCE_MISMATCH", "FORECAST",
})


def is_designated(cluster) -> bool:
    """True for a cluster whose utilisation was designed, not drawn."""
    return bool(DESIGNATED_TAGS.intersection(getattr(cluster, "tags", ()) or ()))


UTILISATION_BANDS = (
    (0.15, 78.0, 92.0),   # stressed - the interesting ones
    (0.50, 42.0, 70.0),   # healthy
    (0.22, 18.0, 40.0),   # light
    (0.13, 5.0, 16.0),    # idle or standby
)

#: Business-domain vocabulary for generated application names. Real enough that
#: an incident about "APP-CLAIMS-INTAKE" reads like an incident about something,
#: which matters when the text is what retrieval works on.
_DOMAINS = [
    ("PAYMENTS", "Payments"), ("CLAIMS", "Claims"), ("LENDING", "Lending"),
    ("CARDS", "Cards"), ("TRADING", "Trading"), ("CUSTODY", "Custody"),
    ("WEALTH", "Wealth"), ("MORTGAGE", "Mortgage"), ("DEPOSITS", "Deposits"),
    ("TREASURY", "Treasury"), ("FRAUD", "Fraud"), ("AML", "AML"),
    ("KYC", "KYC"), ("RISK", "Risk"), ("REG", "Regulatory"),
    ("CRM", "Customer"), ("ONBOARD", "Onboarding"), ("BILLING", "Billing"),
    ("STATEMENTS", "Statements"), ("NOTIFY", "Notifications"),
    ("DATA", "Data Platform"), ("REPORTING", "Reporting"), ("ARCHIVE", "Archive"),
    ("IDENTITY", "Identity"), ("GATEWAY", "API Gateway"), ("BATCH", "Batch"),
]

_FUNCTIONS = [
    ("API", "API"), ("UI", "Portal"), ("SVC", "Service"), ("ETL", "ETL"),
    ("ENGINE", "Engine"), ("SYNC", "Sync"), ("FEED", "Feed"), ("CACHE", "Cache"),
    ("WORKER", "Worker"), ("SCHED", "Scheduler"), ("EXPORT", "Export"),
    ("INTAKE", "Intake"), ("LEDGER", "Ledger"), ("RECON", "Reconciliation"),
]


@dataclass
class ClusterLoadPlan:
    """What a cluster is meant to end up looking like, and what it got.

    ``target_pct`` is drawn before packing; ``achieved_cpu_pct`` is what the
    applications actually placed there add up to. They differ because packing
    stops when the next application would overshoot, and the gap is honest -
    real clusters are not filled to a round number either.
    """

    cluster_idx: int
    cluster_code: str
    environment: str
    target_pct: float
    effective_cpu: float
    effective_mem: float
    allocated_cpu: float = 0.0
    allocated_mem: float = 0.0
    allocated_storage: float = 0.0
    app_count: int = 0
    is_standby: bool = False

    @property
    def achieved_cpu_pct(self) -> float:
        return 0.0 if self.effective_cpu <= 0 else 100.0 * self.allocated_cpu / self.effective_cpu

    @property
    def achieved_mem_pct(self) -> float:
        return 0.0 if self.effective_mem <= 0 else 100.0 * self.allocated_mem / self.effective_mem

    @property
    def stress(self) -> float:
        """0.0 to 1.0, how hard this cluster is working.

        The single number everything downstream keys off: incident density,
        change failure rate, and which clusters a placement recommendation
        should steer away from. Taken as the worse of CPU and memory because a
        cluster out of memory is in trouble regardless of spare cores.
        """
        worst = max(self.achieved_cpu_pct, self.achieved_mem_pct)
        # Below 50% nothing is stressed; above 95% it is as stressed as it gets.
        return max(0.0, min(1.0, (worst - 50.0) / 45.0))


def assign_utilisation_targets(clusters, rng) -> dict:
    """Draw a target utilisation for every cluster, per UTILISATION_BANDS.

    Deterministic given the seeded rng. Sorted by code first so the assignment
    does not depend on the order clusters happen to be defined in - otherwise
    inserting one hand-crafted cluster would reshuffle the stress profile of the
    entire estate and every downstream incident with it.
    """
    ordered = sorted(clusters, key=lambda c: c.code)
    targets = {}
    band_for_index = []
    for share, low, high in UTILISATION_BANDS:
        band_for_index.extend([(low, high)] * max(1, round(share * len(ordered))))
    while len(band_for_index) < len(ordered):
        band_for_index.append(UTILISATION_BANDS[1][1:])
    rng.shuffle(band_for_index)

    for cluster, (low, high) in zip(ordered, band_for_index):
        # Non-production infrastructure runs cooler: it is sized for the peak of
        # a test cycle, not for sustained load.
        scale = 1.0 if cluster.environment == "Production" else 0.75
        drawn = round(rng.uniform(low, high) * scale, 2)
        if is_designated(cluster):
            # Draw anyway and discard, so adding or removing a tag never
            # reshuffles the rng for the other 248 clusters and every incident
            # downstream of them.
            cpu_end, mem_end = cluster.util_profile[1], cluster.util_profile[3]
            targets[cluster.idx] = round(max(cpu_end, mem_end), 2)
        else:
            targets[cluster.idx] = drawn
    return targets


def build_load_plans(clusters, rng) -> dict:
    """One ClusterLoadPlan per cluster, ready to be packed into."""
    targets = assign_utilisation_targets(clusters, rng)
    plans = {}
    for c in clusters:
        effective_cpu = c.total_cpu * (1.0 - c.reserved_cpu_pct / 100.0)
        effective_mem = c.total_mem_gb * (1.0 - c.reserved_mem_pct / 100.0)
        plans[c.idx] = ClusterLoadPlan(
            cluster_idx=c.idx, cluster_code=c.code, environment=c.environment,
            target_pct=targets[c.idx], effective_cpu=effective_cpu, effective_mem=effective_mem,
        )
    return plans


def generate_applications(existing, rng, anchor_date, app_def_factory) -> list:
    """Extend the hand-written applications up to TARGET_APPLICATIONS.

    The originals are kept untouched and first in the list. They carry the
    SCENARIOS map that the placement tests assert against - poor-fit
    applications, insufficient-resiliency clusters, nearing-capacity fixtures -
    and regenerating them procedurally would break every one of those tests for
    no gain.
    """
    generated = []
    next_idx = max(a.idx for a in existing) + 1
    used_codes = {a.code for a in existing}

    # Most applications in a real estate are small and unremarkable. The first
    # version of this used 8/22/42/28 with sizes to match and produced 12,183
    # cores of demand against 12,454 of supply - a 98% full estate before DR
    # standbys, which the packer then had to overflow. Capacity is the budget
    # here, not a preference.
    crit_weights = [("Critical", 0.05), ("High", 0.15), ("Medium", 0.40), ("Low", 0.40)]
    env_weights = [("Production", 0.55), ("Staging", 0.18), ("Test", 0.15), ("Development", 0.12)]

    def weighted(pairs):
        roll = rng.random()
        acc = 0.0
        for value, weight in pairs:
            acc += weight
            if roll <= acc:
                return value
        return pairs[-1][0]

    while len(existing) + len(generated) < TARGET_APPLICATIONS:
        dom_code, dom_name = rng.choice(_DOMAINS)
        fn_code, fn_name = rng.choice(_FUNCTIONS)
        seq = len(generated) + 1
        code = f"APP-{dom_code}-{fn_code}{seq:04d}"
        if code in used_codes:
            continue
        used_codes.add(code)

        criticality = weighted(crit_weights)
        environment = weighted(env_weights)
        # Size follows criticality: a Critical trading engine is not 2 cores, and
        # a Low-criticality export job is not 64. Without this the capacity
        # dimension sees noise instead of a distribution.
        #
        # Memory is deliberately modest relative to cores. It is the binding
        # constraint in this estate - 158 of 241 clusters run out of memory
        # before cores - so generous memory sizing does not produce a busy
        # estate, it produces one where every cluster is over target and the
        # utilisation bands mean nothing.
        if criticality == "Critical":
            cpu, mem = rng.choice([8, 12, 16, 24]), rng.choice([32, 48, 64, 96])
        elif criticality == "High":
            cpu, mem = rng.choice([4, 6, 8, 12]), rng.choice([16, 24, 32, 48])
        elif criticality == "Medium":
            cpu, mem = rng.choice([2, 3, 4, 6]), rng.choice([8, 12, 16, 24])
        else:
            cpu, mem = rng.choice([1, 2, 2, 3]), rng.choice([4, 4, 6, 8])
        storage = cpu * rng.choice([25, 40, 60, 100])

        platform = weighted([("Kubernetes", 0.45), ("OpenShift", 0.20), ("VMware", 0.25), ("Hyper-V", 0.07), ("BareMetal", 0.03)])
        os_req = "Linux/RHEL9" if platform in ("Kubernetes", "OpenShift", "BareMetal") else (
            "Windows/2022" if platform == "Hyper-V" else rng.choice(["Linux/RHEL9", "Linux/Ubuntu22", "Windows/2022"])
        )
        tier = {"Critical": "Tier-1", "High": "Tier-1", "Medium": "Tier-2", "Low": "Tier-3"}[criticality]
        classification = weighted([("Restricted", 0.10), ("Confidential", 0.28), ("Internal", 0.50), ("Public", 0.12)])

        generated.append(
            app_def_factory(
                idx=next_idx, code=code,
                name=f"{dom_name} {fn_name} {seq:04d}",
                criticality=criticality, environment=environment, platform=platform, os=os_req,
                cpu_req=float(cpu), mem_req_gb=float(mem), storage_req_gb=float(storage),
                growth_pct=float(rng.choice([2, 4, 5, 8, 10, 12, 15, 20])),
                avail_tier=tier, classification=classification,
                hosted_since=anchor_date - __import__("datetime").timedelta(days=rng.randint(120, 1400)),
            )
        )
        next_idx += 1

    return generated


def pack_applications(applications, clusters, load_plans, rng, anchor_date):
    """Place every application, filling clusters toward their target utilisation.

    Returns (hosting_rows, primary_cluster_by_app). Each application gets a
    Production-or-own-environment primary, a lower-environment copy or two, and -
    if it is Critical or High - a DR standby.

    WHY PACKING AND NOT RANDOM ASSIGNMENT
    -------------------------------------
    Random placement produces a uniform middle: every cluster ends up at roughly
    the same utilisation, so the capacity sub-score - the heaviest of the seven
    at 0.30 - cannot tell any two candidates apart, and placement silently falls
    through to static attributes. Packing toward a drawn target produces clusters
    that are genuinely stressed and clusters that are genuinely idle, which is
    what both right-sizing and placement need in order to have anything to say.

    The candidate filter is the same shape as RULE-001 and RULE-002: environment
    must match exactly, and the platform must be compatible. Seeding data that
    violates the rules the engine enforces would produce an estate the engine
    considers entirely ineligible.
    """
    from datetime import datetime

    by_env = {}
    for c in clusters:
        if c.lifecycle == "Active":
            by_env.setdefault(c.environment, []).append(c)

    rows = []
    primary_by_app = {}

    def capacity_left(plan, cpu, mem):
        """Headroom to the target, as a fraction, in the tighter of the two.

        Both dimensions, not just CPU. The first version measured CPU alone and
        packed to 97% of cores while memory reached 362% - the packer had no
        idea it was oversubscribing the resource that actually binds. In real
        virtualisation estates memory runs out first, and an estate seeded with
        362% memory allocation is not a stressed estate, it is a broken one.
        """
        cpu_room = plan.effective_cpu * plan.target_pct / 100.0 - plan.allocated_cpu
        mem_room = plan.effective_mem * plan.target_pct / 100.0 - plan.allocated_mem
        if cpu > cpu_room or mem > mem_room:
            return -1.0
        return min(cpu_room - cpu, mem_room - mem)

    def place(app, environment, cpu, mem, storage, is_primary, standby=False):
        pool = by_env.get(environment) or by_env.get("Production") or []
        if not pool:
            return None

        # A hand-written application names its own cluster, and for its PRIMARY
        # that choice wins over the packer.
        #
        # The obvious reading is that the packer should decide. It should not,
        # because host_cluster_code is already authoritative for two other
        # models and only the hosting rows ignored it:
        #
        #   seed_cmdb.py     places VMs from `a.host_cluster_code or by_idx[...]`
        #   generate_seed.py derives cluster utilisation from
        #                    `sum(1 for a in APPLICATIONS if a.host_cluster_code == c.code)`
        #
        # So the packer put APP-CRM in Atlanta, Columbus and Dallas while the CI
        # graph and the utilisation history both had it on den-03, in Denver.
        # Measured: 42 applications had a graph data centre appearing in NONE of
        # their hosting rows, and APP-LEDGER had zero overlap between the two.
        #
        # That is not a missing DR leg - it is a primary placement that two
        # halves of the seed disagree about, and every resiliency answer for the
        # forty flagship applications was computed over the disagreement.
        #
        # Only the primary is pinned. Staging, Test and the DR standby still pack
        # by capacity: host_cluster_code says where the production workload
        # lives, and nothing about where its copies go.
        pinned = None
        if is_primary and getattr(app, "host_cluster_code", ""):
            pinned = next((c for c in pool if c.code == app.host_cluster_code), None)
            if pinned is None:
                # Deliberate placements are few and written by hand, so a name
                # that does not resolve is a typo rather than a capacity
                # decision. Packing it elsewhere silently is precisely the
                # divergence being fixed here.
                raise ValueError(
                    f"{app.code} names host_cluster_code={app.host_cluster_code!r}, "
                    f"which is not an active {environment} cluster. Fix the "
                    f"application row or clear host_cluster_code; letting the "
                    f"packer choose is what made the graph and the hosting table "
                    f"disagree about 42 applications."
                )
        # Prefer a cluster that still has room under its target and can take the
        # platform. Sorted by remaining room so the estate fills evenly toward
        # its targets rather than overloading whichever cluster is examined first.
        eligible = [
            c for c in pool
            if (c.platform == app.platform or app.platform in ("Kubernetes", "OpenShift") and c.platform in ("Kubernetes", "OpenShift"))
            and capacity_left(load_plans[c.idx], cpu, mem) >= 0
        ]
        if standby:
            # A DR standby must not sit in the same data centre as the primary -
            # a failover to the building that just failed is not a failover.
            primary_idx = primary_by_app.get(app.idx)
            if primary_idx is not None:
                primary_dc = next((c.datacenter for c in clusters if c.idx == primary_idx), None)
                eligible = [c for c in eligible if c.datacenter != primary_dc]
        if pinned is None and not eligible:
            # Nothing under its drawn target can take it. Fall back to the
            # emptiest compatible cluster that still has REAL capacity - an
            # unhosted application is a data bug, but a cluster allocated past
            # its physical cores is a worse one. The first version of this
            # ignored capacity entirely and produced a 160% mean allocation:
            # every cluster stressed, which is the same uselessness as every
            # cluster empty, just in the other direction.
            eligible = [
                c for c in pool
                if load_plans[c.idx].allocated_cpu + cpu <= load_plans[c.idx].effective_cpu * 0.97
                and load_plans[c.idx].allocated_mem + mem <= load_plans[c.idx].effective_mem * 0.97
            ]
            if not eligible:
                return None
            eligible = sorted(eligible, key=lambda c: max(load_plans[c.idx].achieved_cpu_pct,
                                                          load_plans[c.idx].achieved_mem_pct))[:5]
        # A pinned primary is placed even when its cluster is over target. The
        # forty hand-written applications are a small fraction of the estate's
        # load, and honouring the choice is the point; silently relocating it is
        # the bug.
        cluster = pinned or max(eligible, key=lambda c: capacity_left(load_plans[c.idx], cpu, mem))

        plan = load_plans[cluster.idx]
        plan.allocated_cpu += cpu
        plan.allocated_mem += mem
        plan.allocated_storage += storage
        plan.app_count += 1
        if standby:
            plan.is_standby = True
        rows.append((
            app.idx, cluster.idx, None, environment,
            round(cpu, 2), round(mem, 2), round(storage, 2),
            "Active", 1 if is_primary else 0,
            datetime.combine(app.hosted_since, datetime.min.time()),
        ))
        return cluster.idx

    # Largest first: a 64-core application placed last would find nowhere with
    # room, and would then land in the fallback branch every time.
    for app in sorted(applications, key=lambda a: -a.cpu_req):
        cpu, mem, sto = float(app.cpu_req), float(app.mem_req_gb), float(app.storage_req_gb)
        primary_idx = place(app, app.environment, cpu, mem, sto, is_primary=True)
        if primary_idx is None:
            continue
        primary_by_app[app.idx] = primary_idx

        # Lower environments, sized down. Staging is a rehearsal of production,
        # not a copy of it - it is built to prove a release works, not to carry
        # the load, so it is routinely a third of the size.
        if app.environment == "Production":
            if rng.random() < 0.72:
                place(app, "Staging", cpu * 0.35, mem * 0.35, sto * 0.30, is_primary=False)
            if rng.random() < 0.55:
                place(app, rng.choice(["Test", "Development"]), cpu * 0.2, mem * 0.2, sto * 0.2, is_primary=False)

            # DR standby for the workloads that would actually be failed over.
            # Modelled as a second Production row with IsPrimary = 0, in another
            # data centre - the schema has no DR environment, and inventing one
            # would make RULE-001 refuse to place production workloads on it.
            if app.criticality in ("Critical", "High") and rng.random() < 0.8:
                place(app, "Production", cpu, mem, sto, is_primary=False, standby=True)

    return rows, primary_by_app


def derive_utilisation_profiles(clusters, load_plans, rng):
    """Rewrite each cluster's utilisation trend to agree with what it now hosts.

    compute_cluster_capacity() reads allocation from hosting rows and measurement
    from the ClusterUtilization time series, then takes the worse of the two. If
    the seed loads a cluster to 88% allocated while its measured series still
    says 12%, the estate contradicts itself: the screens show a nearly empty
    cluster that placement refuses to use, and neither number is wrong on its
    own terms.

    Measured therefore tracks allocated, a little below it - real workloads do
    not consume everything reserved for them - with a mild trend and noise so
    the forecasting path has something to fit.
    """
    for c in clusters:
        plan = load_plans.get(c.idx)
        if plan is None:
            continue
        if is_designated(c):
            # Its profile is the fixture. Packing already aimed at that level
            # via assign_utilisation_targets(), so allocated and measured agree
            # without rewriting the thing the tests assert.
            continue
        cpu_now = plan.achieved_cpu_pct
        mem_now = plan.achieved_mem_pct
        # Measured runs at 78-94% of allocated: reservations are not fully used.
        realised = rng.uniform(0.78, 0.94)
        cpu_end = max(2.0, min(97.0, cpu_now * realised))
        mem_end = max(2.0, min(97.0, mem_now * realised))
        # Six months ago it was lower - the estate has been filling up. Stressed
        # clusters grew faster, which is what makes a forecast on them alarming.
        growth = 1.0 + (0.10 + 0.35 * plan.stress)
        cpu_start = max(2.0, cpu_end / growth)
        mem_start = max(2.0, mem_end / growth)
        sto_end = max(5.0, min(96.0, (plan.allocated_storage / c.total_storage_gb * 100.0) if c.total_storage_gb else 20.0))
        sto_start = max(3.0, sto_end / 1.25)
        c.util_profile = (
            round(cpu_start, 2), round(cpu_end, 2),
            round(mem_start, 2), round(mem_end, 2),
            round(sto_start, 2), round(sto_end, 2),
        )
