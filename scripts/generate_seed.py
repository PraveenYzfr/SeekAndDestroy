#!/usr/bin/env python
"""Deterministic seed generator for SeekAndDestroy.

Regenerating ``database/seed.sql`` from this script is byte-identical: every
random draw goes through ``random.Random(SEED)`` and every date is computed
from the fixed ``ANCHOR_DATE`` below. Never use ``datetime.now()`` /
``date.today()`` anywhere in this file.

Usage:
    .venv\\Scripts\\python.exe scripts\\generate_seed.py

Writes ``database/seed.sql``. Apply with:
    sqlcmd -S LAPTOP-R6U8H616 -d PraveenDB -E -C -i database\\seed.sql
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

SEED = 20240101
ANCHOR_DATE = date(2026, 8, 4)  # "today" for the seed data - fixed, never derived from the clock
HISTORY_DAYS = 180
#: Rows per multi-row INSERT. Was 1000, which SQL Server Express could not
#: ingest once the corpus reached 89,912 comment rows carrying NVARCHAR(MAX)
#: bodies: the seed died partway with "Msg 701 - insufficient system memory in
#: resource pool 'internal'", having loaded applications and clusters but no
#: incidents. Express caps its buffer pool at ~1.4 GB regardless of
#: MSSQL_MEMORY_LIMIT_MB, and on the VM that pool is shared with RLogistics and
#: AutoCoder. 200 keeps each statement small enough to parse and commit inside
#: it, at the cost of a slightly larger file and more round trips - both cheap
#: next to a seed that stops halfway.
BATCH_SIZE = 200

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "database" / "seed.sql"

rng = random.Random(SEED)

# =============================================================================
# SQL emission helpers
# =============================================================================


def sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_val(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, datetime):
        return sql_str(value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
    if isinstance(value, date):
        return sql_str(value.strftime("%Y-%m-%d"))
    return sql_str(str(value))


def emit_inserts(lines: list[str], table: str, columns: list[str], rows: list[tuple]) -> None:
    if not rows:
        return
    col_list = ", ".join(columns)
    for start in range(0, len(rows), BATCH_SIZE):
        chunk = rows[start : start + BATCH_SIZE]
        values_sql = ",\n".join("  (" + ", ".join(sql_val(v) for v in r) + ")" for r in chunk)
        lines.append(f"INSERT INTO {table} ({col_list}) VALUES\n{values_sql};")
    lines.append("GO")


def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def r2(value: float) -> float:
    return round(value, 2)


# =============================================================================
# 1. Employees (20) and support groups (8)
# =============================================================================

# E1001 is the platform owner - the account a human actually signs in as, so
# it carries a real name rather than a generated one. Every other row stays
# fictional.
#
# The email deliberately stays on the @seekanddestroy.example domain even
# though the name is real: this file generates database/seed.sql, which is
# committed, so a real address here would be published in the repository.
# Override it locally instead:
#   SAD_OWNER_EMAIL=you@example.com .venv/Scripts/python.exe scripts/generate_seed.py
# or just UPDATE the row in your own database.
OWNER_EMAIL = os.environ.get("SAD_OWNER_EMAIL", "praveen.yadav@seekanddestroy.example")

EMPLOYEE_NAMES = [
    "Praveen Yadav", "Rohan Mehta", "Priya Nair", "Karan Verma", "Sneha Iyer",
    "Arjun Rao", "Divya Menon", "Vikram Singh", "Neha Kapoor", "Sanjay Gupta",
    "Ananya Pillai", "Rahul Desai", "Kavya Reddy", "Amit Joshi", "Pooja Chawla",
    "Nikhil Bhatt", "Ritu Malhotra", "Suresh Kumar", "Lakshmi Krishnan", "Varun Chopra",
]

EMPLOYEES = []
for i, name in enumerate(EMPLOYEE_NAMES, start=1):
    first, last = name.split(" ", 1)
    email = OWNER_EMAIL if i == 1 else f"{first.lower()}.{last.lower().replace(' ', '')}@seekanddestroy.example"
    is_active = 0 if i in (19, 20) else 1
    EMPLOYEES.append(
        {
            "id": i,
            "number": f"E{1000 + i}",
            "name": name,
            "email": email,
            "active": is_active,
        }
    )

#: 15 lines of business (generic large-bank LOB naming, not any real employer's
#: actual org chart). Index order matches the sg_idx values used by APPLICATIONS
#: below (1-based).
SUPPORT_GROUP_NAMES = [
    "SG-EFT",     # 1  Enterprise Functions Technology
    "SG-CIB",     # 2  Corporate & Investment Banking
    "SG-WHS",     # 3  Wholesale Banking
    "SG-CCB",     # 4  Consumer & Community Banking
    "SG-WM",      # 5  Wealth & Investment Management
    "SG-PAY",     # 6  Payments
    "SG-CARD",    # 7  Card Services
    "SG-MTG",     # 8  Home Lending
    "SG-RISK",    # 9  Enterprise Risk Management
    "SG-COMP",    # 10 Compliance & Regulatory Affairs
    "SG-CTECH",   # 11 Corporate Technology
    "SG-DATA",    # 12 Data & Analytics
    "SG-SEC",     # 13 Cybersecurity
    "SG-HRT",     # 14 HR Technology
    "SG-OPS",     # 15 Enterprise Operations
]

SUPPORT_GROUPS = []
for i, name in enumerate(SUPPORT_GROUP_NAMES, start=1):
    slug = name.lower().replace("sg-", "")
    SUPPORT_GROUPS.append(
        {
            "id": i,
            "name": name,
            "email": f"{slug}@seekanddestroy.example",
            "active": 1,
        }
    )

# =============================================================================
# 2. Neighborhoods - a mid-tier grouping within a data center (shared power/
#    network/cooling domain, aka a "pod") that infra engineers pick between
#    DataCenter and Cluster when browsing by location. Added after the
#    platform's initial build in response to infra-engineer feedback; the
#    hierarchy stops here by design (see docs/business-rules.md).
# =============================================================================

# 8 generic US cities (not any real employer's actual data-center footprint -
# see project history for why real ones aren't used here). Scale target
# (infra-engineer feedback): 8 neighborhoods/pods per city x 4 clusters per
# neighborhood = 32 clusters/city x 8 cities = 256 clusters total. VM-level
# tracking is explicitly out of scope - see docs/business-rules.md.
CITIES = [
    # (city_code, data_center, region)
    ("nyc", "New York-DC1", "US-Northeast"),
    ("dal", "Dallas-DC1", "US-South"),
    ("atl", "Atlanta-DC1", "US-Southeast"),
    ("den", "Denver-DC1", "US-West"),
    ("clt", "Charlotte-DC1", "US-Southeast"),
    ("phx", "Phoenix-DC1", "US-West"),
    ("msp", "Minneapolis-DC1", "US-Midwest"),
    ("cmh", "Columbus-DC1", "US-Midwest"),
]
NEIGHBORHOODS_PER_CITY = 8
CLUSTERS_PER_NEIGHBORHOOD = 4
TOTAL_CLUSTERS_TARGET = len(CITIES) * NEIGHBORHOODS_PER_CITY * CLUSTERS_PER_NEIGHBORHOOD  # 256

NEIGHBORHOODS = [
    (f"NH-{city.upper()}-{n:02d}", f"{dc.split('-')[0]} Neighborhood {n}", dc, region)
    for city, dc, region in CITIES
    for n in range(1, NEIGHBORHOODS_PER_CITY + 1)
]
NEIGHBORHOOD_INDEX = {code: i for i, (code, *_rest) in enumerate(NEIGHBORHOODS, start=1)}

#: Which neighborhood each *hand-crafted* cluster (by code) sits in - always
#: neighborhood 01 (or 02 for a city's second hand-crafted cluster). The 241
#: procedurally-generated clusters get their neighborhood assigned where
#: they're built, further down.
CLUSTER_NEIGHBORHOOD = {
    "nyc-03": "NH-NYC-01", "nyc-05": "NH-NYC-02",
    "dal-03": "NH-DAL-01", "dal-07": "NH-DAL-02",
    "atl-03": "NH-ATL-01", "atl-05": "NH-ATL-02",
    "den-03": "NH-DEN-01", "den-07": "NH-DEN-02",
    "clt-03": "NH-CLT-01", "clt-13": "NH-CLT-02",
    "phx-03": "NH-PHX-01", "phx-05": "NH-PHX-02",
    "msp-03": "NH-MSP-01", "msp-09": "NH-MSP-02",
    "cmh-03": "NH-CMH-01",
}

# =============================================================================
# 3. Infrastructure clusters (15) - hand-authored so every engineered scenario
#    from the specification lands on a specific, named cluster.
# =============================================================================


@dataclass
class ClusterDef:
    idx: int
    code: str
    name: str
    ctype: str
    platform: str
    os: str
    environment: str
    datacenter: str
    region: str
    lifecycle: str
    node_count: int
    total_cpu: float
    total_mem_gb: float
    total_storage_gb: float
    reserved_cpu_pct: float
    reserved_mem_pct: float
    monthly_cost: float
    availability_tier: str
    compliance: str
    tags: list[str] = field(default_factory=list)
    # Utilization profile: (cpu_start,cpu_end,mem_start,mem_end,storage_start,storage_end)
    # values are percentages at day0 (180 days ago) and day179 (== ANCHOR_DATE).
    util_profile: tuple = (30, 30, 35, 35, 25, 25)
    neighborhood_code: str = ""  # set below, after CLUSTERS is built


CLUSTERS = [
    ClusterDef(
        1, "nyc-03", "Prod Kubernetes New York A", "Kubernetes", "Kubernetes", "Linux/RHEL9",
        "Production", "New York-DC1", "US-Northeast", "Active", 6, 64, 256, 10000, 10, 10, 38000,
        "Tier-1", "Restricted", ["OVERPROVISIONED"], (20, 18, 24, 22, 16, 15),
    ),
    ClusterDef(
        2, "dal-03", "Prod VMware Dallas A", "VMware", "VMware", "Linux/RHEL9",
        "Production", "Dallas-DC1", "US-South", "Active", 8, 96, 384, 20000, 10, 10, 52000,
        "Tier-1", "Confidential", ["OVERPROVISIONED"], (22, 20, 27, 25, 20, 18),
    ),
    ClusterDef(
        3, "atl-03", "Prod Kubernetes Atlanta A", "Kubernetes", "Kubernetes", "Linux/RHEL9",
        "Production", "Atlanta-DC1", "US-Southeast", "Active", 11, 240, 1024, 40000, 10, 10, 96000,
        "Tier-1", "Restricted", ["SUITABLE_NEW"], (44, 48, 57, 61, 32, 35),
    ),
    ClusterDef(
        4, "den-03", "Prod Kubernetes Denver A", "Kubernetes", "Kubernetes", "Linux/Ubuntu22",
        "Production", "Denver-DC1", "US-West", "Active", 10, 200, 896, 35000, 10, 10, 84000,
        "Tier-1", "Confidential", ["SUITABLE_NEW"], (36, 40, 46, 50, 27, 30),
    ),
    ClusterDef(
        5, "clt-03", "Prod VMware Charlotte A", "VMware", "VMware", "Linux/RHEL9",
        "Production", "Charlotte-DC1", "US-Southeast", "Active", 6, 120, 512, 25000, 8, 8, 58000,
        "Tier-2", "Internal", ["NEAR_CPU", "FORECAST"], (50, 68, 48, 55, 35, 40),
    ),
    ClusterDef(
        6, "clt-13", "Prod Kubernetes Charlotte B", "Kubernetes", "Kubernetes", "Linux/RHEL9",
        "Production", "Charlotte-DC1", "US-Southeast", "Active", 5, 100, 400, 20000, 8, 8, 46000,
        "Tier-2", "Internal", ["NEAR_MEM", "FORECAST", "COMPLIANCE_MISMATCH"], (46, 52, 55, 72, 30, 35),
    ),
    ClusterDef(
        7, "phx-03", "Prod OpenShift Phoenix A", "OpenShift", "OpenShift", "Linux/RHEL9",
        "Production", "Phoenix-DC1", "US-West", "Active", 1, 80, 360, 18000, 5, 5, 41000,
        "Tier-2", "Confidential", ["NEAR_MEM", "LOW_RESILIENCY"], (40, 45, 70, 74, 30, 33),
    ),
    ClusterDef(
        8, "msp-03", "Prod BareMetal Minneapolis A", "BareMetal", "BareMetal", "Linux/RHEL9",
        "Production", "Minneapolis-DC1", "US-Midwest", "Active", 4, 48, 192, 12000, 0, 0, 45000,
        "Tier-3", "Internal", ["HIGH_COST_LOW_UTIL", "COMPLIANCE_MISMATCH"], (10, 12, 13, 15, 8, 10),
    ),
    ClusterDef(
        9, "cmh-03", "Prod Hyper-V Columbus A", "Hyper-V", "Hyper-V", "Windows/2022",
        "Production", "Columbus-DC1", "US-Midwest", "Active", 2, 32, 128, 8000, 0, 0, 39000,
        "Tier-1", "Internal", ["HIGH_COST_LOW_UTIL", "LOW_RESILIENCY", "FORECAST"], (12, 14, 15, 17, 60, 78),
    ),
    ClusterDef(
        10, "nyc-05", "Staging Kubernetes New York", "Kubernetes", "Kubernetes", "Linux/Ubuntu22",
        "Staging", "New York-DC1", "US-Northeast", "Active", 6, 60, 240, 15000, 5, 5, 18000,
        "Staging", "Internal", ["SUITABLE_NEW"], (26, 30, 30, 35, 18, 20),
    ),
    ClusterDef(
        11, "atl-05", "Staging VMware Atlanta", "VMware", "VMware", "Linux/Ubuntu22",
        "Staging", "Atlanta-DC1", "US-Southeast", "Active", 5, 40, 160, 10000, 5, 5, 12000,
        "Staging", "Internal", [], (40, 45, 46, 50, 35, 38),
    ),
    ClusterDef(
        12, "phx-05", "Staging Kubernetes Phoenix", "Kubernetes", "Kubernetes", "Linux/Ubuntu22",
        "Staging", "Phoenix-DC1", "US-West", "Active", 4, 45, 180, 11000, 5, 5, 14000,
        "Staging", "Confidential", ["NEAR_CPU"], (55, 70, 40, 44, 28, 30),
    ),
    ClusterDef(
        13, "dal-07", "Test Kubernetes Dallas", "Kubernetes", "Kubernetes", "Linux/Ubuntu22",
        "Test", "Dallas-DC1", "US-South", "Active", 2, 24, 96, 6000, 0, 0, 7000,
        "Test", "Internal", [], (30, 33, 32, 35, 20, 22),
    ),
    ClusterDef(
        14, "den-07", "Test VMware Denver", "VMware", "VMware", "Linux/Ubuntu22",
        "Test", "Denver-DC1", "US-West", "Active", 3, 30, 120, 7000, 0, 0, 9000,
        "Test", "Internal", ["OVERPROVISIONED"], (14, 12, 16, 14, 10, 9),
    ),
    ClusterDef(
        15, "msp-09", "Dev BareMetal Minneapolis", "BareMetal", "BareMetal", "Linux/Ubuntu22",
        "Development", "Minneapolis-DC1", "US-Midwest", "Active", 2, 16, 64, 4000, 0, 0, 4000,
        "Test", "Internal", [], (25, 28, 28, 30, 15, 18),
    ),
]

assert len(CLUSTERS) == 15, "the 15 hand-crafted, scenario-carrying clusters"

# =============================================================================
# 3b. Procedurally-generated clusters (241) filling out the rest of the
#     256-cluster estate. These carry no engineered scenario tags - they're
#     realistic "generic" capacity: varied platform/environment/size, mild
#     utilization jitter. Every hand-crafted cluster above keeps its exact
#     numbers; this section only ADDS clusters, never touches them.
# =============================================================================

PLATFORM_WEIGHTS = [("Kubernetes", 50), ("VMware", 20), ("OpenShift", 15), ("BareMetal", 10), ("Hyper-V", 5)]
ENV_WEIGHTS = [("Production", 55), ("Staging", 15), ("Test", 15), ("Development", 15)]
CLASSIFICATION_WEIGHTS = [("Internal", 40), ("Confidential", 35), ("Restricted", 15), ("Public", 10)]
LIFECYCLE_WEIGHTS = [("Active", 92), ("Deprecated", 8)]

SIZE_RANGES = {
    "small":  {"nodes": (2, 4),   "cpu": (8, 24),   "mem": (32, 96),   "storage": (500, 2000),   "cost": (3000, 8000)},
    "medium": {"nodes": (5, 12),  "cpu": (24, 80),  "mem": (96, 320),  "storage": (2000, 8000),  "cost": (8000, 25000)},
    "large":  {"nodes": (13, 25), "cpu": (80, 200), "mem": (320, 800), "storage": (8000, 25000), "cost": (25000, 70000)},
}


def _weighted_choice(weights: list[tuple[str, float]]) -> str:
    total = sum(w for _, w in weights)
    pick = rng.uniform(0, total)
    upto = 0.0
    for item, w in weights:
        upto += w
        if pick <= upto:
            return item
    return weights[-1][0]


def _os_for_platform(platform: str) -> str:
    if platform == "Hyper-V":
        return "Windows/2022"
    if platform == "VMware":
        return rng.choice(["Linux/RHEL9", "Linux/RHEL9", "Windows/2022"])
    return rng.choice(["Linux/RHEL9", "Linux/Ubuntu22"])


def _size_tier() -> str:
    roll = rng.random()
    if roll < 0.40:
        return "small"
    if roll < 0.80:
        return "medium"
    return "large"


def _availability_tier_for_env(environment: str) -> str:
    if environment == "Production":
        return _weighted_choice([("Tier-1", 35), ("Tier-2", 50), ("Tier-3", 15)])
    if environment == "Staging":
        return _weighted_choice([("Tier-2", 60), ("Tier-3", 40)])
    return "Tier-3"


def _reserved_pct_for_platform(platform: str) -> float:
    if platform == "BareMetal":
        return 0.0
    if platform in ("Kubernetes", "OpenShift"):
        return float(rng.randint(5, 10))
    return float(rng.randint(8, 15))


_procedural_idx = 15
PROCEDURAL_CLUSTERS: list[ClusterDef] = []
for city_code, dc, region in CITIES:
    hand_crafted_here = [c for c in CLUSTERS if c.datacenter == dc]
    for n in range(1, NEIGHBORHOODS_PER_CITY + 1):
        nh_code = f"NH-{city_code.upper()}-{n:02d}"
        existing_in_nh = sum(1 for c in hand_crafted_here if CLUSTER_NEIGHBORHOOD.get(c.code) == nh_code)
        needed = CLUSTERS_PER_NEIGHBORHOOD - existing_in_nh
        for _ in range(needed):
            _procedural_idx += 1
            seq = _procedural_idx - 15
            code = f"{city_code}-p{seq:03d}"
            platform = _weighted_choice(PLATFORM_WEIGHTS)
            os_val = _os_for_platform(platform)
            environment = _weighted_choice(ENV_WEIGHTS)
            ranges = SIZE_RANGES[_size_tier()]
            node_count = rng.randint(*ranges["nodes"])
            total_cpu = float(rng.randint(*ranges["cpu"]))
            total_mem = float(rng.randint(*ranges["mem"]))
            total_storage = float(rng.randint(*ranges["storage"]))
            monthly_cost = float(rng.randint(*ranges["cost"]))
            cpu0 = rng.uniform(15, 55)
            cpu1 = clamp(cpu0 + rng.uniform(-8, 8), 5, 90)
            mem0 = rng.uniform(20, 60)
            mem1 = clamp(mem0 + rng.uniform(-8, 8), 5, 90)
            sto0 = rng.uniform(10, 50)
            sto1 = clamp(sto0 + rng.uniform(-5, 5), 5, 90)
            name = f"{environment} {platform} {dc.split('-')[0]} {seq:03d}"
            cluster = ClusterDef(
                _procedural_idx, code, name, platform, platform, os_val, environment, dc, region,
                _weighted_choice(LIFECYCLE_WEIGHTS), node_count, total_cpu, total_mem, total_storage,
                _reserved_pct_for_platform(platform), _reserved_pct_for_platform(platform), monthly_cost,
                _availability_tier_for_env(environment), _weighted_choice(CLASSIFICATION_WEIGHTS), [],
                (r2(cpu0), r2(cpu1), r2(mem0), r2(mem1), r2(sto0), r2(sto1)),
            )
            cluster.neighborhood_code = nh_code
            PROCEDURAL_CLUSTERS.append(cluster)

CLUSTERS.extend(PROCEDURAL_CLUSTERS)
assert len(CLUSTERS) == TOTAL_CLUSTERS_TARGET, len(CLUSTERS)

_total_nodes = sum(c.node_count for c in CLUSTERS)
assert 2000 <= _total_nodes <= 5000, (
    f"total node count {_total_nodes} is outside the intended ~2-5k range - individual node rows "
    f"are informational (resiliency/right-sizing math reads ClusterUtilization, not per-node data), "
    f"so this is deliberately far short of a literal one-row-per-physical-server inventory."
)

CLUSTER_BY_CODE = {c.code: c for c in CLUSTERS}

NEIGHBORHOOD_BY_CODE = {code: (code, name, dc, region) for code, name, dc, region in NEIGHBORHOODS}
for c in CLUSTERS:
    if c.code in CLUSTER_NEIGHBORHOOD:
        c.neighborhood_code = CLUSTER_NEIGHBORHOOD[c.code]
    assert c.neighborhood_code, f"{c.code} has no neighborhood assigned"
    _, _, nh_dc, _ = NEIGHBORHOOD_BY_CODE[c.neighborhood_code]
    assert nh_dc == c.datacenter, f"{c.code}: neighborhood {c.neighborhood_code} is in {nh_dc}, cluster is in {c.datacenter}"

# Availability tiers used on non-production clusters mirror the app-side scale so
# RULE-004 comparisons stay well-defined even for Staging/Test/Development infra.
NONPROD_TIER_ALIAS = {"Staging": "Tier-2", "Test": "Tier-3", "Development": "Tier-3"}
for c in CLUSTERS:
    if c.availability_tier in NONPROD_TIER_ALIAS.values():
        continue
    if c.availability_tier in ("Staging", "Test"):
        c.availability_tier = NONPROD_TIER_ALIAS[c.environment]

# =============================================================================
# 3. Cluster nodes - procedurally generated from each cluster's NodeCount.
# =============================================================================


@dataclass
class NodeDef:
    idx: int
    cluster: ClusterDef
    seq: int
    host_name: str
    ip_address: str
    cpu_cores: float
    memory_gb: float
    storage_gb: float
    lifecycle: str
    last_seen: datetime
    monthly_cost: float


NODES: list[NodeDef] = []
_node_id = 0
for c in CLUSTERS:
    per_node_cpu = r2(c.total_cpu / c.node_count)
    per_node_mem = r2(c.total_mem_gb / c.node_count)
    per_node_storage = r2(c.total_storage_gb / c.node_count)
    per_node_cost = r2(c.monthly_cost / c.node_count)
    # Two octets derived from cluster idx keep every value in the valid 0-255
    # range even at 256 clusters (idx alone would overflow a single octet
    # past ~245 clusters).
    ip_base = 1000 + c.idx
    ip_octet2 = (ip_base // 256) % 256
    ip_octet3 = ip_base % 256
    for seq in range(1, c.node_count + 1):
        _node_id += 1
        host = f"{c.code}-NODE-{seq:02d}"
        ip = f"10.{ip_octet2}.{ip_octet3}.{seq}"
        last_seen = datetime.combine(ANCHOR_DATE, datetime.min.time()) - timedelta(
            minutes=rng.randint(1, 240)
        )
        NODES.append(
            NodeDef(
                _node_id, c, seq, host, ip, per_node_cpu, per_node_mem, per_node_storage,
                "Active", last_seen, per_node_cost,
            )
        )

assert len(NODES) == _total_nodes, (len(NODES), _total_nodes)
assert 2000 <= len(NODES) <= 5000, len(NODES)

# =============================================================================
# 4. Applications (40) - hand-authored so every engineered scenario is explicit.
#    Field order: code, name, criticality, environment, platform, os, cpu_req,
#    mem_req_gb, storage_req_gb, growth_pct, avail_tier, classification,
#    preferred_location, owner_idx, sg_idx, host_cluster_code, alloc_factor,
#    hosting_status, tags
# =============================================================================


@dataclass
class AppDef:
    idx: int
    code: str
    name: str
    criticality: str
    environment: str
    platform: str
    os: str
    cpu_req: float
    mem_req_gb: float
    storage_req_gb: float
    growth_pct: float
    avail_tier: str
    classification: str
    preferred_location: str | None
    owner_idx: int
    sg_idx: int
    host_cluster_code: str
    alloc_factor: float
    hosting_status: str
    hosted_since: date
    tags: list[str] = field(default_factory=list)


_A = []
_A.append(('APP-PAYMENTS', 'Payments Processing Engine', 'Critical', 'Production', 'Kubernetes', 'Linux/RHEL9', 32, 128, 4000, 40, 'Tier-1', 'Restricted', 'Atlanta-DC1', 1, 6, 'atl-03', 1.0, ['EXPANSION']))
_A.append(('APP-CRM', 'Customer Relationship Mgmt', 'High', 'Production', 'Kubernetes', 'Linux/RHEL9', 16, 64, 2000, 15, 'Tier-1', 'Confidential', 'Atlanta-DC1', 2, 4, 'den-03', 1.0, ['STRONG_ALT']))
_A.append(('APP-LEDGER', 'General Ledger', 'Critical', 'Production', 'Kubernetes', 'Linux/RHEL9', 24, 96, 3000, 35, 'Tier-1', 'Restricted', 'Atlanta-DC1', 1, 1, 'atl-03', 1.0, ['EXPANSION']))
_A.append(('APP-CARDS', 'Card Issuance & Switching', 'Critical', 'Production', 'Kubernetes', 'Linux/RHEL9', 20, 80, 2500, 20, 'Tier-1', 'Restricted', 'New York-DC1', 1, 7, 'atl-03', 1.0, []))
_A.append(('APP-FRAUD', 'Fraud Detection', 'Critical', 'Production', 'Kubernetes', 'Linux/RHEL9', 18, 72, 1500, 25, 'Tier-1', 'Confidential', 'New York-DC1', 3, 9, 'den-03', 1.0, []))
_A.append(('APP-KYC', 'KYC Verification', 'High', 'Production', 'Kubernetes', 'Linux/RHEL9', 10, 40, 1000, 12, 'Tier-1', 'Confidential', 'New York-DC1', 3, 10, 'den-03', 1.0, []))
_A.append(('APP-LOANS', 'Loan Origination', 'High', 'Production', 'Kubernetes', 'Linux/RHEL9', 14, 56, 1800, 18, 'Tier-1', 'Confidential', 'Atlanta-DC1', 4, 8, 'atl-03', 1.0, []))
_A.append(('APP-TREASURY', 'Treasury Management', 'Critical', 'Production', 'VMware', 'Linux/RHEL9', 12, 48, 1200, 10, 'Tier-1', 'Confidential', 'New York-DC1', 5, 1, 'dal-03', 1.0, []))
_A.append(('APP-RECON', 'Transaction Reconciliation', 'High', 'Production', 'Kubernetes', 'Linux/RHEL9', 10, 40, 2200, 8, 'Tier-2', 'Confidential', 'Charlotte-DC1', 6, 1, 'den-03', 1.0, []))
_A.append(('APP-SETTLEMENT', 'Settlement Engine', 'Critical', 'Production', 'Kubernetes', 'Linux/RHEL9', 16, 64, 2000, 15, 'Tier-1', 'Confidential', 'Atlanta-DC1', 1, 6, 'den-03', 1.0, []))
_A.append(('APP-NOTIFICATIONS', 'Customer Notifications', 'Medium', 'Production', 'Kubernetes', 'Linux/Ubuntu22', 6, 24, 500, 10, 'Tier-2', 'Internal', 'New York-DC1', 7, 11, 'nyc-03', 1.0, ['CONSOLIDATE']))
_A.append(('APP-DOCSTORE', 'Document Store', 'Medium', 'Production', 'Kubernetes', 'Linux/Ubuntu22', 4, 16, 3000, 10, 'Tier-2', 'Internal', 'New York-DC1', 7, 11, 'nyc-03', 1.0, ['CONSOLIDATE']))
_A.append(('APP-REPORTING', 'Regulatory Reporting', 'High', 'Production', 'Kubernetes', 'Linux/RHEL9', 12, 48, 2500, 12, 'Tier-2', 'Confidential', 'New York-DC1', 8, 12, 'den-03', 1.0, []))
_A.append(('APP-ANALYTICS', 'Business Analytics Platform', 'Medium', 'Production', 'Kubernetes', 'Linux/RHEL9', 20, 80, 5000, 22, 'Tier-2', 'Internal', 'Charlotte-DC1', 9, 12, 'clt-13', 1.0, ['STRONG_ALT']))
_A.append(('APP-AUDIT', 'Audit Trail Service', 'High', 'Production', 'Kubernetes', 'Linux/RHEL9', 8, 32, 4000, 8, 'Tier-2', 'Confidential', 'New York-DC1', 10, 10, 'den-03', 1.0, []))
_A.append(('APP-BILLING', 'Billing & Invoicing Core', 'High', 'Production', 'BareMetal', 'Linux/RHEL9', 6, 24, 1500, 6, 'Tier-2', 'Confidential', 'Phoenix-DC1', 11, 6, 'msp-03', 1.0, ['COMPLIANCE_MISMATCH_HOST']))
_A.append(('APP-INVOICING', 'Invoicing Service', 'Medium', 'Production', 'Kubernetes', 'Linux/Ubuntu22', 6, 24, 800, 8, 'Tier-2', 'Internal', 'Phoenix-DC1', 11, 6, 'phx-03', 1.0, []))
_A.append(('APP-PAYROLL', 'Payroll Processing', 'High', 'Production', 'Kubernetes', 'Linux/RHEL9', 8, 32, 900, 5, 'Tier-2', 'Confidential', 'Charlotte-DC1', 12, 6, 'clt-13', 1.0, ['COMPLIANCE_MISMATCH_HOST']))
_A.append(('APP-HRPORTAL', 'HR Self-Service Portal', 'Medium', 'Production', 'Kubernetes', 'Linux/Ubuntu22', 4, 16, 500, 6, 'Tier-2', 'Internal', 'Charlotte-DC1', 12, 14, 'clt-13', 1.0, []))
_A.append(('APP-IDENTITY', 'Identity Provider', 'Critical', 'Production', 'Kubernetes', 'Linux/RHEL9', 10, 40, 500, 15, 'Tier-1', 'Confidential', 'New York-DC1', 13, 13, 'atl-03', 1.0, []))
_A.append(('APP-SSO', 'Single Sign-On Gateway', 'Critical', 'Production', 'Kubernetes', 'Linux/RHEL9', 6, 24, 300, 10, 'Tier-1', 'Confidential', 'New York-DC1', 13, 13, 'den-03', 1.0, []))
_A.append(('APP-APIGATEWAY', 'API Gateway', 'Critical', 'Production', 'Kubernetes', 'Linux/RHEL9', 12, 48, 400, 20, 'Tier-1', 'Internal', 'Atlanta-DC1', 14, 11, 'atl-03', 1.0, []))
_A.append(('APP-MOBILEBFF', 'Mobile Backend-for-Frontend', 'High', 'Production', 'Kubernetes', 'Linux/RHEL9', 10, 40, 400, 25, 'Tier-1', 'Internal', 'Atlanta-DC1', 14, 11, 'den-03', 1.0, []))
_A.append(('APP-WEBPORTAL', 'Customer Web Portal', 'High', 'Production', 'Kubernetes', 'Linux/RHEL9', 12, 48, 600, 18, 'Tier-1', 'Internal', 'Atlanta-DC1', 15, 11, 'atl-03', 1.0, []))
_A.append(('APP-CHATBOT', 'Support Chatbot', 'Low', 'Production', 'BareMetal', 'Linux/Ubuntu22', 3, 12, 200, 5, 'Tier-3', 'Internal', 'Phoenix-DC1', 16, 11, 'msp-03', 1.0, ['POOR_FIT']))
_A.append(('APP-SEARCH', 'Enterprise Search', 'Medium', 'Production', 'Kubernetes', 'Linux/Ubuntu22', 5, 20, 1500, 8, 'Tier-2', 'Internal', 'New York-DC1', 17, 11, 'nyc-03', 1.0, ['CONSOLIDATE']))
_A.append(('APP-CACHEADMIN', 'Cache Cluster Admin', 'Low', 'Production', 'Kubernetes', 'Linux/Ubuntu22', 2, 8, 100, 4, 'Tier-3', 'Internal', 'New York-DC1', 17, 11, 'nyc-03', 1.0, ['CONSOLIDATE']))
_A.append(('APP-BATCHSCHED', 'Batch Job Scheduler', 'High', 'Production', 'Hyper-V', 'Windows/2022', 6, 24, 300, 8, 'Tier-2', 'Internal', 'Columbus-DC1', 18, 15, 'cmh-03', 1.0, ['POOR_FIT']))
_A.append(('APP-ETL', 'ETL Orchestration', 'Medium', 'Production', 'Kubernetes', 'Linux/RHEL9', 14, 56, 3000, 15, 'Tier-2', 'Internal', 'Charlotte-DC1', 19, 12, 'clt-13', 1.0, ['STRONG_ALT']))
_A.append(('APP-DATALAKE', 'Data Lake Ingestion', 'Medium', 'Production', 'Kubernetes', 'Linux/RHEL9', 18, 72, 8000, 20, 'Tier-2', 'Internal', 'Charlotte-DC1', 19, 12, 'clt-13', 1.0, []))
_A.append(('APP-MLSCORING', 'ML Risk Scoring', 'High', 'Production', 'Kubernetes', 'Linux/RHEL9', 20, 80, 1000, 45, 'Tier-2', 'Confidential', 'Charlotte-DC1', 20, 12, 'atl-03', 1.0, ['EXPANSION']))
_A.append(('APP-RISKENGINE', 'Risk Calculation Engine', 'High', 'Production', 'Kubernetes', 'Linux/RHEL9', 16, 64, 1200, 18, 'Tier-1', 'Confidential', 'Atlanta-DC1', 4, 9, 'den-03', 1.0, ['STRONG_ALT']))
_A.append(('APP-COMPLIANCE', 'Compliance Monitoring', 'High', 'Production', 'Kubernetes', 'Linux/RHEL9', 8, 32, 1500, 10, 'Tier-2', 'Confidential', 'New York-DC1', 6, 10, 'den-03', 1.0, []))
_A.append(('APP-DOCSIGN', 'Digital Document Signing', 'Medium', 'Production', 'VMware', 'Linux/RHEL9', 6, 24, 700, 8, 'Tier-2', 'Confidential', 'New York-DC1', 6, 10, 'dal-03', 1.0, []))
_A.append(('APP-ONBOARDING', 'Customer Onboarding', 'High', 'Production', 'Kubernetes', 'Linux/RHEL9', 8, 32, 600, 20, 'Tier-2', 'Confidential', 'Atlanta-DC1', 8, 10, 'den-03', 1.0, []))
_A.append(('APP-SUPPORTDESK', 'IT Support Desk', 'Low', 'Production', 'Hyper-V', 'Windows/2022', 4, 16, 400, 5, 'Tier-3', 'Internal', 'Columbus-DC1', 18, 15, 'cmh-03', 1.0, ['POOR_FIT']))
_A.append(('APP-INVENTORY', 'Branch Inventory Mgmt', 'Medium', 'Production', 'OpenShift', 'Linux/RHEL9', 4, 16, 300, 6, 'Tier-2', 'Internal', 'Phoenix-DC1', 10, 15, 'phx-03', 1.0, ['POOR_FIT']))
_A.append(('APP-PROCUREMENT', 'Procurement Portal', 'Low', 'Staging', 'Kubernetes', 'Linux/Ubuntu22', 3, 12, 300, 5, 'Tier-3', 'Internal', 'New York-DC1', 9, 15, 'nyc-05', 1.0, []))
_A.append(('APP-VENDORPORTAL', 'Vendor Self-Service Portal', 'Low', 'Staging', 'Kubernetes', 'Linux/Ubuntu22', 2, 8, 200, 4, 'Tier-3', 'Internal', 'Atlanta-DC1', 9, 15, 'phx-05', 1.0, []))
_A.append(('APP-LEGACYMF', 'Legacy Statement Archive', 'Low', 'Production', 'BareMetal', 'Linux/RHEL9', 4, 16, 3500, 2, 'Tier-3', 'Internal', 'Phoenix-DC1', 20, 1, 'msp-03', 1.0, ['POOR_FIT']))

import seed_itsm  # noqa: E402  - sibling module, imported after the dataclasses it needs
import seed_scale  # noqa: E402

APPLICATIONS: list[AppDef] = []
for i, row in enumerate(_A, start=1):
    (code, name, crit, env, platform, os_req, cpu, mem, storage, growth, tier,
     classification, loc, owner, sg, host_cluster, alloc_factor, tags) = row
    hosted_since = ANCHOR_DATE - timedelta(days=rng.randint(200, 1100))
    APPLICATIONS.append(
        AppDef(
            i, code, name, crit, env, platform, os_req, cpu, mem, storage, growth, tier,
            classification, loc, owner, sg, host_cluster, alloc_factor, "Active", hosted_since, tags,
        )
    )

assert len(APPLICATIONS) == 40, len(APPLICATIONS)
APP_BY_CODE = {a.code: a for a in APPLICATIONS}

# =============================================================================
# Scale: bring the estate up to a realistic application-to-cluster ratio.
#
# The hand-written applications above stay exactly as they are - they carry the
# SCENARIOS fixtures the placement tests assert against, and regenerating them
# would break those tests for nothing. Everything below is added on top.
#
# 40 applications on 256 clusters meant 246 clusters hosted nothing and the
# capacity sub-score - the heaviest of the seven at 0.30 - was comparing empty
# boxes. See scripts/seed_scale.py for why packing rather than sprinkling.
# =============================================================================
def _make_app(*, idx, code, name, criticality, environment, platform, os, cpu_req,
              mem_req_gb, storage_req_gb, growth_pct, avail_tier, classification, hosted_since):
    return AppDef(
        idx, code, name, criticality, environment, platform, os, cpu_req, mem_req_gb,
        storage_req_gb, growth_pct, avail_tier, classification,
        None, rng.randint(1, len(EMPLOYEES)), rng.randint(1, len(SUPPORT_GROUPS)),
        "", 1.0, "Active", hosted_since, [],
    )


APPLICATIONS.extend(
    seed_scale.generate_applications(APPLICATIONS, rng, ANCHOR_DATE, _make_app)
)

# Draw a utilisation target per cluster, then pack applications in until each is
# reached. The stress figure this produces drives incident density and change
# failure rates downstream - if allocation were random those correlations would
# be decoration.
LOAD_PLANS = seed_scale.build_load_plans(CLUSTERS, rng)
HOSTING_ROWS, PRIMARY_CLUSTER_BY_APP = seed_scale.pack_applications(
    APPLICATIONS, CLUSTERS, LOAD_PLANS, rng, ANCHOR_DATE
)
for _a in APPLICATIONS:
    _a.primary_cluster_idx = PRIMARY_CLUSTER_BY_APP.get(_a.idx)

#: Applications the packer could not place, exported so the number is a measured
#: property rather than a discovery.
#:
#: pack_applications() deliberately refuses to allocate a cluster past 97% of its
#: physical cores, on the reasoning that an unhosted application is a data bug and
#: an overcommitted cluster is a worse one. That trade-off is right, and it was
#: invisible: 87 applications - every one of them Staging, where capacity is
#: tightest because non-production clusters are drawn at 0.75 of a production
#: target - ended up with no hosting row, no VM, and no relationship of any kind.
#: seekanddestroy-c2 found them while auditing CMDB orphans and had to chase them
#: back through three layers to establish they were not a bug in the CI graph.
#:
#: They are also realistic. A real bank's CMDB carries applications nobody ever
#: mapped to infrastructure - decommissioned, shadow IT, or registered ahead of a
#: build. So they stay, as a named fixture with a bound: if this list grows past a
#: tenth of the estate, the packer is failing rather than being principled.
_HOSTED_IDS = {row[0] for row in HOSTING_ROWS}
UNHOSTED_APPLICATIONS = [a.code for a in APPLICATIONS if a.idx not in _HOSTED_IDS]
assert len(UNHOSTED_APPLICATIONS) < len(APPLICATIONS) * 0.10, (
    f"{len(UNHOSTED_APPLICATIONS)} of {len(APPLICATIONS)} applications are unhosted - "
    "the packer is out of capacity, not exercising judgement"
)

# Rewrite each cluster's utilisation trend so the measured series agrees with
# what it now hosts. compute_cluster_capacity() takes the worse of allocated and
# measured, so leaving the old trend would have clusters that are 88% allocated
# reporting 12% measured - an estate that contradicts itself.
seed_scale.derive_utilisation_profiles(CLUSTERS, LOAD_PLANS, rng)


# Sanity-check the engineered-scenario counts promised by the specification.
_poor_fit = [a for a in APPLICATIONS if "POOR_FIT" in a.tags]
_consolidate = [a for a in APPLICATIONS if "CONSOLIDATE" in a.tags]
_expansion = [a for a in APPLICATIONS if "EXPANSION" in a.tags]
_strong_alt = [a for a in APPLICATIONS if "STRONG_ALT" in a.tags]
assert len(_poor_fit) == 5, len(_poor_fit)
assert len(_consolidate) == 4, len(_consolidate)
assert len(_expansion) == 3, len(_expansion)
assert len(_strong_alt) == 4, len(_strong_alt)

_overprov = [c for c in CLUSTERS if "OVERPROVISIONED" in c.tags]
_near_cpu = [c for c in CLUSTERS if "NEAR_CPU" in c.tags]
_near_mem = [c for c in CLUSTERS if "NEAR_MEM" in c.tags]
_high_cost = [c for c in CLUSTERS if "HIGH_COST_LOW_UTIL" in c.tags]
_suitable = [c for c in CLUSTERS if "SUITABLE_NEW" in c.tags]
_low_resil = [c for c in CLUSTERS if "LOW_RESILIENCY" in c.tags]
_compliance = [c for c in CLUSTERS if "COMPLIANCE_MISMATCH" in c.tags]
_forecast = [c for c in CLUSTERS if "FORECAST" in c.tags]
assert len(_overprov) == 3, len(_overprov)
assert len(_near_cpu) == 2, len(_near_cpu)
assert len(_near_mem) == 2, len(_near_mem)
assert len(_high_cost) == 2, len(_high_cost)
assert len(_suitable) == 3, len(_suitable)
assert len(_low_resil) == 2, len(_low_resil)
assert len(_compliance) == 2, len(_compliance)
assert len(_forecast) == 3, len(_forecast)

#: Exported for tests - the single source of truth for "which seeded entity
#: plays which engineered scenario", so assertions never hardcode magic codes.
SCENARIOS = {
    "overprovisioned_clusters": [c.code for c in _overprov],
    "nearing_cpu_clusters": [c.code for c in _near_cpu],
    "nearing_memory_clusters": [c.code for c in _near_mem],
    "high_cost_low_utilization_clusters": [c.code for c in _high_cost],
    "suitable_for_new_workloads_clusters": [c.code for c in _suitable],
    "insufficient_resiliency_clusters": [c.code for c in _low_resil],
    "compliance_mismatch_clusters": [c.code for c in _compliance],
    "forecast_exhaustion_clusters": [c.code for c in _forecast],
    "poor_fit_applications": [a.code for a in _poor_fit],
    "consolidation_applications": [a.code for a in _consolidate],
    "expansion_applications": [a.code for a in _expansion],
    "strong_alternative_applications": [a.code for a in _strong_alt],
    "unhosted_applications": UNHOSTED_APPLICATIONS,
}

# The CMDB fixtures live in seed_cmdb and are only produced when main() runs, so
# they are persisted to database/scenarios.json at generation time and merged back
# here. Without this, importing generate_seed gives a SCENARIOS dict that is
# missing every CMDB key - and a test using .get() on a missing key passes while
# asserting nothing.
def _load_persisted_scenarios() -> None:
    import json
    path = OUTPUT_PATH.parent / "scenarios.json"
    if not path.exists():
        return
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return          # a corrupt fixture file must not stop the generator
    for key, value in stored.items():
        SCENARIOS.setdefault(key, value)


_load_persisted_scenarios()


# =============================================================================
# 5. Time-series generation
# =============================================================================


def daily_value(day_index: int, start: float, end: float, noise: float) -> float:
    """Linear interpolation between day0 and day(HISTORY_DAYS-1) plus jitter."""
    t = day_index / (HISTORY_DAYS - 1)
    base = start + (end - start) * t
    jitter = rng.uniform(-noise, noise)
    return clamp(base + jitter)


def build_cluster_utilization_rows() -> list[tuple]:
    rows = []
    for c in CLUSTERS:
        cpu0, cpu1, mem0, mem1, sto0, sto1 = c.util_profile
        workload_count = sum(1 for a in APPLICATIONS if a.host_cluster_code == c.code)
        for d in range(HISTORY_DAYS):
            day = ANCHOR_DATE - timedelta(days=HISTORY_DAYS - 1 - d)
            metric_dt = datetime.combine(day, datetime.min.time()) + timedelta(hours=6)
            cpu = daily_value(d, cpu0, cpu1, 3.0)
            mem = daily_value(d, mem0, mem1, 3.0)
            sto = daily_value(d, sto0, sto1, 1.5)
            net = clamp(25 + rng.uniform(-8, 8))
            active_workloads = max(1, workload_count + rng.randint(-1, 1))
            request_volume = max(0, int((c.total_cpu * 800) + rng.uniform(-5000, 5000)))
            rows.append(
                (c.idx, metric_dt, r2(cpu), r2(mem), r2(sto), r2(net), active_workloads, request_volume)
            )
    return rows


#: Node-level utilization isn't read by any capacity/scoring/right-sizing/
#: forecasting calculation (those all read ClusterUtilization) - it's purely
#: informational (get_node_utilization tool/endpoint). The 75 nodes on the 15
#: hand-crafted scenario clusters keep full 180-day history (unchanged,
#: several tests were written against it); the ~2,400 nodes on the 241
#: procedurally-generated clusters get a much shorter window so seeding
#: 256 clusters' worth of nodes doesn't produce hundreds of thousands of
#: rows nobody reads.
PROCEDURAL_NODE_HISTORY_DAYS = 14


def build_node_utilization_rows(cluster_util_by_cluster_day: dict) -> list[tuple]:
    rows = []
    for n in NODES:
        is_hand_crafted = n.cluster.idx <= 15
        days = HISTORY_DAYS if is_hand_crafted else PROCEDURAL_NODE_HISTORY_DAYS
        start_day = HISTORY_DAYS - days
        for d in range(start_day, HISTORY_DAYS):
            cpu, mem, sto = cluster_util_by_cluster_day[(n.cluster.idx, d)]
            node_cpu = clamp(cpu + rng.uniform(-5, 5))
            node_mem = clamp(mem + rng.uniform(-5, 5))
            node_sto = clamp(sto + rng.uniform(-3, 3))
            node_net = clamp(25 + rng.uniform(-10, 10))
            day = ANCHOR_DATE - timedelta(days=HISTORY_DAYS - 1 - d)
            metric_dt = datetime.combine(day, datetime.min.time()) + timedelta(hours=6, minutes=15)
            rows.append((n.idx, metric_dt, r2(node_cpu), r2(node_mem), r2(node_sto), r2(node_net)))
    return rows


def build_application_usage_rows() -> list[tuple]:
    rows = []
    criticality_volume = {"Critical": 1.0, "High": 0.7, "Medium": 0.4, "Low": 0.15}
    for a in APPLICATIONS:
        util_ratio_start = 0.45 + rng.uniform(-0.05, 0.05)
        growth_fraction = a.growth_pct / 100.0
        util_ratio_end = clamp(util_ratio_start * (1 + growth_fraction * 0.5), 0.2, 0.97) / 100 * 100
        util_ratio_end = min(0.95, util_ratio_start * (1 + growth_fraction))
        vol_factor = criticality_volume[a.criticality]
        for d in range(HISTORY_DAYS):
            t = d / (HISTORY_DAYS - 1)
            ratio = util_ratio_start + (util_ratio_end - util_ratio_start) * t
            ratio = clamp(ratio + rng.uniform(-0.03, 0.03), 0.05, 0.98) / 100 * 100
            ratio = ratio / 100.0
            day = ANCHOR_DATE - timedelta(days=HISTORY_DAYS - 1 - d)
            usage_dt = datetime.combine(day, datetime.min.time()) + timedelta(hours=9)
            user_count = max(1, int(500 * vol_factor * (0.8 + 0.4 * ratio) + rng.uniform(-20, 20)))
            request_count = max(0, int(user_count * rng.uniform(15, 40)))
            cpu_consumed = max(0.01, r2(a.cpu_req * ratio))
            mem_consumed = max(0.01, r2(a.mem_req_gb * ratio))
            storage_consumed = max(0.01, r2(a.storage_req_gb * clamp(0.4 + ratio * 0.3, 0, 1)))
            base_latency = 300 if "POOR_FIT" in a.tags else 150
            response_ms = max(20, int(base_latency + rng.uniform(-40, 80)))
            rows.append(
                (a.idx, usage_dt, user_count, request_count, cpu_consumed, mem_consumed,
                 storage_consumed, response_ms)
            )
    return rows


# =============================================================================
# 6. Application hosting
# =============================================================================


def build_hosting_rows() -> list[tuple]:
    """Produced by the packer during module import - see seed_scale.

    Previously one row per application, always Production, always IsPrimary=1.
    Now each application has a primary, usually a smaller Staging copy, often a
    Test or Development copy, and - when it is Critical or High - a DR standby:
    a second Production row in a different data centre with IsPrimary = 0.
    """
    return HOSTING_ROWS


# =============================================================================
# 7. Application dependencies
# =============================================================================


def build_dependency_rows() -> list[tuple]:
    edges = [
        ("APP-PAYMENTS", "APP-LEDGER", "SynchronousApi", "High", True),
        ("APP-PAYMENTS", "APP-FRAUD", "SynchronousApi", "High", True),
        ("APP-PAYMENTS", "APP-SETTLEMENT", "AsynchronousMessaging", "Medium", True),
        ("APP-CARDS", "APP-FRAUD", "SynchronousApi", "High", True),
        ("APP-CARDS", "APP-KYC", "SynchronousApi", "Medium", False),
        ("APP-LOANS", "APP-KYC", "SynchronousApi", "Medium", True),
        ("APP-LOANS", "APP-RISKENGINE", "SynchronousApi", "High", True),
        ("APP-TREASURY", "APP-LEDGER", "Database", "High", True),
        ("APP-RECON", "APP-LEDGER", "Database", "Medium", False),
        ("APP-SETTLEMENT", "APP-LEDGER", "Database", "High", True),
        ("APP-CRM", "APP-IDENTITY", "Authentication", "Medium", True),
        ("APP-WEBPORTAL", "APP-SSO", "Authentication", "High", True),
        ("APP-MOBILEBFF", "APP-SSO", "Authentication", "High", True),
        ("APP-MOBILEBFF", "APP-APIGATEWAY", "SynchronousApi", "High", True),
        ("APP-WEBPORTAL", "APP-APIGATEWAY", "SynchronousApi", "High", True),
        ("APP-ONBOARDING", "APP-KYC", "SynchronousApi", "Medium", True),
        ("APP-ONBOARDING", "APP-IDENTITY", "Authentication", "Medium", True),
        ("APP-BILLING", "APP-LEDGER", "Database", "Medium", False),
        ("APP-INVOICING", "APP-BILLING", "SynchronousApi", "Low", False),
        ("APP-PAYROLL", "APP-HRPORTAL", "SynchronousApi", "Low", False),
        ("APP-ANALYTICS", "APP-DATALAKE", "FileTransfer", "Low", False),
        ("APP-MLSCORING", "APP-DATALAKE", "FileTransfer", "Medium", True),
        ("APP-MLSCORING", "APP-RISKENGINE", "SynchronousApi", "High", True),
        ("APP-ETL", "APP-DATALAKE", "FileTransfer", "Low", False),
        ("APP-COMPLIANCE", "APP-AUDIT", "AsynchronousMessaging", "Low", True),
        ("APP-DOCSIGN", "APP-IDENTITY", "Authentication", "Medium", False),
        ("APP-NOTIFICATIONS", "APP-CRM", "AsynchronousMessaging", "Low", False),
        ("APP-SEARCH", "APP-DOCSTORE", "SynchronousApi", "Low", False),
        ("APP-CHATBOT", "APP-CRM", "SynchronousApi", "Low", False),
        # Cross-region, high-latency-sensitive, critical dependency -> hard RULE-008
        # failure fixture: APP-FRAUD (hosted Chennai) depends synchronously and
        # critically on APP-IDENTITY (hosted Mumbai) with high latency sensitivity.
        ("APP-FRAUD", "APP-IDENTITY", "SynchronousApi", "High", True),
    ]
    rows = []
    for source_code, target_code, dep_type, latency, critical in edges:
        source = APP_BY_CODE[source_code]
        target = APP_BY_CODE[target_code]
        rows.append((source.idx, target.idx, None, dep_type, latency, critical, True))

    # A couple of cluster-level dependencies (workload pinned to infra in a region).
    cluster_edges = [
        ("APP-BATCHSCHED", "cmh-03", "FileTransfer", "Low", False),
        ("APP-DATALAKE", "clt-13", "FileTransfer", "Medium", False),
    ]
    for source_code, cluster_code, dep_type, latency, critical in cluster_edges:
        source = APP_BY_CODE[source_code]
        cluster = CLUSTER_BY_CODE[cluster_code]
        rows.append((source.idx, None, cluster.idx, dep_type, latency, critical, True))

    return rows


# =============================================================================
# 8. Incidents
# =============================================================================


def build_incident_rows() -> list[tuple]:
    rows = []
    root_causes = ["Capacity", "Configuration", "Hardware", "Network", "Software", "Dependency", "Unknown"]

    def add(app_code, cluster_code, node_id, severity, days_ago, duration_hours, status, root_cause):
        app = APP_BY_CODE.get(app_code)
        cluster = CLUSTER_BY_CODE.get(cluster_code)
        opened = datetime.combine(ANCHOR_DATE, datetime.min.time()) - timedelta(
            days=days_ago, hours=rng.randint(0, 23)
        )
        closed = None
        if status in ("Resolved", "Closed"):
            closed = opened + timedelta(hours=duration_hours)
        rows.append(
            (
                app.idx if app else None,
                cluster.idx if cluster else None,
                node_id,
                severity,
                opened,
                closed,
                status,
                root_cause,
            )
        )

    # Weighted toward the poor-fit / low-resiliency / nearing-capacity fixtures so
    # HistoricalPerformance and OperationalRisk sub-scores have real signal.
    for code in SCENARIOS["poor_fit_applications"]:
        add(code, None, None, "Sev2", rng.randint(5, 60), rng.randint(2, 10), "Resolved", "Capacity")
        add(code, None, None, "Sev3", rng.randint(5, 90), rng.randint(1, 6), "Resolved", "Configuration")

    for code in SCENARIOS["insufficient_resiliency_clusters"]:
        add(None, code, None, "Sev1", rng.randint(5, 45), rng.randint(1, 8), "Resolved", "Hardware")
        add(None, code, None, "Sev2", rng.randint(5, 100), rng.randint(2, 12), "Resolved", "Hardware")

    for code in SCENARIOS["nearing_cpu_clusters"] + SCENARIOS["nearing_memory_clusters"]:
        add(None, code, None, "Sev2", rng.randint(5, 80), rng.randint(1, 6), "Resolved", "Capacity")

    # Open incidents (unresolved) on the current pain points.
    add(None, "phx-03", None, "Sev2", 3, 0, "Open", "Capacity")
    add(None, "cmh-03", None, "Sev1", 1, 0, "InProgress", "Hardware")
    add("APP-BATCHSCHED", None, None, "Sev2", 2, 0, "Open", "Dependency")

    # A general background of routine incidents spread across the estate so
    # historical-performance scoring is not trivially binary.
    all_app_codes = [a.code for a in APPLICATIONS]
    all_cluster_codes = [c.code for c in CLUSTERS]
    for _ in range(40):
        target_is_app = rng.random() < 0.6
        severity = rng.choices(["Sev1", "Sev2", "Sev3", "Sev4"], weights=[5, 15, 45, 35])[0]
        days_ago = rng.randint(1, 179)
        duration = rng.randint(1, 24)
        root_cause = rng.choice(root_causes)
        if target_is_app:
            add(rng.choice(all_app_codes), None, None, severity, days_ago, duration, "Closed", root_cause)
        else:
            add(None, rng.choice(all_cluster_codes), None, severity, days_ago, duration, "Closed", root_cause)

    return rows


# =============================================================================
# 9. Capacity requests
# =============================================================================


def build_capacity_request_rows() -> list[tuple]:
    rows = []

    def add(app_code, requested_by_idx, environment, cpu, mem, storage, growth, tier, platform,
            location, classification, required_by_days, status):
        app = APP_BY_CODE.get(app_code)
        required_by = ANCHOR_DATE + timedelta(days=required_by_days) if required_by_days else None
        rows.append(
            (
                app.idx if app else None,
                requested_by_idx,
                environment,
                cpu, mem, storage, growth, tier, platform, location, classification,
                required_by, status,
            )
        )

    # Scenario A/B follow-ups for the 3 EXPANSION applications.
    add("APP-PAYMENTS", 1, "Production", 14, 56, 1500, 40, "Tier-1", "Kubernetes",
        "Atlanta-DC1", "Restricted", 45, "Open")
    add("APP-LEDGER", 1, "Production", 10, 40, 1000, 35, "Tier-1", "Kubernetes",
        "Atlanta-DC1", "Restricted", 60, "Open")
    add("APP-MLSCORING", 20, "Production", 12, 48, 500, 45, "Tier-2", "Kubernetes",
        "Charlotte-DC1", "Confidential", 30, "InAnalysis")

    # Scenario B - pure "new space" requirements not tied to an existing application.
    add(None, 5, "Production", 16, 64, 2000, 20, "Tier-1", "Kubernetes",
        "Atlanta-DC1", "Confidential", 90, "Open")
    add(None, 8, "Production", 8, 32, 1000, 15, "Tier-2", "VMware",
        "New York-DC1", "Internal", 45, "Open")
    add(None, 14, "Production", 24, 96, 3000, 30, "Tier-1", "Kubernetes",
        None, "Restricted", 75, "Recommended")
    add(None, 9, "Staging", 6, 24, 500, 10, "Tier-3", "Kubernetes",
        "New York-DC1", "Internal", 20, "Approved")
    add(None, 17, "Production", 4, 16, 300, 8, "Tier-2", "Kubernetes",
        None, "Internal", None, "Cancelled")
    add(None, 6, "Production", 10, 40, 800, 12, "Tier-2", "OpenShift",
        "Phoenix-DC1", "Confidential", 40, "Open")
    add(None, 19, "Production", 6, 24, 400, 10, "Tier-1", "Hyper-V",
        "Columbus-DC1", "Internal", 25, "Rejected")

    return rows


# =============================================================================
# main
# =============================================================================


def main() -> None:
    lines: list[str] = []
    lines.append("/* =============================================================================")
    lines.append("   SeekAndDestroy - deterministic seed data")
    lines.append(f"   Generated by scripts/generate_seed.py (SEED={SEED}, ANCHOR_DATE={ANCHOR_DATE}).")
    lines.append("   Do not hand-edit. Regenerate with:")
    lines.append("     .venv\\Scripts\\python.exe scripts\\generate_seed.py")
    lines.append("   Apply with:")
    lines.append("     sqlcmd -S LAPTOP-R6U8H616 -d PraveenDB -E -C -i database\\seed.sql")
    lines.append("============================================================================= */")
    lines.append("SET NOCOUNT ON;")
    # Required, not cosmetic. sad.Incident carries UQ_Incident_Number, a
    # FILTERED unique index (migration_007), and SQL Server refuses any INSERT
    # into a table with one unless QUOTED_IDENTIFIER is ON. sqlcmd does not
    # guarantee it, so without this the very first incident INSERT fails with a
    # message about SET options that names neither the index nor the table.
    lines.append("SET QUOTED_IDENTIFIER ON;")
    lines.append("SET ANSI_NULLS ON;")
    lines.append("GO")
    lines.append("")

    # --- Employee ---
    lines.append("-- 20 employees")
    emit_inserts(
        lines, "sad.Employee", ["EmployeeNumber", "DisplayName", "Email", "IsActive"],
        [(e["number"], e["name"], e["email"], e["active"]) for e in EMPLOYEES],
    )

    # --- SupportGroup ---
    lines.append("-- 8 support groups")
    emit_inserts(
        lines, "sad.SupportGroup", ["GroupName", "Email", "IsActive"],
        [(g["name"], g["email"], g["active"]) for g in SUPPORT_GROUPS],
    )

    # --- CmdbApplication ---
    lines.append("-- 40 applications")
    emit_inserts(
        lines, "sad.CmdbApplication",
        [
            "ApplicationCode", "ApplicationName", "Description", "BusinessCriticality", "Environment",
            "LifecycleStatus", "TechnologyPlatform", "OperatingSystemRequirement", "CpuRequirement",
            "MemoryRequirementGb", "StorageRequirementGb", "ExpectedAnnualGrowthPercent",
            "AvailabilityTier", "DataClassification", "PreferredLocation", "OwnerEmployeeId",
            "SupportGroupId",
        ],
        [
            (
                a.code, a.name, f"{a.name} - seeded CMDB record.", a.criticality, a.environment,
                "Active", a.platform, a.os, a.cpu_req, a.mem_req_gb, a.storage_req_gb, a.growth_pct,
                a.avail_tier, a.classification, a.preferred_location, a.owner_idx, a.sg_idx,
            )
            for a in APPLICATIONS
        ],
    )

    # --- Neighborhood ---
    lines.append("-- 8 neighborhoods (data-center sub-groupings)")
    emit_inserts(
        lines, "sad.Neighborhood",
        ["NeighborhoodCode", "NeighborhoodName", "DataCenter", "Region"],
        [(code, name, dc, region) for code, name, dc, region in NEIGHBORHOODS],
    )

    # --- InfrastructureCluster ---
    lines.append("-- 15 infrastructure clusters")
    emit_inserts(
        lines, "sad.InfrastructureCluster",
        [
            "ClusterCode", "ClusterName", "ClusterType", "Platform", "OperatingSystem", "Environment",
            "NeighborhoodId", "DataCenter", "Region", "LifecycleStatus", "NodeCount", "TotalCpuCores",
            "TotalMemoryGb", "TotalStorageGb", "ReservedCpuPercent", "ReservedMemoryPercent", "MonthlyCost",
            "AvailabilityTier", "ComplianceClassification",
        ],
        [
            (
                c.code, c.name, c.ctype, c.platform, c.os, c.environment,
                NEIGHBORHOOD_INDEX[c.neighborhood_code], c.datacenter, c.region,
                c.lifecycle, c.node_count, c.total_cpu, c.total_mem_gb, c.total_storage_gb,
                c.reserved_cpu_pct, c.reserved_mem_pct, c.monthly_cost, c.availability_tier,
                c.compliance,
            )
            for c in CLUSTERS
        ],
    )

    # --- ClusterNode ---
    lines.append("-- 75 cluster nodes")
    emit_inserts(
        lines, "sad.ClusterNode",
        ["ClusterId", "HostName", "IpAddress", "CpuCores", "MemoryGb", "StorageGb", "LifecycleStatus",
         "LastSeenAt", "MonthlyCost"],
        [
            (n.cluster.idx, n.host_name, n.ip_address, n.cpu_cores, n.memory_gb, n.storage_gb,
             n.lifecycle, n.last_seen, n.monthly_cost)
            for n in NODES
        ],
    )

    # --- ApplicationHosting ---
    lines.append("-- application hosting relationships (1 per application)")
    emit_inserts(
        lines, "sad.ApplicationHosting",
        ["ApplicationId", "ClusterId", "NodeId", "Environment", "AllocatedCpuCores",
         "AllocatedMemoryGb", "AllocatedStorageGb", "HostingStatus", "IsPrimary", "HostedSince"],
        build_hosting_rows(),
    )

    # --- ClusterUtilization (180 days x 15 clusters) ---
    lines.append("-- cluster utilization: 180 days x 15 clusters")
    cluster_util_rows = build_cluster_utilization_rows()
    # index by (cluster_idx, day_index) for the node-utilization derivation step
    cluster_util_by_cluster_day = {}
    for c in CLUSTERS:
        cpu0, cpu1, mem0, mem1, sto0, sto1 = c.util_profile
        for d in range(HISTORY_DAYS):
            # Recompute the *noiseless* trend line for node derivation so nodes
            # track the cluster trend without inheriting the cluster's own jitter.
            t = d / (HISTORY_DAYS - 1)
            cpu = cpu0 + (cpu1 - cpu0) * t
            mem = mem0 + (mem1 - mem0) * t
            sto = sto0 + (sto1 - sto0) * t
            cluster_util_by_cluster_day[(c.idx, d)] = (cpu, mem, sto)
    emit_inserts(
        lines, "sad.ClusterUtilization",
        ["ClusterId", "MetricDateTime", "CpuUsedPercent", "MemoryUsedPercent", "StorageUsedPercent",
         "NetworkUsedPercent", "ActiveWorkloadCount", "RequestVolume"],
        cluster_util_rows,
    )

    # --- NodeUtilization (180 days x 75 nodes) ---
    lines.append("-- node utilization: 180 days x 75 nodes")
    emit_inserts(
        lines, "sad.NodeUtilization",
        ["NodeId", "MetricDateTime", "CpuUsedPercent", "MemoryUsedPercent", "StorageUsedPercent",
         "NetworkUsedPercent"],
        build_node_utilization_rows(cluster_util_by_cluster_day),
    )

    # --- ApplicationUsage (180 days x 40 applications) ---
    lines.append("-- application usage: 180 days x 40 applications")
    emit_inserts(
        lines, "sad.ApplicationUsage",
        ["ApplicationId", "UsageDateTime", "UserCount", "RequestCount", "CpuConsumed",
         "MemoryConsumedGb", "StorageConsumedGb", "ResponseTimeMs"],
        build_application_usage_rows(),
    )

    # --- ApplicationDependency ---
    lines.append("-- application dependencies")
    emit_inserts(
        lines, "sad.ApplicationDependency",
        ["SourceApplicationId", "TargetApplicationId", "TargetClusterId", "DependencyType",
         "LatencySensitivity", "IsCritical", "IsActive"],
        build_dependency_rows(),
    )

    # --- Change / Problem / Incident and their comments ---------------------
    # Order is dictated by the foreign keys: Problem.PermanentFixChangeId and
    # Incident.CausedByChangeId both point at Change, and Incident.ProblemId
    # points at Problem. Identity values are implied by insertion order, the
    # same convention the rest of this file uses.
    anchor_dt = datetime.combine(ANCHOR_DATE, datetime.min.time())
    change_rows, change_comment_rows, _freezes = seed_itsm.build_changes(
        CLUSTERS, APPLICATIONS, LOAD_PLANS, rng, anchor_dt
    )
    lines.append(f"-- {len(change_rows)} change requests")
    emit_inserts(
        lines, "sad.Change",
        ["Number", "ShortDescription", "Description", "Type", "State", "ClusterId", "NodeId",
         "ApplicationId", "PlannedStart", "PlannedEnd", "ActualStart", "ActualEnd", "CloseCode",
         "CloseNotes", "ImplementationPlan", "BackoutPlan", "RiskAssessment", "AssignmentGroup",
         "FreezeUntil"],
        change_rows,
    )

    incident_rows, incident_comment_rows, event_ids = seed_itsm.build_incidents(
        CLUSTERS, APPLICATIONS, NODES, LOAD_PLANS, len(change_rows), rng, anchor_dt
    )
    problem_rows = seed_itsm.build_problems(
        CLUSTERS, LOAD_PLANS, event_ids, len(change_rows), rng, anchor_dt
    )
    lines.append(f"-- {len(problem_rows)} problem records")
    emit_inserts(
        lines, "sad.Problem",
        ["Number", "ShortDescription", "Description", "RootCause", "Workaround", "FixNotes",
         "IsKnownError", "State", "PermanentFixChangeId", "ClusterId", "ApplicationId",
         "OpenedAt", "ClosedAt"],
        problem_rows,
    )

    # Fill ProblemId and CausedByChangeId now that both sides exist. Without
    # this the columns are present on 10,000 rows and populated on none, and
    # "has this happened before" has nothing to follow.
    linked_p, linked_c = seed_itsm.link_incidents(
        incident_rows, problem_rows, event_ids, change_rows, rng
    )
    lines.append(f"-- {len(incident_rows)} incidents "
                 f"({linked_p} linked to a problem, {linked_c} to a causing change)")
    emit_inserts(
        lines, "sad.Incident",
        ["ApplicationId", "ClusterId", "NodeId", "Severity", "OpenedAt", "ClosedAt", "Status",
         "RootCauseCategory", "Number", "ShortDescription", "Description", "CloseNotes",
         "AssignmentGroup", "Impact", "Urgency", "ProblemId", "CausedByChangeId"],
        incident_rows,
    )

    lines.append(f"-- {len(incident_comment_rows)} incident work notes")
    emit_inserts(
        lines, "sad.IncidentComment",
        ["IncidentId", "Sequence", "CreatedAt", "CreatedBy", "Type", "Text"],
        incident_comment_rows,
    )

    lines.append(f"-- {len(change_comment_rows)} change work notes")
    emit_inserts(
        lines, "sad.ChangeComment",
        ["ChangeId", "Sequence", "CreatedAt", "CreatedBy", "Type", "Text"],
        change_comment_rows,
    )

    # --- CapacityRequest ---
    lines.append("-- capacity requests")
    emit_inserts(
        lines, "sad.CapacityRequest",
        ["ApplicationId", "RequestedBy", "Environment", "RequiredCpuCores", "RequiredMemoryGb",
         "RequiredStorageGb", "ExpectedGrowthPercent", "RequiredAvailabilityTier", "RequiredPlatform",
         "PreferredLocation", "DataClassification", "RequiredByDate", "Status"],
        build_capacity_request_rows(),
    )

    # The CMDB layer goes last: every configuration item, class table and edge has
    # a foreign key into rows the blocks above created, and the whole graph
    # references CiIds this module allocates rather than ones the database picks.
    import seed_cmdb
    estate = seed_cmdb.build(CLUSTERS, APPLICATIONS, NODES, rng, ANCHOR_DATE,
                             NEIGHBORHOOD_INDEX)
    seed_cmdb.emit(lines, estate)
    SCENARIOS.update(estate.scenarios)

    # Persisted so tests can read the fixtures WITHOUT regenerating a 74 MB seed.
    #
    # The CMDB fixtures are produced by seed_cmdb.build(), which runs here in
    # main() - so a test that imports this module never sees them, and
    # SCENARIOS silently contains only the older cluster and application keys.
    # That is worse than an error: an assertion reading SCENARIOS["orphan_cis"]
    # raises KeyError, but one reading .get("orphan_cis", []) passes vacuously.
    #
    # This file IS committed, unlike seed.sql. It is a few kilobytes, it is the
    # contract between the generator and every test that asserts against it, and
    # regenerating the seed rewrites it in step.
    import json as _json
    scenarios_path = OUTPUT_PATH.parent / "scenarios.json"
    scenarios_path.write_text(
        _json.dumps(SCENARIOS, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(f"  fixtures: {scenarios_path} ({len(SCENARIOS)} keys)")
    print(f"  CMDB: {len(estate.cis):,} configuration items, {len(estate.edges):,} relationships")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} ({len(lines)} lines)")
    loaded = [p for p in LOAD_PLANS.values() if p.app_count]
    print(f"  incidents linked  : {linked_p} to a problem, {linked_c} to a change")
    print(f"  applications      : {len(APPLICATIONS)}")
    print(f"  hosting rows      : {len(HOSTING_ROWS)}")
    print(f"  clusters occupied : {len(loaded)} of {len(CLUSTERS)}")
    if loaded:
        stressed = [p for p in loaded if p.stress > 0.6]
        print(f"  stressed (>70%)   : {len(stressed)}")
        print(f"  mean allocation   : {sum(p.achieved_cpu_pct for p in loaded)/len(loaded):.1f}% CPU")
    print("Scenario map:")
    for k, v in SCENARIOS.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
