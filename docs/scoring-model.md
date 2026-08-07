# SeekAndDestroy — Scoring Model

Implemented in `ai-service/app/scoring/` (`weights.py`, `subscores.py`, `engine.py`). Only candidates that pass all ten hard rules (`docs/business-rules.md`) are scored; rejected candidates are explained but never ranked against eligible ones.

There are **two scales**, and they are not comparable: the cluster score below, and the node score used to rank hosts inside a chosen cluster (see [Node scoring](#node-scoring--ranking-hosts-inside-a-cluster)).

## Score formula

```
Overall = 0.30 * Capacity + 0.15 * Compatibility + 0.15 * Resiliency
        + 0.15 * CostEfficiency + 0.10 * DependencyLocality
        + 0.10 * HistoricalPerformance + 0.05 * (100 - OperationalRisk)
```

Every term is computed in `decimal.Decimal` and rounded `ROUND_HALF_UP` to 2 decimal places at each step - re-running the same inputs always produces the byte-identical score (`test_candidate_scores_are_reproducible`).

## Weight configuration

Defaults (`SAD_SCORING__WEIGHT_*` in `.env`, always validated to sum to 1.0 by `ScoringSettings`):

| Weight | Default |
|---|---|
| Capacity | 0.30 |
| Compatibility | 0.15 |
| Resiliency | 0.15 |
| Cost | 0.15 |
| Dependency | 0.10 |
| Historical | 0.10 |
| Risk | 0.05 |

## Sub-score formulas

### Capacity (0-100)
```
target_headroom = average(100 - cpu_threshold, 100 - memory_threshold, 100 - storage_threshold)   # default 20
capacity_score  = clamp(projected_headroom_percent / target_headroom * 100, 0, 100)
```

### Compatibility (0-100)
```
base = 100 if candidate.Platform == requirement.platform else 82   # compatible-but-not-exact, e.g. K8s app on OpenShift
if preferred_location set and candidate.DataCenter != preferred_location:
    base -= 10
compatibility_score = clamp(base, 0, 100)
```

### Resiliency (0-100)
```
tier_base = {Tier-1: 100, Tier-2: 80, Tier-3: 60}[candidate.AvailabilityTier]
bonus     = min(20, max(0, active_node_count - min_required_nodes) * 5)
if active_node_count < min_required_nodes:
    tier_base *= 0.5   # cannot structurally back its own advertised tier
resiliency_score = clamp(tier_base + bonus, 0, 100)
```
`min_required_nodes` is 3 for Critical workloads, 2 for High, 1 otherwise (mirrors RULE-010).

### Cost efficiency (0-100) — batch-normalized
Computed once per eligible candidate *set*, not per candidate in isolation:
```
cost_efficiency_score(cluster) = 100 * (max_cost - cluster.estimated_cost) / (max_cost - min_cost)
```
(100 for every candidate if all costs are equal.) `estimated_monthly_cost = cluster.MonthlyCost * max(cpu_share, memory_share)` where `*_share = requirement / cluster.Total*`.

### Dependency locality (0-100)
Weighted average over the requirement's resolved dependencies, weight = `1 + (1 if critical) + (1 if High latency-sensitivity, else 0.5 if Medium)`:
```
same data center   -> 100
same region, diff DC -> 75
different region     -> 40
unresolved target     -> 0
```
No dependencies → neutral 100.

### Historical performance (0-100)
```
weight(Sev1)=10, weight(Sev2)=5, weight(Sev3)=2, weight(Sev4)=1
score = clamp(100 - 2 * sum(weight(incident.Severity) for incident in last 90 days on this cluster), 0, 100)
```

### Operational risk (0-100, higher = worse; the overall formula uses `100 - risk`)
```
risk  = clamp(stddev(30d CPU series), 0, 20)
      + clamp(stddev(30d memory series), 0, 20)
      + (30 if cluster.LifecycleStatus == 'Deprecated' else 0)
      + min(30, 15 * count(open Sev1/Sev2 incidents))
      + (15 if forecast breaches threshold within 90 days else 0)
```

## Ranking — total order

```
sort key = (overall_score DESC, estimated_monthly_cost ASC, cluster_code ASC)
```
No ties are possible (cluster codes are unique), so ranking is fully deterministic (`test_best_candidate_is_ranked_correctly`). Eligible candidates are ranked 1..N; rejected candidates are listed after, sorted alphabetically by cluster code.

## Node scoring — ranking hosts inside a cluster

A second, separate scale. Once a cluster is chosen, `app/services/node_placement.py` ranks the hosts inside it.

```
NodeOverall = 0.50 * Capacity + 0.20 * CostEfficiency
            + 0.20 * Reliability + 0.10 * (100 - OperationalRisk)
```

Four sub-scores, not seven: **compatibility, resiliency tier and dependency locality are properties of the cluster**, identical for every host in it. They order clusters; they cannot order hosts. Capacity carries more weight here (0.50 vs 0.30) because by this point the only questions left are "does the workload fit on this host", "what does this host cost" and "is this host healthy".

Weights are `SAD_SCORING__NODE_WEIGHT_*`, validated to sum to 1.0 by `ScoringSettings` independently of the cluster weights.

### Node capacity (0-100) — deliberately NOT the cluster formula
```
node_capacity_score = clamp(projected_headroom_percent, 0, 100)
```
The cluster formula divides by a 20% target headroom and clamps at 100. That is right across a varied estate, and useless within one cluster: hosts inside a cluster are near-identical by construction, so every host clearing the target saturates at 100 and the ranking silently collapses to alphabetical order by hostname. Using headroom directly preserves the small real differences that actually decide. Guarded by `test_node_capacity_subscore_does_not_saturate_between_healthy_hosts`.

**Consequence when reading a number:** node scores are only meaningful *relative to other hosts in the same cluster*. A host scoring 63 is not "worse" than its cluster scoring 91 — they are on different scales.

### Node cost efficiency (0-100) — normalized within the cluster
Same min-max formula as the cluster version, but over the *sibling hosts only*: it answers "is this host cheap for this cluster". Normalizing across the estate would just re-derive the cluster ordering. `estimated_monthly_cost = node.MonthlyCost * max(cpu_share, memory_share)` where the shares are of the host's **portion** of the raw requirement (see the `share`/`whole` split in business-rules.md § 1b).

Where hosts in a cluster share one chargeback rate (as in the seed estate), every host scores 100 and this term stops discriminating — by design, since they genuinely cost the same.

### Node reliability (0-100)
Reuses `historical_performance_subscore` against incidents recorded on that specific host (`Incident.NodeId`) in the last 90 days.

### Node operational risk (0-100, higher = worse)
```
risk  = (30 if Deprecated else 15 if not Active else 0)
      + min(40, 20 * count(open Sev1/Sev2 incidents on this host))
      + ramp to 25 as staleness exceeds node_stale_after_days
      + (20 if no utilization samples in the window)
```
Not the cluster risk formula: that one is driven by utilization *volatility*, which would mean pulling a full 30-day series for every host in every shortlisted cluster to order a handful of siblings. The node signals that actually discriminate are reporting freshness, lifecycle and open severe incidents.

### Node ranking — total order
```
sort key = (overall_score DESC, estimated_monthly_cost ASC, host_name ASC)
```
Host names are unique (`UQ_ClusterNode_HostName`), so no ties are possible. Eligible hosts rank 1..N *within their cluster*; rejected hosts follow, alphabetically — "why not that host" stays answerable.

## Worked example: APP-CRM

From a live run against the seed data (`nyc-03` eligible, cost $9,500/mo):

```json
{
  "capacity": 100.00, "compatibility": 90.00, "resiliency": 100.00,
  "cost": 0.00, "dependency": 40.00, "historical": 90.00, "risk": 3.23
}
```
```
Overall = 0.30*100 + 0.15*90 + 0.15*100 + 0.15*0 + 0.10*40 + 0.10*90 + 0.05*(100-3.23)
        = 30 + 13.5 + 15 + 0 + 4 + 9 + 4.84 = 76.34
```
matches the persisted `OverallScore` exactly.

## Sample candidate comparison

Comparing `atl-03` and `den-03` for `APP-CRM` (see `docs/demo-scenarios.md` for the full walkthrough): `atl-03` wins on capacity (more headroom) and cost (cheaper share); `den-03` is the exact-platform match. The `/api/recommendations/hosting` response includes every sub-score for both so the trade-off is visible, not just the winner.
