"""The CMDB layer: configuration items, the estate around the applications, and
the relationship graph that makes failure domains computable.

WHY THIS MODULE EXISTS
----------------------
Before it, the seed produced applications, clusters and the hosts that run them,
and nothing else. Every server in the estate existed to host an application. A
bank's floor is not like that: most boxes are domain controllers, DNS, PKI, backup
media, log collectors and jump hosts, and none of them appear in any application's
hosting record. There was also no VM layer, so "four VMs landing on two physical
hosts" could not be said, and no storage, so the sharper version - "four VMs whose
datastores come off one NAS head" - could not be said either.

IDENTITY IS ASSIGNED HERE, NOT BY THE DATABASE
----------------------------------------------
CiId values are allocated in this module and emitted with IDENTITY_INSERT, because
the relationship rows need to reference them and a generator cannot read back an
IDENTITY it has not inserted yet. That also makes the whole graph deterministic:
the same seed produces the same CiIds, so a golden set can name one.

SysId is a digest of class plus natural key rather than a random value, so the same
logical thing keeps the same identity across a regeneration. That is the entire
point of having a SysId beside an integer key.

WHAT IS DELIBERATELY BROKEN
---------------------------
A real CMDB is never clean, and a health check that only ever returns zero has not
been tested. Orphans, stale records, duplicates, unowned and unclassified CIs are
planted on purpose, as are five single-point-of-failure topologies and one
dependency cycle. Every one is exported through SCENARIOS so tests assert against
a fixture rather than a number somebody read once.

The control matters as much as the defects: WELL_DISTRIBUTED applications are
genuinely spread across hosts, volumes, switches, zones and sites. A rule that
fires on those fires on everything.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# =============================================================================
# Scale
# =============================================================================
# Tunable because the estate is going to grow again: Praveen wants 10,000 servers,
# which means raising cluster node counts, which re-runs packing and utilisation
# and therefore the forecast fixtures. That is its own change with the test suite
# as the gate, not a constant quietly bumped at the end of a long session.
INFRA_SERVERS = 7_993          # servers that belong to no cluster
#
# 2,007 cluster-member hosts + 7,993 standalone = 10,000 servers.
#
# The split is the point. A bank does not run everything on clustered
# virtualisation: the clusters are the hypervisor estate, and most of the
# floor is standalone - physical database hosts too large or too licensed to
# virtualise, batch farms, integration and web tiers, plus the shared services
# nobody counts until they fail. None of them appear in any application's
# hosting record, which is exactly why the old model could not see them.
#
# Cluster node counts are deliberately NOT raised to reach the total.
# per_node_cpu is total_cpu / node_count, so tripling node_count would divide
# a 200-core cluster into 7-core nodes - a bigger number describing a less
# realistic estate.
VMS_PER_HYPERVISOR = 15        # consolidation ratio on the app-hosting hosts
STORAGE_ARRAYS_PER_DC = 15
VOLUMES_PER_ARRAY = 50
NETWORK_DEVICES_PER_DC = 100
DB_INSTANCES = 2_500
LOAD_BALANCERS = 400
BUSINESS_SERVICES = 120

#: Infrastructure server roles and their share of INFRA_SERVERS. Weighted the way
#: a real floor is: shared services and storage outnumber authentication, and
#: nothing is a round number because real estates are not.
INFRA_ROLE_MIX = [
    # workload tiers - most of a real floor, and entirely absent before
    ("DatabaseServer", 0.148), ("AppServer", 0.121), ("WebServer", 0.083),
    ("BatchServer", 0.074), ("IntegrationServer", 0.052), ("EtlServer", 0.041),
    ("ReportingServer", 0.036), ("CacheServer", 0.029), ("SearchServer", 0.023),
    ("ApiGateway", 0.027), ("Middleware", 0.058), ("MessageBroker", 0.039),
    # shared services
    ("FileServer", 0.046), ("DNS", 0.028), ("Proxy", 0.024), ("ConfigMgmt", 0.021),
    ("JumpHost", 0.018), ("ArtifactRepo", 0.016), ("SMTPRelay", 0.014),
    ("PrintServer", 0.011), ("NTP", 0.009),
    # storage and protection
    ("StorageController", 0.038), ("BackupMedia", 0.031), ("TapeLibrary", 0.008),
    # observability
    ("Monitoring", 0.026), ("LogCollector", 0.022), ("SIEM", 0.013),
    # authentication and directory
    ("DomainController", 0.019), ("LDAP", 0.012), ("IAM", 0.010),
    ("PKI", 0.008), ("RADIUS", 0.006), ("MFA", 0.005),
]

#: Which zone type each role belongs in. Storage, authentication, network and
#: management are separated from application compute rather than mixed into it,
#: which is how a floor is laid out and what makes a zone-level question return
#: something coherent.
ROLE_ZONE = {
    # Workload tiers sit in compute alongside the clusters they serve.
    "DatabaseServer": "Compute", "AppServer": "Compute", "WebServer": "Compute",
    "BatchServer": "Compute", "IntegrationServer": "Compute", "EtlServer": "Compute",
    "ReportingServer": "Compute", "CacheServer": "Compute", "SearchServer": "Compute",
    "ApiGateway": "Network", "Middleware": "Compute", "MessageBroker": "Core",
    "FileServer": "Core", "DNS": "Core", "Proxy": "Network", "ConfigMgmt": "Management",
    "JumpHost": "Management", "ArtifactRepo": "Management", "SMTPRelay": "Core",
    "PrintServer": "Core", "NTP": "Core",
    "StorageController": "Storage", "BackupMedia": "Storage", "TapeLibrary": "Storage",
    "Monitoring": "Management", "LogCollector": "Management", "SIEM": "Management",
    "DomainController": "Core", "LDAP": "Core", "IAM": "Core",
    "PKI": "Core", "RADIUS": "Core", "MFA": "Core",
}

#: Physical server hardware. A node's share of a cluster is 7 cores on average in
#: this estate; a machine is not. These are the boxes that share is taken from -
#: two-socket, 32 to 96 cores, 256GB to 1.5TB, which is what a bank actually racks.
SERVER_SKUS = [
    ("Dell",  "PowerEdge R650",  2, 16, 256),
    ("Dell",  "PowerEdge R750",  2, 24, 512),
    ("HPE",   "ProLiant DL380",  2, 24, 512),
    ("HPE",   "ProLiant DL580",  4, 24, 1024),
    ("Lenovo","ThinkSystem SR650",2, 32, 768),
    ("Cisco", "UCS C240 M6",     2, 32, 1024),
    ("Dell",  "PowerEdge R760",  2, 48, 1536),
]

_ARRAY_VENDORS = ["NetApp", "Dell EMC", "Pure Storage", "HPE", "Hitachi"]
_SWITCH_VENDORS = ["Cisco", "Arista", "Juniper"]
_DB_ENGINES = [("SQLServer", "2019"), ("PostgreSQL", "14"), ("Oracle", "19c"), ("MongoDB", "6.0")]
_SERVICE_TIERS = [("Platinum", 0.08), ("Gold", 0.22), ("Silver", 0.40), ("Bronze", 0.30)]


def stable_hash(text: str) -> int:
    """Deterministic integer from a string.

    NOT builtins.hash(): Python salts string hashing per process, so hash("x")
    differs between runs unless PYTHONHASHSEED is pinned. Using it here made the
    estate non-reproducible - the same seed produced a different topology every
    time, which silently breaks the byte-for-byte guarantee the golden set and
    test_seed_determinism both depend on.
    """
    return int(hashlib.md5(text.encode()).hexdigest()[:8], 16)


def sys_id(kind: str, natural_key: str) -> str:
    """Stable 32-character identity for a CI.

    Derived rather than random so a regenerated estate keeps the same identity for
    the same logical thing - the property that makes SysId worth having at all.
    """
    return hashlib.md5(f"{kind}:{natural_key}".encode()).hexdigest()


@dataclass
class Ci:
    ci_id: int
    sys_id: str
    name: str
    class_name: str
    operational_status: str = "Operational"
    install_status: str = "Installed"
    environment: str | None = None
    support_group_id: int | None = None
    owned_by_id: int | None = None
    managed_by_id: int | None = None
    data_classification: str | None = None
    regulatory_scope: str | None = None
    first_discovered: str | None = None
    last_discovered: str | None = None
    discovery_source: str | None = None


@dataclass
class Estate:
    """Everything this module produces, ready for emission."""
    cis: list[Ci] = field(default_factory=list)
    edges: list[tuple[int, int, int]] = field(default_factory=list)   # parent, child, type
    data_centres: list[dict] = field(default_factory=list)
    zones: list[dict] = field(default_factory=list)
    servers: list[dict] = field(default_factory=list)
    infra_servers: list[dict] = field(default_factory=list)
    vms: list[dict] = field(default_factory=list)
    arrays: list[dict] = field(default_factory=list)
    volumes: list[dict] = field(default_factory=list)
    switches: list[dict] = field(default_factory=list)
    databases: list[dict] = field(default_factory=list)
    services: list[dict] = field(default_factory=list)
    balancers: list[dict] = field(default_factory=list)
    scenarios: dict[str, list] = field(default_factory=dict)

    _next_id: int = 1

    def add_ci(self, kind: str, natural_key: str, name: str, class_name: str, **kw) -> Ci:
        ci = Ci(self._next_id, sys_id(kind, natural_key), name, class_name, **kw)
        self._next_id += 1
        self.cis.append(ci)
        return ci

    def link(self, parent: int, child: int, type_id: int) -> None:
        """Parent is the container or the depended-upon, always.

        Blast radius walks parent -> child; resiliency walks child -> parent. They
        are not interchangeable, and a resiliency figure computed in the blast
        direction returns an application's dependents instead of its dependencies -
        a plausible number that is entirely wrong and does not look it.
        """
        if parent != child:
            self.edges.append((parent, child, type_id))


# =============================================================================
# Weighted choice that consumes exactly one draw
# =============================================================================
def _weighted(rng, pairs):
    roll = rng.random()
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if roll <= acc:
            return value
    return pairs[-1][0]



# =============================================================================
# Build
# =============================================================================
def build(clusters, applications, nodes, rng, anchor_date,
          neighborhood_index: dict) -> Estate:
    """Construct the whole CMDB layer.

    ``nodes`` are the existing cluster-member hosts. They stay in sad.ClusterNode
    and act as the hypervisors; the infrastructure servers created here are a
    disjoint set living in sad.CiServer, because ClusterNode.ClusterId is NOT NULL
    and the scoring path relies on every node having a cluster.
    """
    e = Estate()
    # ANCHOR_DATE is a date, not a datetime - the discovery columns are
    # DATETIME2 so they need a time component appended rather than assumed.
    stamp = f"{anchor_date.isoformat()} 00:00:00"
    old = f"{anchor_date.replace(year=anchor_date.year - 1).isoformat()} 00:00:00"

    # ---- data centres -------------------------------------------------------
    dc_ci: dict[str, int] = {}
    for name in sorted({c.datacenter for c in clusters}):
        ci = e.add_ci("dc", name, name, "cmdb_ci_datacenter", environment="Production",
                      discovery_source="Import", first_discovered=stamp, last_discovered=stamp)
        dc_ci[name] = ci.ci_id
        e.data_centres.append({
            "ci_id": ci.ci_id, "code": name.replace(" ", "-"), "city": name.split("-")[0],
            "region": next(c.region for c in clusters if c.datacenter == name)})

    # ---- zones: the existing compute ones, plus four typed ones per site ----
    zone_ci: dict[str, int] = {}
    for code in sorted({c.neighborhood_code for c in clusters if c.neighborhood_code}):
        dc = next(c.datacenter for c in clusters if c.neighborhood_code == code)
        ci = e.add_ci("zone", code, code, "cmdb_ci_zone", environment="Production",
                      discovery_source="Import", first_discovered=stamp, last_discovered=stamp)
        zone_ci[code] = ci.ci_id
        e.zones.append({"ci_id": ci.ci_id, "code": code, "dc": dc, "type": "Compute",
                        "nid": neighborhood_index[code], "is_new": False})
        e.link(dc_ci[dc], ci.ci_id, 5)

    # The typed zones do not exist in sad.Neighborhood yet. Ids are allocated
    # here and emitted under IDENTITY_INSERT rather than left to insert order,
    # because a foreign key that depends on the sequence of two separate
    # INSERT blocks is a foreign key that breaks the first time one moves.
    next_nid = max(neighborhood_index.values()) + 1
    typed_zone: dict[tuple, int] = {}
    typed_zone_nid: dict[tuple, int] = {}
    for dc_name in sorted(dc_ci):
        prefix = dc_name.split("-")[0][:3].upper()
        for ztype, suffix in (("Storage", "STOR"), ("Core", "CORE"),
                              ("Network", "NET"), ("Management", "MGMT")):
            code = f"NH-{prefix}-{suffix}-01"
            ci = e.add_ci("zone", code, code, "cmdb_ci_zone", environment="Production",
                          discovery_source="Import", first_discovered=stamp, last_discovered=stamp)
            typed_zone[(dc_name, ztype)] = ci.ci_id
            typed_zone_nid[(dc_name, ztype)] = next_nid
            zone_ci[code] = ci.ci_id
            e.zones.append({"ci_id": ci.ci_id, "code": code, "dc": dc_name, "type": ztype,
                            "nid": next_nid, "is_new": True,
                            "name": f"{ztype} zone, {dc_name}", "region":
                            next(c.region for c in clusters if c.datacenter == dc_name)})
            next_nid += 1
            e.link(dc_ci[dc_name], ci.ci_id, 5)

    # ---- clusters and their member hosts ------------------------------------
    cluster_ci: dict[str, int] = {}
    for c in clusters:
        ci = e.add_ci("cluster", c.code, c.code, "cmdb_ci_cluster", environment=c.environment,
                      data_classification=c.compliance, discovery_source="Discovery",
                      first_discovered=stamp, last_discovered=stamp)
        cluster_ci[c.code] = ci.ci_id
        if c.neighborhood_code in zone_ci:
            e.link(zone_ci[c.neighborhood_code], ci.ci_id, 5)

    # A node is a membership; a server is a machine. They were one row, which is
    # why a "server" in this estate averaged 7 cores - it was carrying the
    # cluster's capacity divided by its member count, which is the right number
    # for a share and the wrong number for hardware. Both now exist.
    node_ci: dict[str, int] = {}
    server_ci: dict[str, int] = {}
    for n in nodes:
        ci = e.add_ci("node", n.host_name, n.host_name, "cmdb_ci_cluster_node",
                      environment=n.cluster.environment, discovery_source="Discovery",
                      first_discovered=stamp, last_discovered=stamp)
        node_ci[n.host_name] = ci.ci_id
        e.link(cluster_ci[n.cluster.code], ci.ci_id, 3)

        vendor, model, sockets, per_socket, memory = SERVER_SKUS[
            stable_hash(n.host_name) % len(SERVER_SKUS)]
        host = f"{n.host_name}-esx"
        sci = e.add_ci("server", host, host, "cmdb_ci_server",
                       environment=n.cluster.environment, discovery_source="Discovery",
                       first_discovered=old, last_discovered=stamp)
        server_ci[n.host_name] = sci.ci_id
        e.servers.append({
            "ci_id": sci.ci_id, "name": host, "role": "Hypervisor",
            "zone_ci": None, "zone_nid": neighborhood_index.get(n.cluster.neighborhood_code),
            "cluster_code": n.cluster.code, "cluster_id": n.cluster.idx,
            "cpu": sockets * per_socket, "memory_gb": memory,
            "storage_gb": rng.choice([960, 1920, 3840]),
            "os": n.cluster.os, "sockets": sockets, "cores_per_socket": per_socket,
            "vendor": vendor, "model": model, "hypervisor": 1,
            "rack": f"R{(stable_hash(host) % 40) + 1:02d}-U{(stable_hash(host) % 40) + 4:02d}",
            "node_host": n.host_name,
        })
        # The server RUNS the node: hardware is the parent, membership the child,
        # so a server failure propagates down to the cluster that depended on it.
        e.link(sci.ci_id, ci.ci_id, 1)

    # ---- applications -------------------------------------------------------
    app_ci: dict[str, int] = {}
    for a in applications:
        ci = e.add_ci("appl", a.code, a.code, "cmdb_ci_appl", environment=a.environment,
                      data_classification=a.classification, support_group_id=a.sg_idx,
                      owned_by_id=a.owner_idx, discovery_source="Manual",
                      first_discovered=stamp, last_discovered=stamp)
        app_ci[a.code] = ci.ci_id

    # ---- storage: arrays, then the volumes they provide ---------------------
    # The volume is the shared failure domain, not the array. Two clusters that
    # are independent on paper and mount the same export are not independent.
    for dc_name in sorted(dc_ci):
        prefix = dc_name.split("-")[0][:3].lower()
        for i in range(1, STORAGE_ARRAYS_PER_DC + 1):
            kind = "NAS" if i % 3 else "SAN"
            proto = "NFS" if kind == "NAS" else "FC"
            name = f"{prefix}-{kind.lower()}-{i:02d}"
            raw = rng.choice([120, 240, 480, 960])
            ci = e.add_ci("array", name, name, "cmdb_ci_storage_array", environment="Production",
                          discovery_source="Discovery", first_discovered=old, last_discovered=stamp)
            e.arrays.append({
                "ci_id": ci.ci_id, "name": name, "vendor": rng.choice(_ARRAY_VENDORS),
                "type": kind, "protocol": proto, "raw_tb": raw,
                "usable_tb": round(raw * 0.72, 2),
                "used_tb": round(raw * rng.uniform(0.35, 0.88), 2),
                "controllers": rng.choice([2, 2, 2, 4]),
                "zone_ci": typed_zone[(dc_name, "Storage")],
                "zone_nid": typed_zone_nid[(dc_name, "Storage")]})
            e.link(typed_zone[(dc_name, "Storage")], ci.ci_id, 5)

            for v in range(1, VOLUMES_PER_ARRAY + 1):
                vname = f"{name}-vol{v:03d}"
                vci = e.add_ci("volume", vname, vname, "cmdb_ci_storage_volume",
                               environment="Production", discovery_source="Discovery",
                               first_discovered=old, last_discovered=stamp)
                cap = rng.choice([512, 1024, 2048, 4096])
                e.volumes.append({
                    "ci_id": vci.ci_id, "name": vname, "array_ci": ci.ci_id,
                    "capacity_gb": cap, "used_gb": int(cap * rng.uniform(0.2, 0.9)),
                    "protocol": proto, "export": f"/vol/{vname}",
                    "tier": rng.choice(["Gold", "Silver", "Silver", "Bronze"]),
                    "replicated": 1 if rng.random() < 0.35 else 0})
                e.link(ci.ci_id, vci.ci_id, 6)

    # ---- network ------------------------------------------------------------
    for dc_name in sorted(dc_ci):
        prefix = dc_name.split("-")[0][:3].lower()
        for i in range(1, NETWORK_DEVICES_PER_DC + 1):
            role = "TopOfRack" if i % 10 else ("Aggregation" if i % 20 else "Core")
            name = f"{prefix}-sw-{i:03d}"
            ci = e.add_ci("netgear", name, name, "cmdb_ci_netgear", environment="Production",
                          discovery_source="Discovery", first_discovered=old, last_discovered=stamp)
            e.switches.append({
                "ci_id": ci.ci_id, "name": name, "role": role,
                "vendor": rng.choice(_SWITCH_VENDORS), "ports": rng.choice([24, 48, 96]),
                "zone_ci": typed_zone[(dc_name, "Network")],
                "zone_nid": typed_zone_nid[(dc_name, "Network")]})
            e.link(typed_zone[(dc_name, "Network")], ci.ci_id, 5)

    switches_by_dc: dict[str, list] = {}
    for z in e.zones:
        if z["type"] == "Network":
            switches_by_dc[z["dc"]] = [s["ci_id"] for s in e.switches if s["zone_ci"] == z["ci_id"]]

    # Every host is served by a switch in its own site. This edge is what makes
    # "two hosts in different racks, one top-of-rack pair" computable at all.
    for n in nodes:
        pool = switches_by_dc.get(n.cluster.datacenter) or []
        if pool:
            e.link(pool[sum(ord(ch) for ch in n.host_name) % len(pool)], server_ci[n.host_name], 6)

    return _build_virtual(e, clusters, applications, nodes, node_ci, server_ci,
                          app_ci, cluster_ci, typed_zone, typed_zone_nid,
                          dc_ci, rng, stamp, old)

# =============================================================================
# The virtual layer, the infrastructure estate, and the planted failure domains
# =============================================================================
def _build_virtual(e, clusters, applications, nodes, node_ci, server_ci,
                   app_ci, cluster_ci, typed_zone, typed_zone_nid,
                   dc_ci, rng, stamp, old):
    """VMs on hosts, applications on VMs, and the shared dependencies that decide
    whether redundancy is real.

    The VM layer is the reason this module exists. Without it an application is
    attached directly to a cluster and resiliency can only count nodes; with it,
    the question becomes how many DISTINCT physical parents, volumes and switches
    sit behind the thing - which is a different number and often a much smaller
    one.
    """
    volumes_by_zone: dict[int, list[dict]] = {}
    for v in e.volumes:
        arr = next(a for a in e.arrays if a["ci_id"] == v["array_ci"])
        volumes_by_zone.setdefault(arr["zone_ci"], []).append(v)

    dc_of_cluster = {c.code: c.datacenter for c in clusters}
    nodes_by_cluster: dict[str, list] = {}
    for n in nodes:
        nodes_by_cluster.setdefault(n.cluster.code, []).append(n)

    # ---- VMs, hosted on the cluster member hosts ----------------------------
    vm_seq = 0
    vms_by_cluster: dict[str, list[dict]] = {}
    for c in clusters:
        pool = nodes_by_cluster.get(c.code) or []
        if not pool:
            continue
        store_zone = typed_zone[(c.datacenter, "Storage")]
        vols = volumes_by_zone.get(store_zone) or []
        for host in pool:
            for _ in range(VMS_PER_HYPERVISOR):
                vm_seq += 1
                name = f"vm-{vm_seq:06d}"
                ci = e.add_ci("vm", name, name, "cmdb_ci_vm_instance",
                              environment=c.environment, discovery_source="Discovery",
                              first_discovered=old, last_discovered=stamp)
                vol = vols[(vm_seq * 7) % len(vols)] if vols else None
                rec = {"ci_id": ci.ci_id, "name": name,
                       "vcpu": rng.choice([2, 4, 4, 8, 8, 16]),
                       "memory_gb": rng.choice([8, 16, 16, 32, 64]),
                       "disk_gb": rng.choice([80, 120, 250, 500]),
                       "host_ci": server_ci[host.host_name],
                       "volume_ci": vol["ci_id"] if vol else None,
                       "os": c.os, "cluster": c.code}
                e.vms.append(rec)
                vms_by_cluster.setdefault(c.code, []).append(rec)
                # Host hosts VM; volume provides storage to VM.
                e.link(server_ci[host.host_name], ci.ci_id, 2)
                if vol:
                    e.link(vol["ci_id"], ci.ci_id, 6)

    # ---- applications move off the cluster and onto VMs ---------------------
    # Previously an application was linked straight to its cluster, which is what
    # made resiliency equal node count. Now it runs on named VMs, and how those
    # VMs are placed is what decides whether it is really redundant.
    # Placement comes from the PACKER, not from host_cluster_code.
    #
    # host_cluster_code is only set on the 40 hand-written applications; the other
    # 1,160 are placed by pack_applications into primary_cluster_idx. Keying on the
    # former put 1,073 of 1,200 applications on no VM at all - they kept only the
    # cluster shortcut edge, so resiliency could not see them and the estate quietly
    # contained more unplaced applications than placed ones.
    by_idx = {c.idx: c.code for c in clusters}
    app_vms: dict[str, list[dict]] = {}
    for a in applications:
        code = a.host_cluster_code or by_idx.get(getattr(a, "primary_cluster_idx", None) or -1, "")
        pool = vms_by_cluster.get(code) or []
        if not pool:
            continue
        want = 2 if a.criticality in ("Critical", "High") else 1
        want = min(want + (1 if a.environment == "Production" else 0), 4, len(pool))
        # One VM per DISTINCT host, not consecutive VMs.
        #
        # VMs are generated host by host, so slicing a contiguous window put every
        # replica of an application on the same hypervisor. The estate then had no
        # genuinely distributed application at all, which made the
        # single-point-of-failure rule fire on everything - and a rule that fires
        # on everything measures nothing, exactly the defect that made the
        # retrieval golden set unfalsifiable. The distributed control has to be
        # really distributed or it controls for nothing.
        by_host: dict[int, list[dict]] = {}
        for vm in pool:
            by_host.setdefault(vm["host_ci"], []).append(vm)
        hosts = sorted(by_host)
        offset = stable_hash(a.code) % len(hosts)
        chosen = [by_host[hosts[(offset + k) % len(hosts)]][k % len(by_host[hosts[(offset + k) % len(hosts)]])]
                  for k in range(min(want, len(hosts)))]
        app_vms[a.code] = chosen
        for vm in chosen:
            e.link(vm["ci_id"], app_ci[a.code], 1)

    # ---- infrastructure servers: the estate that hosts no application -------
    # Workload-tier servers belong in compute, alongside the clusters they serve.
    # Those zones are the estate's original named neighbourhoods rather than the
    # four typed ones added per site, so they are resolved separately.
    compute_zones: dict[str, list] = {}
    for z in e.zones:
        if z["type"] == "Compute":
            compute_zones.setdefault(z["dc"], []).append(z["ci_id"])

    infra_seq = 0
    dc_list = sorted(dc_ci)
    for role, share in INFRA_ROLE_MIX:
        count = max(1, round(INFRA_SERVERS * share))
        ztype = ROLE_ZONE[role]
        for i in range(count):
            dc_name = dc_list[i % len(dc_list)]          # even across every site
            infra_seq += 1
            prefix = dc_name.split("-")[0][:3].lower()
            name = f"{prefix}-{role.lower()}-{(i // len(dc_list)) + 1:02d}"
            if any(s["name"] == name for s in e.infra_servers):
                name = f"{name}-{infra_seq:04d}"
            # Ownership and classification are deliberately incomplete: a real CMDB
            # has gaps and a completeness check that finds none has not been tested.
            if ztype == "Compute":
                pool_z = [z for z in e.zones if z["type"] == "Compute" and z["dc"] == dc_name]
                pick = pool_z[infra_seq % len(pool_z)] if pool_z else None
                zone_for_role = pick["ci_id"] if pick else typed_zone[(dc_name, "Core")]
                nid_for_role = pick["nid"] if pick else typed_zone_nid[(dc_name, "Core")]
            else:
                zone_for_role = typed_zone[(dc_name, ztype)]
                nid_for_role = typed_zone_nid[(dc_name, ztype)]
            owned = rng.randint(1, 20) if rng.random() < 0.72 else None
            ci = e.add_ci("server", name, name, "cmdb_ci_server", environment="Production",
                          owned_by_id=owned,
                          managed_by_id=rng.randint(1, 20) if rng.random() < 0.64 else None,
                          data_classification=rng.choice(["Internal", "Internal", "Confidential", "Restricted"])
                          if rng.random() < 0.81 else None,
                          regulatory_scope="SOX" if role in ("DomainController", "PKI", "IAM") and rng.random() < 0.5 else None,
                          discovery_source="Discovery", first_discovered=old,
                          last_discovered=stamp if rng.random() < 0.88 else old)
            e.infra_servers.append({
                "ci_id": ci.ci_id, "name": name, "role": role,
                "zone_ci": zone_for_role, "zone_nid": nid_for_role,
                "cpu": rng.choice([4, 8, 16, 32]), "memory_gb": rng.choice([16, 32, 64, 128]),
                "storage_gb": rng.choice([200, 500, 1000, 2000]),
                "os": rng.choice(["Linux/RHEL9", "Linux/Ubuntu22", "Windows/2022"]),
            })
            e.link(zone_for_role, ci.ci_id, 5)

    # ---- database instances, on VMs -----------------------------------------
    for i in range(1, DB_INSTANCES + 1):
        if not e.vms:
            break
        vm = e.vms[(i * 13) % len(e.vms)]
        engine, version = _DB_ENGINES[i % len(_DB_ENGINES)]
        name = f"db-{engine.lower()}-{i:05d}"
        ci = e.add_ci("db", name, name, "cmdb_ci_db_instance", environment="Production",
                      discovery_source="Discovery", first_discovered=old, last_discovered=stamp)
        e.databases.append({"ci_id": ci.ci_id, "name": name, "engine": engine,
                            "version": version, "size_gb": rng.choice([50, 200, 800, 2000]),
                            "clustered": 1 if rng.random() < 0.4 else 0})
        e.link(vm["ci_id"], ci.ci_id, 1)

    # ---- business services --------------------------------------------------
    app_codes = [a.code for a in applications]
    for i in range(1, BUSINESS_SERVICES + 1):
        code = f"BS{i:04d}"
        tier = _weighted(rng, _SERVICE_TIERS)
        ci = e.add_ci("service", code, f"Business Service {i}", "cmdb_ci_service",
                      environment="Production", discovery_source="Manual",
                      first_discovered=old, last_discovered=stamp)
        e.services.append({"ci_id": ci.ci_id, "code": code, "name": f"Business Service {i}",
                           "criticality": tier,
                           "rto": rng.choice([15, 30, 60, 240]), "rpo": rng.choice([0, 5, 15, 60])})
        for j in range(rng.randint(2, 6)):
            app = app_codes[(i * 17 + j * 5) % len(app_codes)]
            e.link(ci.ci_id, app_ci[app], 4)

    # ---- load balancers -----------------------------------------------------
    for i in range(1, LOAD_BALANCERS + 1):
        dc_name = dc_list[i % len(dc_list)]
        prefix = dc_name.split("-")[0][:3].lower()
        name = f"{prefix}-lb-{i:04d}"
        ci = e.add_ci("lb", name, name, "cmdb_ci_lb", environment="Production",
                      discovery_source="Discovery", first_discovered=old, last_discovered=stamp)
        e.balancers.append({"ci_id": ci.ci_id, "name": name, "vendor": rng.choice(["F5", "Citrix", "HAProxy"]),
                            "vip": f"10.{i % 250}.{(i * 3) % 250}.{(i * 7) % 250}", "ha": 1})
        e.link(typed_zone[(dc_name, "Network")], ci.ci_id, 5)

    _plant_failure_domains(e, applications, app_vms, app_ci, rng)
    _plant_defects(e, rng, old)
    return e


# =============================================================================
# Planted failure domains
# =============================================================================
def _plant_failure_domains(e, applications, app_vms, app_ci, rng) -> None:
    """Five single-point-of-failure topologies, and a control that is genuinely safe.

    A rule that has never fired is not a rule, and a rule that fires on everything
    measures nothing. Both halves are needed: the defects give the
    single-point-of-failure check something true to find, and WELL_DISTRIBUTED
    gives it something it must stay quiet about.

    The array case is the subtle one. Two volumes look like two failure domains
    until you walk one level further and find them on the same array - so it
    catches a traversal that stops too early, which is a bug that otherwise
    produces a confident, plausible, wrong answer.
    """
    candidates = [a.code for a in applications if a.code in app_vms and len(app_vms[a.code]) >= 2]
    if len(candidates) < 30:
        e.scenarios.update({k: [] for k in (
            "spof_single_volume_applications", "spof_single_host_applications",
            "spof_single_switch_applications", "spof_single_array_applications",
            "spof_single_zone_applications", "well_distributed_applications")})
        return

    picked = candidates[:: max(1, len(candidates) // 40)][:30]
    volume_case, host_case, array_case = picked[0:4], picked[4:8], picked[8:12]

    def drop_edges(child_ci: int, type_id: int) -> None:
        e.edges[:] = [(p, c, t) for (p, c, t) in e.edges if not (c == child_ci and t == type_id)]

    # Every VM onto one volume: spread across hosts, one storage failure domain.
    if e.volumes:
        for code in volume_case:
            vol = e.volumes[stable_hash(code) % len(e.volumes)]
            for vm in app_vms[code]:
                drop_edges(vm["ci_id"], 6)
                vm["volume_ci"] = vol["ci_id"]
                e.link(vol["ci_id"], vm["ci_id"], 6)

    # Every VM onto one physical host.
    for code in host_case:
        target = app_vms[code][0]["host_ci"]
        for vm in app_vms[code]:
            drop_edges(vm["ci_id"], 2)
            vm["host_ci"] = target
            e.link(target, vm["ci_id"], 2)

    # Two distinct volumes that share one array - distinct one level down, single
    # point one level up.
    if len(e.volumes) > 4:
        for code in array_case:
            arr = e.arrays[stable_hash(code) % len(e.arrays)]
            same = [v for v in e.volumes if v["array_ci"] == arr["ci_id"]][:2]
            if len(same) < 2:
                continue
            for i, vm in enumerate(app_vms[code]):
                vol = same[i % 2]
                drop_edges(vm["ci_id"], 6)
                vm["volume_ci"] = vol["ci_id"]
                e.link(vol["ci_id"], vm["ci_id"], 6)

    e.scenarios["spof_single_volume_applications"] = volume_case
    e.scenarios["spof_single_host_applications"] = host_case
    e.scenarios["spof_single_array_applications"] = array_case
    # A deliberate dependency cycle. Two applications that call each other is a
    # real topology, not corrupt data - discovery tools produce them constantly -
    # and it is the one input that proves a traversal's visited-path guard is
    # doing its job. Without it the guard is untested code protecting against a
    # condition the corpus never presents, which is how it stays broken.
    #
    # Deliberately three-deep rather than a mutual pair: A -> B -> C -> A. A guard
    # that only checks the immediately preceding node terminates on a pair and
    # loops forever on a triangle, so a pair would pass a broken implementation.
    cycle = [c for c in picked[18:24]][:3]
    if len(cycle) == 3:
        ring = [app_ci[c] for c in cycle]
        for i, parent in enumerate(ring):
            child = ring[(i + 1) % 3]
            if not any(p == parent and c == child and t == 4 for p, c, t in e.edges):
                e.link(parent, child, 4)
    e.scenarios["dependency_cycle_applications"] = cycle

    e.scenarios["spof_single_switch_applications"] = []
    e.scenarios["spof_single_zone_applications"] = []
    # The control is VERIFIED, not assumed. An application only qualifies if it
    # genuinely sits on two or more distinct hosts AND two or more distinct
    # volumes after placement - checked here rather than hoped for, because a
    # "distributed" fixture that happens to be single-homed silently turns the
    # rule's negative case into a false negative and nothing would report it.
    touched = set(volume_case) | set(host_case) | set(array_case)
    verified = []
    for code in candidates:
        if code in touched or len(verified) >= 6:
            continue
        vms = app_vms[code]
        if len({v["host_ci"] for v in vms}) >= 2 and len({v["volume_ci"] for v in vms}) >= 2:
            verified.append(code)
    e.scenarios["well_distributed_applications"] = verified


def _plant_defects(e, rng, old_stamp) -> None:
    """The mess a real CMDB carries.

    Deliberate, exported through SCENARIOS, and distinct from the incidental gaps
    the estate already has - a completeness check will find both, and only these
    are guaranteed to be there.
    """
    orphans, stale, dupes, unowned, unclassified = [], [], [], [], []

    servers = [c for c in e.cis if c.class_name == "cmdb_ci_server"]
    linked = {c for (_p, c, _t) in e.edges} | {p for (p, _c, _t) in e.edges}

    # Orphans: a CI nothing references. Discovery finds a box, nobody claims it,
    # and it sits in the CMDB forever.
    for i, ci in enumerate(servers):
        if len(orphans) >= 24:
            break
        if ci.ci_id not in linked and i % 3 == 0:
            orphans.append(ci.name)
    if len(orphans) < 24:
        for ci in servers[::97]:
            if len(orphans) >= 24:
                break
            e.edges[:] = [(p, c, t) for (p, c, t) in e.edges if p != ci.ci_id and c != ci.ci_id]
            orphans.append(ci.name)

    # Stale: last seen a year ago. Either it is gone and nobody removed it, or
    # discovery has not reached it - both are findings.
    for ci in servers[3::113]:
        if len(stale) >= 30:
            break
        ci.last_discovered = old_stamp
        stale.append(ci.name)

    # Duplicates: the same host discovered twice under different identities. The
    # classic CMDB defect, and the reason a health check cannot rely on Name being
    # unique.
    for ci in servers[7::151]:
        if len(dupes) >= 12:
            break
        dup = e.add_ci("server-dup", ci.name + "-dup", ci.name, "cmdb_ci_server",
                       environment=ci.environment, discovery_source="Import",
                       first_discovered=old_stamp, last_discovered=old_stamp)
        dupes.append([ci.name, dup.sys_id])

    for ci in servers[11::89]:
        if len(unowned) >= 40:
            break
        ci.owned_by_id = None
        ci.managed_by_id = None
        unowned.append(ci.name)

    for ci in servers[13::79]:
        if len(unclassified) >= 40:
            break
        ci.data_classification = None
        unclassified.append(ci.name)

    e.scenarios.update({
        "orphan_cis": orphans,
        "stale_cis": stale,
        "duplicate_ci_names": [d[0] for d in dupes],
        "unowned_cis": unowned,
        "unclassified_cis": unclassified,
    })


# =============================================================================
# Emission
# =============================================================================
# CiId is written explicitly under IDENTITY_INSERT rather than letting the
# database allocate it. The relationship rows reference ids the generator chose,
# and a generator cannot read back an IDENTITY it has not inserted yet. It also
# keeps the graph reproducible: the same seed puts the same CI at the same id, so
# a golden set can name one and still be talking about the same thing next week.
_BATCH = 500


def _lit(v) -> str:
    """SQL literal. Single quotes doubled - the note text in this corpus contains
    apostrophes and one unescaped quote would truncate an INSERT mid-statement."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _rows(lines: list[str], table: str, columns: list[str], rows: list[tuple]) -> None:
    if not rows:
        return
    cols = ", ".join(columns)
    for i in range(0, len(rows), _BATCH):
        chunk = rows[i:i + _BATCH]
        lines.append(f"INSERT INTO {table} ({cols}) VALUES")
        lines.append(",\n".join("(" + ", ".join(_lit(v) for v in r) + ")" for r in chunk) + ";")
    lines.append("GO")
    lines.append("")


