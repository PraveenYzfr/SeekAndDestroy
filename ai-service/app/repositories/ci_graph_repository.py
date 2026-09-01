"""Traversal over sad.CiRelationship - the CMDB dependency graph.

DIRECTION IS THE WHOLE THING
----------------------------
One table, two opposite questions, and mixing them up produces a plausible number
that is entirely wrong:

  UPWARD   child -> parent, following ChildCiId = current.
           "What does this application STAND ON."
           An app's VMs, their physical hosts, the volumes they mount, the
           switches serving them, the zone and the data centre.
           This is what resiliency is computed from.

  DOWNWARD parent -> child, following ParentCiId = current.
           "What DIES if this fails."
           A storage array reaches its volumes, the VMs mounting them, the
           applications on those VMs, and the applications depending on those.
           This is blast radius.

Compute resiliency with the downward walk and you get the application's
dependents instead of its dependencies. Both are non-empty, both are the right
order of magnitude, and nothing about the result looks wrong. Hence two named
functions rather than one with a flag - a flag gets passed wrongly, a name has to
be read.

THREE GUARDS, ALL REQUIRED
--------------------------
1. Visited-path guard. Containment edges are acyclic by construction, but
   `Depends on::Used by` is not: two applications calling each other is a real
   topology and it is deliberately seeded. Without the guard the CTE recurses
   until the server stops it.

2. Depth ceiling in the WHERE clause. Bounds the walk semantically.

3. Explicit OPTION (MAXRECURSION n). This one is the trap. SQL Server's default
   is 100, and on exceeding it the statement ERRORS rather than truncating - but
   a ceiling set too low combined with a swallowed error is how you get a number
   that looks complete and is too small. Set it explicitly and well above the
   depth ceiling so the ceiling is what stops the walk, not the engine.

MAXRECURSION cannot be parameterised - SQL Server requires a literal - so it is
interpolated after an int() conversion rather than bound. Nothing user-supplied
reaches it.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.repositories.base import T, fetch_all

#: How deep a walk may go before it stops on its own terms.
DEFAULT_MAX_DEPTH = 20

#: Headroom above DEFAULT_MAX_DEPTH so the engine's limit is never what stops a
#: walk. If these two are ever equal, the ceiling stops being a semantic choice
#: and becomes a race with the query planner.
MAXRECURSION = 200

#: Edge type ids, from sad.CiRelationshipType. Named rather than inlined because
#: `TypeId = 4` at a call site is unreadable and `TypeId = 6` doubly so - type 6
#: carries both storage and network, which is not guessable from the number.
RUNS_ON = 1  # Runs on::Runs        parent = VM (or cluster today), child = app
HOSTED_ON = 2  # Hosted on::Hosts     parent = physical host, child = VM
MEMBER_OF = 3  # Member of::Members   parent = cluster, child = host
DEPENDS_ON = 4  # Depends on::Used by  parent = provider, child = consumer
LOCATED_IN = 5  # Located in::Contains parent = site, child = contained
PROVIDES = 6  # Provides::Uses       parent = array/volume/switch, child = consumer

#: Containment plus provision: the edges that answer "what holds this up".
#: `Depends on::Used by` is deliberately EXCLUDED from resiliency. A dependency on
#: another application is a real risk, but it is not a failure domain this
#: application is redundant across - counting it would inflate the redundancy of
#: an app simply because it calls many services.
SUPPORT_EDGES = (RUNS_ON, HOSTED_ON, MEMBER_OF, LOCATED_IN, PROVIDES)

#: Structural edges only. Used when descending from a cluster to find hardware,
#: where following `Depends on::Used by` would wander into other applications'
#: estates and return machines that have nothing to do with this cluster.
CONTAINMENT_EDGES = (RUNS_ON, HOSTED_ON, MEMBER_OF, LOCATED_IN)

#: ServiceNow class names, as stored in sad.ConfigurationItem.ClassName.
CLASS_SERVER = "cmdb_ci_server"
#: A cluster NODE is a membership record - a machine's share of a cluster, with
#: the utilisation history attached. A SERVER is the machine. They were one row
#: until migration 011, and conflating them produced hardware that averaged 7
#: cores and 27GB because the row carried total_cpu / node_count.
#:
#: Counting nodes where you meant servers is the dangerous version of this
#: mistake: today there is one server per node, so the two counts AGREE, and
#: they diverge silently the moment anything shares hardware. Failure domains
#: are counted in SERVERS - a machine is what fails.
CLASS_CLUSTER_NODE = "cmdb_ci_cluster_node"
CLASS_VM = "cmdb_ci_vm_instance"
CLASS_CLUSTER = "cmdb_ci_cluster"
CLASS_ZONE = "cmdb_ci_zone"
CLASS_DATACENTER = "cmdb_ci_datacenter"
CLASS_STORAGE_ARRAY = "cmdb_ci_storage_array"
CLASS_STORAGE_VOLUME = "cmdb_ci_storage_volume"
CLASS_NETWORK = "cmdb_ci_netgear"
CLASS_APPLICATION = "cmdb_ci_appl"


@dataclass(frozen=True)
class Walk:
    """A traversal result, carrying its own truncation signal.

    `nodes` alone is a trap. The projection aggregates MIN(Depth) per CI, which
    is the right depth to report - but it destroys the evidence that anything
    was cut. A node reached at depth 3 by a short path may ALSO have been on a
    longer path that hit the ceiling and stopped before reaching something else
    entirely, and no surviving depth in the result shows that.

    `hit_ceiling` is computed from the deepest RAW depth, before aggregation, so
    it survives the projection.

    Conservative by design: a branch that ended naturally exactly at the ceiling
    is indistinguishable from one that was truncated there, and both set the
    flag. That is the right direction to be wrong in. Reporting "at least 7" when
    the truth is exactly 7 costs a reader nothing; reporting a bare "7" when the
    truth is more is the wrong-number bug this guard exists to prevent.

    Raised by seekanddestroy-c2, who wanted to consume this traversal rather than
    keep a second copy of the guard logic.
    """

    nodes: tuple[GraphNode, ...]
    #: True when any path reached the depth ceiling - the result may be partial.
    hit_ceiling: bool
    #: The ceiling that was REQUESTED, not the depth actually reached. The name
    #: reads like it could be either; it is the cap. For how deep the walk got,
    #: use `observed_depth`. Flagged by seekanddestroy-c2, who had to work it out
    #: from behaviour rather than read it.
    max_depth: int

    def __iter__(self):
        return iter(self.nodes)

    def __len__(self) -> int:
        return len(self.nodes)

    @property
    def observed_depth(self) -> int:
        """How deep the walk actually reached. 0 for an empty walk.

        Distinct from `max_depth`, which is the requested cap. Both callers of
        this module computed it by hand before it existed, which is the usual
        sign it belongs here.
        """
        return max((n.depth for n in self.nodes), default=0)

    def of_class(self, class_name: str) -> tuple[GraphNode, ...]:
        return tuple(n for n in self.nodes if n.class_name == class_name)


@dataclass(frozen=True)
class GraphNode:
    ci_id: int
    name: str
    class_name: str
    #: Hops from the starting CI. 1 is a direct parent (or child, walking down).
    depth: int


def _walk(
    start_ci_id: int,
    *,
    upward: bool,
    max_depth: int,
    edge_types: tuple[int, ...] | None,
) -> Walk:
    """One recursive CTE, parameterised by direction.

    `upward` swaps which column is matched and which is collected. Everything
    else - the guards, the path accumulation, the projection - is identical, and
    duplicating the SQL to avoid the flag would mean fixing every guard twice.
    The flag is private; callers get two named functions.
    """
    depth = int(max_depth)
    if depth < 1:
        return Walk(nodes=(), hit_ceiling=False, max_depth=depth)

    # Walking up: match the child, collect the parent. Walking down: the reverse.
    match_col, collect_col = ("ChildCiId", "ParentCiId") if upward else ("ParentCiId", "ChildCiId")

    params: dict = {"start": start_ci_id, "max_depth": depth}
    edge_filter = ""
    if edge_types:
        # Bound individually - a joined string would be an injection seam even
        # with ints, and it defeats plan caching.
        names = []
        for i, t in enumerate(edge_types):
            key = f"t{i}"
            params[key] = int(t)
            names.append(f":{key}")
        edge_filter = f"AND r.TypeId IN ({', '.join(names)})"

    sql = f"""
    WITH walk AS (
        SELECT r.{collect_col} AS CiId,
               1 AS Depth,
               CAST('/' + CAST(:start AS varchar(20)) + '/'
                    + CAST(r.{collect_col} AS varchar(20)) + '/' AS varchar(4000)) AS Path
        FROM {T('CiRelationship')} r
        WHERE r.{match_col} = :start {edge_filter}

        UNION ALL

        SELECT r.{collect_col},
               w.Depth + 1,
               CAST(w.Path + CAST(r.{collect_col} AS varchar(20)) + '/' AS varchar(4000))
        FROM walk w
        JOIN {T('CiRelationship')} r ON r.{match_col} = w.CiId {edge_filter}
        -- Cycle guard. Dependency edges are genuinely cyclic; without this the
        -- walk does not terminate.
        WHERE w.Path NOT LIKE '%/' + CAST(r.{collect_col} AS varchar(20)) + '/%'
          AND w.Depth < :max_depth
    )
    SELECT w.CiId, MIN(w.Depth) AS Depth, MAX(w.Depth) AS DeepestDepth,
           ci.Name, ci.ClassName
    FROM walk w
    JOIN {T('ConfigurationItem')} ci ON ci.CiId = w.CiId
    GROUP BY w.CiId, ci.Name, ci.ClassName
    OPTION (MAXRECURSION {MAXRECURSION})
    """
    # MIN(Depth): a CI reachable by several paths is reported at its shortest,
    # which is the one a human would quote.
    #
    # MAX(Depth) is selected purely to recover the truncation signal. The maximum
    # over the per-CI groups equals the maximum over the raw pre-aggregation
    # rows, so this is exact - and it costs one more column rather than a second
    # evaluation of a recursive CTE.
    rows = fetch_all(sql, params, max_rows=100_000)
    return Walk(
        nodes=tuple(
            GraphNode(ci_id=r["CiId"], name=r["Name"], class_name=r["ClassName"], depth=r["Depth"])
            for r in rows
        ),
        hit_ceiling=any(r["DeepestDepth"] >= depth for r in rows),
        max_depth=depth,
    )


def support_graph(
    ci_id: int,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    edge_types: tuple[int, ...] | None = SUPPORT_EDGES,
) -> Walk:
    """Everything this CI stands on. Upward: child -> parent.

    Resiliency is computed from this. See the module docstring on why the
    direction is not interchangeable with blast_radius.
    """
    return _walk(ci_id, upward=True, max_depth=max_depth, edge_types=edge_types)


def blast_radius(
    ci_id: int,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    edge_types: tuple[int, ...] | None = None,
) -> Walk:
    """Everything that fails if this CI fails. Downward: parent -> child.

    Unlike support_graph this includes `Depends on::Used by` by default: when a
    provider dies, its consumers are genuinely affected, and excluding them
    would understate the damage.
    """
    return _walk(ci_id, upward=False, max_depth=max_depth, edge_types=edge_types)


def servers_under_clusters(cluster_ci_ids: list[int], *, max_depth: int = 3) -> list[GraphNode]:
    """Physical servers belonging to these clusters, whatever the shape between.

    Needed because of an asymmetry in the current topology: an application is
    attached to its cluster and hardware hangs beneath that cluster, so hosts are
    SIBLINGS of the application rather than ancestors of it, and a pure upward
    walk never reaches them. Once VM instances sit between application and server
    the upward walk reaches hardware directly and this stops being needed.

    WHY THIS IS NOT ONE JOIN

    It was, and the join was `cluster -[Member of]-> ClassName='cmdb_ci_server'`.
    That is correct for the shape where clusters contain servers directly and
    returns NOTHING, silently, for the shape migration 011 introduces, where a
    cluster contains NODES and a node relates to a SERVER. An empty host count
    does not raise; it just reports fewer failure domains than exist, and
    under-counting a failure domain manufactures a single point of failure.

    seekanddestroy-e7 also flagged that the server-to-node edge DIRECTION may
    still flip - whether a node is the parent of its server or the child is not
    settled. So this resolves by CLASS rather than by asserting a direction:

      1. everything reachable downward from the cluster, bounded
      2. servers among them - covers cluster -> server and cluster -> node -> server
      3. plus servers that are PARENTS of any node found - covers the flip

    Both directions cost one bounded query each. Hardcoding the direction that
    happens to be true tonight costs a rewrite and, until the rewrite, a wrong
    number that looks right.
    """
    if not cluster_ci_ids:
        return []

    found: dict[int, GraphNode] = {}
    node_ids: list[int] = []
    for cluster_id in cluster_ci_ids:
        for n in _walk(cluster_id, upward=False, max_depth=max_depth, edge_types=CONTAINMENT_EDGES):
            if n.class_name == CLASS_SERVER:
                found[n.ci_id] = n
            elif n.class_name == CLASS_CLUSTER_NODE:
                node_ids.append(n.ci_id)

    # The flip case: server sits ABOVE the node rather than below it.
    if node_ids:
        params = {f"n{i}": int(v) for i, v in enumerate(sorted(set(node_ids)))}
        placeholders = ", ".join(f":{k}" for k in params)
        params["cls"] = CLASS_SERVER
        rows = fetch_all(
            f"SELECT DISTINCT ci.CiId, ci.Name, ci.ClassName "
            f"FROM {T('CiRelationship')} r "
            f"JOIN {T('ConfigurationItem')} ci ON ci.CiId = r.ParentCiId "
            f"WHERE r.ChildCiId IN ({placeholders}) AND ci.ClassName = :cls",
            params,
            max_rows=100_000,
        )
        for r in rows:
            found.setdefault(
                r["CiId"],
                GraphNode(ci_id=r["CiId"], name=r["Name"], class_name=r["ClassName"], depth=2),
            )

    return list(found.values())


#: Backwards-compatible alias. The old name said "members", which is now exactly
#: the thing this must NOT return - cluster members are nodes.
cluster_members = servers_under_clusters


def ci_for_application(application_code: str) -> GraphNode | None:
    """The CI representing an application, by its business code.

    Matched on Name because that is what the seed writes; if the CMDB later
    grows a proper correlation column this is the single place to change.
    """
    rows = fetch_all(
        f"SELECT TOP 1 CiId, Name, ClassName FROM {T('ConfigurationItem')} "
        f"WHERE ClassName = :cls AND Name = :name",
        {"cls": CLASS_APPLICATION, "name": application_code},
        max_rows=1,
    )
    if not rows:
        return None
    r = rows[0]
    return GraphNode(ci_id=r["CiId"], name=r["Name"], class_name=r["ClassName"], depth=0)
