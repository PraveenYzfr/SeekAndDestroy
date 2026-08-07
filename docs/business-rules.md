# SeekAndDestroy — Business Rules and Guardrails

## 1. Eligibility rules (RULE-001 .. RULE-010)

Implemented in `ai-service/app/rules/eligibility.py`. All ten always run against every candidate (even after a failure), so a rejected candidate's explanation lists every reason, not just the first. A candidate is `Eligible` only if all ten pass.

| Rule | Name | Logic |
|---|---|---|
| RULE-001 | Environment compatibility | `Production` workloads require a `Production` cluster. Non-production environments must match exactly (`environment_compatible`). |
| RULE-002 | Platform compatibility | Cluster platform must be in the app's compatible set (`Kubernetes` apps may run on `Kubernetes` or `OpenShift` clusters; the reverse is not true) **and** the OS family (`Linux`/`Windows`/`Any`) must match. |
| RULE-003 | Capacity requirement | `available_* >= required_*_effective` for CPU, memory and storage, where `required_*_effective` already includes growth and the safety margin (see §2). |
| RULE-004 | Availability requirement | `rank(candidate_tier) <= rank(required_tier)`, Tier-1=1 (strongest) .. Tier-3=3. |
| RULE-005 | Data classification | `level(cluster.ComplianceClassification) >= level(app.DataClassification)`, `Public(0) < Internal(1) < Confidential(2) < Restricted(3)`. |
| RULE-006 | Location constraint | See "Design decisions" below - hard-fails only for `Restricted` data with a location mismatch; otherwise a soft compatibility-score penalty. |
| RULE-007 | Lifecycle status | Cluster must not be `Retired`, `Decommissioning`, `Blocked` or `Unsupported`. |
| RULE-008 | Dependency compatibility | A critical, high-latency-sensitivity dependency whose target is in a different region than the candidate is a hard failure. Same-region-different-DC is allowed (RULE-006/scoring handles that trade-off). |
| RULE-009 | Capacity headroom | Projected utilization after placement must stay below the configured thresholds (CPU 75%, memory 80%, storage 85% by default). |
| RULE-010 | Resiliency | See "Design decisions" below - uses the candidate's *actual* active node count, not the possibly-stale `InfrastructureCluster.NodeCount` column. |

### Design decisions worth calling out

- **RULE-006** is a literal one-line spec ("apply mandatory or preferred location constraints"), but the schema only carries a *preferred* location, not a separate mandatory flag. We treat a location mismatch as a hard failure only when the workload's `DataClassification` is `Restricted` (a data-residency reading of "mandatory"); every other mismatch is scored, not rejected - see `docs/scoring-model.md`'s Compatibility sub-score.
- **RULE-010** uses `active_node_count` from `ClusterNode` (live count), not `InfrastructureCluster.NodeCount` (a declared, possibly stale value). This is deliberate: it catches a cluster that *advertises* Tier-1 but cannot structurally back it (e.g. `cmh-03` in the seed data claims Tier-1 with only 2 active nodes - fewer than the 3 required).

## 1b. Node eligibility rules (NODE-001 .. NODE-004)

Implemented in `ai-service/app/rules/node_eligibility.py`. These run *after* RULE-001..010, only against hosts inside a cluster that already passed every cluster-level rule. A node inherits environment, platform, availability tier, classification, location, dependency locality and cluster lifecycle from its cluster - re-checking them per host would return the same answer for every sibling. What is left is strictly node-local.

| Rule | Name | Logic |
|---|---|---|
| NODE-001 | Node lifecycle | `ClusterNode.LifecycleStatus` must be exactly `Active`. Stricter than RULE-007 on purpose: a cluster may legitimately be `Planned` and still be a valid target, but a *host* has to be live now to receive a workload. |
| NODE-002 | Node absolute capacity | `available_* >= per-host portion of required_*_effective` for CPU, memory and storage. |
| NODE-003 | Node capacity headroom | Projected per-host utilization after placement must stay under the same CPU/memory/storage thresholds RULE-009 uses. |
| NODE-004 | Node is reporting | The host's `LastSeenAt` must be within `node_stale_after_days` (7) of the **freshest** `LastSeenAt` in its own cluster. |