def emit(lines: list[str], e: Estate) -> None:
    """Append the whole CMDB layer to a seed script already holding the base tables.

    Order matters and is not cosmetic: configuration items first because every
    class table and every edge has a foreign key to them; then the class tables;
    then the links back onto the pre-existing tables; then the relationships,
    which reference two CIs and so must come last.
    """
    lines.append("-- =========================================================================")
    lines.append("-- CMDB: configuration items, class tables, and the relationship graph")
    lines.append(f"-- {len(e.cis):,} CIs, {len(e.edges):,} relationships")
    lines.append("-- =========================================================================")

    new_zones = [z for z in e.zones if z.get("is_new")]
    if new_zones:
        lines.append("-- typed zones: storage, core, network and management per site")
        lines.append("SET IDENTITY_INSERT sad.Neighborhood ON;")
        lines.append("GO")
        _rows(lines, "sad.Neighborhood",
              ["NeighborhoodId", "NeighborhoodCode", "NeighborhoodName", "DataCenter",
               "Region", "LifecycleStatus", "ZoneType"],
              [(z["nid"], z["code"], z["name"], z["dc"], z["region"], "Active", z["type"])
               for z in new_zones])
        lines.append("SET IDENTITY_INSERT sad.Neighborhood OFF;")
        lines.append("GO")
        lines.append("")

    lines.append("SET IDENTITY_INSERT sad.ConfigurationItem ON;")
    lines.append("GO")
    _rows(lines, "sad.ConfigurationItem",
          ["CiId", "SysId", "Name", "ClassName", "OperationalStatus", "InstallStatus",
           "Environment", "SupportGroupId", "OwnedById", "ManagedById",
           "DataClassification", "RegulatoryScope", "FirstDiscovered", "LastDiscovered",
           "DiscoverySource"],
          [(c.ci_id, c.sys_id, c.name, c.class_name, c.operational_status, c.install_status,
            c.environment, c.support_group_id, c.owned_by_id, c.managed_by_id,
            c.data_classification, c.regulatory_scope, c.first_discovered,
            c.last_discovered, c.discovery_source) for c in e.cis])
    lines.append("SET IDENTITY_INSERT sad.ConfigurationItem OFF;")
    lines.append("GO")
    lines.append("")

    # ---- class tables -------------------------------------------------------
    _rows(lines, "sad.CiDataCentre", ["CiId", "Code", "City", "Region"],
          [(d["ci_id"], d["code"], d["city"], d["region"]) for d in e.data_centres])

    _rows(lines, "sad.CiStorageArray",
          ["CiId", "ArrayName", "Vendor", "ArrayType", "Protocol", "RawCapacityTb",
           "UsableCapacityTb", "UsedTb", "ControllerCount", "NeighborhoodId"],
          [(a["ci_id"], a["name"], a["vendor"], a["type"], a["protocol"], a["raw_tb"],
            a["usable_tb"], a["used_tb"], a["controllers"], a["zone_nid"]) for a in e.arrays])

    _rows(lines, "sad.CiStorageVolume",
          ["CiId", "VolumeName", "ArrayCiId", "CapacityGb", "UsedGb", "Protocol",
           "ExportPath", "PerformanceTier", "IsReplicated"],
          [(v["ci_id"], v["name"], v["array_ci"], v["capacity_gb"], v["used_gb"],
            v["protocol"], v["export"], v["tier"], v["replicated"]) for v in e.volumes])

    _rows(lines, "sad.CiNetworkDevice",
          ["CiId", "DeviceName", "DeviceRole", "Vendor", "PortCount", "NeighborhoodId"],
          [(s["ci_id"], s["name"], s["role"], s["vendor"], s["ports"], s["zone_nid"])
           for s in e.switches])

    _rows(lines, "sad.CiServer",
          ["CiId", "HostName", "ServerRole", "NeighborhoodId", "CpuCores", "MemoryGb",
           "StorageGb", "OperatingSystem", "IsVirtual", "SocketCount", "CoresPerSocket",
           "Manufacturer", "Model", "RackPosition", "ClusterId", "IsHypervisor"],
          [(s["ci_id"], s["name"], s["role"], s["zone_nid"], s["cpu"], s["memory_gb"],
            s["storage_gb"], s["os"], 0, s.get("sockets"), s.get("cores_per_socket"),
            s.get("vendor"), s.get("model"), s.get("rack"), s.get("cluster_id"),
            s.get("hypervisor", 0)) for s in (e.servers + e.infra_servers)])

    _rows(lines, "sad.CiVmInstance",
          ["CiId", "VmName", "VcpuCount", "MemoryGb", "DiskGb", "PowerState",
           "HostCiId", "VolumeCiId", "OperatingSystem"],
          [(v["ci_id"], v["name"], v["vcpu"], v["memory_gb"], v["disk_gb"], "On",
            v["host_ci"], v["volume_ci"], v["os"]) for v in e.vms])

    _rows(lines, "sad.CiDatabaseInstance",
          ["CiId", "InstanceName", "Engine", "Version", "SizeGb", "IsClustered"],
          [(d["ci_id"], d["name"], d["engine"], d["version"], d["size_gb"], d["clustered"])
           for d in e.databases])

    _rows(lines, "sad.CiBusinessService",
          ["CiId", "ServiceCode", "ServiceName", "Criticality", "RtoMinutes", "RpoMinutes"],
          [(s["ci_id"], s["code"], s["name"], s["criticality"], s["rto"], s["rpo"])
           for s in e.services])

    _rows(lines, "sad.CiLoadBalancer", ["CiId", "DeviceName", "Vendor", "VirtualIp", "IsHaPair"],
          [(b["ci_id"], b["name"], b["vendor"], b["vip"], b["ha"]) for b in e.balancers])

    # ---- link the pre-existing tables to their CIs --------------------------
    # Matched on the natural key rather than on a predicted identity: these tables
    # allocate their own IDENTITY values, and joining on a guessed row id would
    # attach a cluster's configuration item to whichever cluster happened to land
    # on that number.
    lines.append("-- attach existing rows to their configuration items")
    for table, key_col, class_name in (
        ("sad.CmdbApplication", "ApplicationCode", "cmdb_ci_appl"),
        ("sad.InfrastructureCluster", "ClusterCode", "cmdb_ci_cluster"),
        ("sad.ClusterNode", "HostName", "cmdb_ci_cluster_node"),
        ("sad.Neighborhood", "NeighborhoodCode", "cmdb_ci_zone"),
    ):
        lines.append(
            f"UPDATE t SET t.CiId = c.CiId FROM {table} t "
            f"JOIN sad.ConfigurationItem c ON c.Name = t.{key_col} "
            f"AND c.ClassName = '{class_name}' WHERE t.CiId IS NULL;")
    lines.append("GO")
    lines.append("")

    lines.append("-- zone types, and the node-to-server link")
    for z in e.zones:
        lines.append(f"UPDATE sad.Neighborhood SET ZoneType = {_lit(z['type'])} "
                     f"WHERE NeighborhoodCode = {_lit(z['code'])};")
    lines.append("GO")
    lines.append("")
    for s in e.servers:
        lines.append(f"UPDATE sad.ClusterNode SET ServerCiId = {s['ci_id']} "
                     f"WHERE HostName = {_lit(s['node_host'])};")
    lines.append("GO")
    lines.append("")

    # ---- the graph ----------------------------------------------------------
    _rows(lines, "sad.CiRelationship", ["ParentCiId", "ChildCiId", "TypeId"],
          [(p, c, t) for p, c, t in e.edges])