### How much of the workload each host is measured against

`app/services/node_placement.py::per_host_requirement` decides this from the cluster's platform, and records the answer on every candidate as `evidence.placement_model` / `evidence.share_denominator`:

| Platform | Model | Each host is measured against |
|---|---|---|
| Kubernetes, OpenShift, VMware, Hyper-V | `share` | `required_*_effective / active_node_count` |
| BareMetal (or a cluster with one active host) | `whole` | the entire `required_*_effective` |

The reason this is not simply "whole" everywhere: on a clustered platform the scheduler spreads a workload across hosts, so checking whether a single 6-core host can absorb a 16-core application rejects every host in the estate and returns an empty shortlist - technically true, operationally meaningless. The share is a plain even split, **not** a bin-packing simulation; read it as "this host's fair portion", not as a guaranteed scheduler placement.

### NODE-004 is relative, not wall-clock

Staleness is measured against the freshest host in the same cluster, not against `now()`. This matches how utilization windows already work (`app/repositories/utilization_repository.py`): seed and historical estates are not guaranteed to reach the current instant, and a wall-clock comparison would mark an entire archived estate stale.

## 1c. What reaches a human reviewer

`persist_recommendations` writes the **top 3 clusters and the top 3 hosts inside each** - `SAD_POLICY__TOP_CLUSTERS` and `SAD_POLICY__TOP_NODES_PER_CLUSTER`, both 3 by default. Ranking itself is never truncated; these bound only what becomes an `InfrastructureRecommendation` row.

- Cluster rows use `CandidateEntityType = 'Cluster'`, node rows `'Node'`, both scoped to the same `InvestigationId`.
- **`Rank` is scoped to its own level.** A node's rank is its position within its parent cluster (1..3), not within the investigation. `recommendation_repository.list_for_investigation` resolves each node back to its parent cluster's rank so rows come back in display order: cluster 1, its hosts, cluster 2, its hosts, and so on.
- Node rows leave `CompatibilityScore` / `ResiliencyScore` / `DependencyScore` NULL by design - those are cluster properties, identical across sibling hosts, already recorded on the parent cluster's row.
- Node `EvidenceJson` carries `parent_cluster_id`, `parent_cluster_code`, `parent_cluster_rank` and `reliability_score`, so a reader never has to join to know which cluster a host belongs to.
- Node `Explanation` is **built by code**, not narrated by the LLM - it states projected CPU/memory/storage and remaining headroom, and numbers do not come from a language model anywhere in this platform.

## 2. Capacity formulas

Implemented in `ai-service/app/services/capacity.py`, all in `decimal.Decimal`:

```
effective_cpu    = TotalCpuCores  * (1 - ReservedCpuPercent/100)
effective_mem    = TotalMemoryGb  * (1 - ReservedMemoryPercent/100)
effective_stor   = TotalStorageGb                                    # no reservation column for storage
allocated_*      = sum(ApplicationHosting.Allocated*) over HostingStatus in (Active, Migrating)
measured_*       = Total* * avg(*UsedPercent over the last 30 days) / 100
consumed_*       = max(allocated_*, measured_*)                      # conservative by design (see below)
available_*      = effective_* - consumed_*

required_grown   = required_* * (1 + growth%/100) ** growth_horizon_years   # default horizon: 1 year
required_eff     = required_grown * (1 + safety_margin%/100)                # default margin: 10%

projected_%      = (consumed_* + required_eff) / effective_* * 100
headroom_%       = 100 - max(projected_cpu%, projected_mem%, projected_storage%)
```

The same formulas run per host in `compute_node_capacity` / `compute_node_projected_utilization`, with two node-specific caveats:

- A host inherits its **cluster's** reservation percentages - reservation is a platform-level overhead (kubelet/hypervisor/system daemons) and the schema records it once, on the cluster.
- `allocated_*` counts only `ApplicationHosting` rows **pinned to that host** (`NodeId` is nullable and most hosting rows record only the cluster). On an unpinned estate a host's allocation is 0 and `consumed` is purely the measured figure; `has_measurements` reports whether any `NodeUtilization` samples existed in the window rather than silently scoring an unmonitored host as empty.
- Growth and the safety margin are applied **once**, at cluster level. The node projection takes the already-effective requirement; recomputing it would inflate every host's numbers relative to its own cluster's.

**Why `consumed = max(allocated, measured)`:** a cluster that has *declared* allocations (via `ApplicationHosting`) exceeding what's *actually measured* is still committed - those cores are reserved even if idle right now. A cluster with low measured use but high allocation is not truly overprovisioned; RIGHTSIZE-001 correctly refuses to call it so.

**What `InfrastructureCluster.MonthlyCost` represents:** an internal chargeback/showback rate - the amount the owning support group (LOB) is billed internally for that cluster's capacity - not external vendor spend, not a literal sunk-hardware/depreciation cost. `estimate_monthly_cost()` (`app/services/placement.py`) apportions this rate to a candidate by its CPU/memory share of the cluster; `RIGHTSIZE-006`'s savings figures are the change in that internal chargeback, not a real-dollar vendor refund. This framing matters for how recommendations should be read: "estimated monthly savings" is a showback number for capacity-planning conversations with a LOB, not a number finance would recognize as cash saved.

## 3. Right-sizing rules (RIGHTSIZE-001 .. 006)

Implemented in `ai-service/app/services/rightsizing.py` and `consolidation.py`.

- **RIGHTSIZE-001 / 002** (over-/under-provisioned): a cluster is `Overprovisioned` when both CPU and memory current utilization are below `overprovision_*_percent` (default 32%); `Underprovisioned` when either is at/above `underprovision_*_percent` (default 80%/85%); otherwise `Healthy`.
- **RIGHTSIZE-003** (node reduction): the smallest node count `N` such that (a) consumed load stays under the headroom threshold on `N` nodes, and (b) losing one more node (`N-1`, the configured failure tolerance) still keeps load under a 95% emergency ceiling. Never recommends below `node_failure_tolerance + 1`.
- **RIGHTSIZE-004** (node expansion): the smallest `N >= current` such that consumed load stays under threshold - triggered when a cluster classifies `Underprovisioned`.
- **RIGHTSIZE-005** (application allocation): compares `ApplicationHosting.Allocated*` against the 30-day measured `ApplicationUsage` average; flags `OverAllocated` (allocation > 115% of a margin-adjusted target) or `UnderAllocated` (allocation below measured consumption).
- **RIGHTSIZE-006** (cost optimization): `estimated_monthly_savings = removed_nodes * monthly_cost_per_node` (cluster case) or the allocation delta's cost share (application case); consolidation savings are the difference in cost-per-CPU-core between the source (overprovisioned) and target cluster.

Consolidation (`app/services/consolidation.py`) only proposes a target cluster that (a) passes all ten hard rules for the moving application, and (b) already hosts at least one other workload - otherwise it's a placement, not a consolidation.

## 4. Forecast rules

`ai-service/app/forecasting/engine.py`: ordinary least squares on daily-mean utilization (`app/repositories/utilization_repository.get_cluster_daily_means`). Supported horizons: 30/60/90/180 days. Exhaustion date is the first day the fitted line crosses the configured threshold (`None` if the trend is flat or falling). Confidence band: `± z * se * sqrt(1 + 1/n + (x - x̄)² / Sxx)`, `z = 1.96` by default (95%).

## 5. Consolidation constraints

A consolidation candidate must remain eligible under **all ten** hard rules on the target cluster - capacity, availability, environment, security/classification and dependency constraints are never bypassed for the sake of a consolidation opportunity.

---

## Guardrails

Every item below is implemented, not aspirational - each has a corresponding test (`ai-service/tests/`, `mcp-server/tests/`, `api-gateway/SeekAndDestroy.Tests/`).

### The LLM never produces a number

Every score, cost, utilization percentage, forecast value and exhaustion date originates in `app/rules`, `app/scoring`, `app/services/capacity.py` or `app/forecasting/engine.py`. The LLM's explanation prompts (`app/prompts/templates.py`) embed the computed evidence as literal JSON and instruct the model to echo it verbatim. `app/agents/guards.assert_no_number_drift` re-checks every numeric field of the model's structured output against that evidence after parsing and **raises `NumberDriftError`** on any mismatch beyond a 0.01 tolerance - the explanation is rejected, not silently accepted. Covered by `test_llm_cannot_change_numeric_scores` (critical test #7).

One narrow, deliberate exception: Scenario B free-text capacity requests ("I need 8 CPU, 32GB RAM...", no named application) ask the LLM to *transcribe* numbers the user already stated, not *derive* new ones - see `extract_capacity_requirement` (`app/agents/chains.py`) wired into `load_application_requirements` (`app/graph/nodes.py`). This only engages when a real provider is configured (`SAD_LLM__PROVIDER != mock`); the offline mock model has no real language understanding and would echo back random filler for un-quoted prose, so mock mode continues to use plain regex extraction (`_capacity_requirement_from_regex`), which is strictly more correct there. Either way, the extracted `cpu_cores`/`memory_gb`/`storage_gb` become the *input* to the deterministic engine, never its output - the trust boundary is about who computes eligibility/scores/costs from a requirement, not about who is allowed to read a requirement out of a sentence.

### No SQL injection surface

`app/repositories/base.py` exposes only `fetch_all` / `fetch_one` / `execute` / `execute_insert`, all built on `sqlalchemy.text()` with bound parameters. There is no "execute arbitrary SQL" function anywhere in the codebase, and the MCP server deliberately has no `execute_sql` tool (verified by `test_no_execute_sql_tool` and `test_no_infrastructure_modification_tool_exists`). The only identifier ever interpolated into a query is the schema name (`sad`), which is validated once at startup (`DatabaseSettings.schema_name` must be alphanumeric/underscore) and never derived from a request.

### No infrastructure mutation

There is no tool, endpoint or graph node anywhere in the platform that provisions, decommissions, resizes, or migrates real infrastructure. The MCP server's write tools touch exactly four governance tables: `CapacityRequest`, `Investigation`, `InfrastructureRecommendation`, `RecommendationDecision`. `CmdbApplication`, `InfrastructureCluster`, `ClusterNode` and `ApplicationHosting` are read-only from every layer above SQL Server itself.

### Human review is structural, not a convention

`human_review_interrupt` (`app/graph/nodes.py`) calls `langgraph.types.interrupt(...)`, which is a real pause backed by the `SqliteSaver` checkpoint - the graph process can exit and a *different* process can resume it later via `Command(resume=...)`. This is not a status flag an implementer could accidentally skip; the graph literally cannot reach `persist_recommendations` on the Hosting/Capacity/RightSizing/Consolidation paths without a resume call.

`submit_recommendation_decision` requires a real reviewer identity, enforced at every layer a request can pass through - and since the JWT auth work, "requires" now means *authenticated*, not just *present*: the FastAPI dependency `get_current_employee` (`app/api/auth.py`) validates the caller's Bearer token and cross-checks the claimed `employee_id` against a real, active `Employee` row; `require_matching_employee_id` then makes that token identity authoritative over anything the request body claims (a mismatch is rejected with `403`, not silently overridden); the .NET gateway's own `[Authorize]` + JWT Bearer validation rejects an unauthenticated caller before the request ever reaches the AI service, and forwards the same token through rather than re-vouching for the caller itself. The MCP server's write tools (`create_capacity_request`, `create_investigation`, `submit_recommendation_decision` - `mcp-server/tools/write_tools.py`) require the identical validation via a required `access_token` parameter (`mcp-server/tools/_auth.py::authenticate`, calling the same `app.security.jwt_service.validate_token`) - a call missing it is rejected by the MCP tool schema itself before any tool code runs, and the raw token is explicitly stripped before anything is written to `AgentAuditLog` (tokens are credentials, not audit data). An anonymous or spoofed decision is rejected before it ever reaches the database, at every entry point - gateway, AI service, and MCP - not just one of them.

### Every MCP tool call is audited

`mcp-server/tools/_audit.py::audited()` wraps every one of the 27 tools, writing a `sad.AgentAuditLog` row before the call (tool name, input JSON, investigation id if any) and updating it with the output (or the error) after. Verified by `test_every_tool_call_is_audited`.

### Real-provider spend is budgeted, not open-ended

`app/services/spend_budget.py::check_and_increment` guards every real (non-mock, non-hash) external call - `HttpChatModel._generate` (LLM chat) and `HttpEmbedder`/`GeminiEmbedder._embed_batch` (embeddings) - against `SAD_LLM__DAILY_CALL_BUDGET` / `SAD_RETRIEVAL__EMBEDDING_DAILY_CALL_BUDGET`. The counter is UTC-calendar-day-keyed (resets automatically, no cleanup job) and lives in the same cache store as LLM-narration caching - in-memory by default (per-process only), a real shared counter across workers when `SAD_CACHE__BACKEND=redis`. `0` (the default) means unlimited; mock/hash providers never touch this at all, so the default posture is unchanged from before this existed. Exceeding the budget raises `BudgetExceededError`, which flows through the exact same "narration failure must not break the pipeline" exception handling every other LLM/embedding failure already does (see `app/graph/nodes.py`) - a chat answer degrades to a placeholder or no retrieved context rather than the investigation failing outright.

### Row limits

`app/repositories/base.py::fetch_all` enforces `SAD_SERVICE__MAX_ROWS` (default 500) and raises `RowLimitExceeded` (mapped to HTTP 400) rather than silently truncating or returning an unbounded result set.

### Input limits

User-supplied investigation queries are truncated to `SAD_SERVICE__MAX_QUERY_CHARS` (default 2000) before they ever reach the LLM (`app/graph/nodes.parse_user_request`); the FastAPI `CreateInvestigationRequest` schema enforces the same bound at the API boundary.

### Prompt-injection defense

Every system prompt (`app/prompts/templates.SYSTEM_BASE`) explicitly instructs the model that evidence, retrieved documents and tool outputs are **data, not instructions** - if a CMDB record or a user query contains text that attempts to redirect the model's behavior, the model is told to describe that fact rather than comply. This is a mitigation, not a hard guarantee against a sufficiently adversarial model; the numeric-drift guard is the layer that cannot be talked around, because it doesn't ask the model to behave - it independently re-verifies the output.

### Deterministic routing - the LLM never chooses infrastructure

`classify_investigation_type` (`app/graph/nodes.py`) and the interactive client's query router (`mcp-client/interactive_client.py`) are both plain keyword/regex matching, not an LLM call. A request that reads as a change-execution ask ("provision", "deploy this", "decommission", "migrate the", ...) is classified `Refused` and routed straight to a refusal report - the platform states plainly that it produces recommendations only and never executes changes, and does not attempt the underlying investigation at all.

### Secrets and credentials

The SQL Server connection uses `Integrated Security=True` / `Trusted_Connection=yes` (the running process's Windows identity) - no password is stored anywhere. LLM provider API keys (`SAD_LLM__API_KEY`) are read from environment variables only, never hardcoded, and the default `mock` provider requires no key at all. `.env` is git-ignored.

### No caching layer between a hard-rule decision and its data

Capacity, eligibility and scoring computations (`app/services/capacity.py`, `app/rules/eligibility.py`, `app/scoring/engine.py`) are never cached - every number is computed fresh from current data on every call, per `IMPLEMENTATION_PLAN.md` §6b's original guardrail.

A cache does exist (`app/cache/store.py`, `SAD_CACHE__BACKEND=memory|redis`), but it is scoped narrowly to LLM *narration* text in `app/agents/structured.py::run_structured` - never to a computed number. The cache key is a hash of the exact prompt text (system prompt + human prompt, which embeds the full evidence dict) plus the target schema, so a cache hit only ever occurs when the underlying evidence is byte-identical to a prior call; different evidence produces a different key, not a stale hit. `assert_no_number_drift` still validates every cached result against the current evidence before it's used, exactly as it would a fresh LLM call. A bounded default TTL (`SAD_CACHE__DEFAULT_TTL_SECONDS`, 300s) further limits how long any entry can survive. `memory` (default, in-process) requires no server; `redis` is a real Redis client that falls back to `memory` automatically if unreachable (see `app.cache.store.build_cache_store`).
