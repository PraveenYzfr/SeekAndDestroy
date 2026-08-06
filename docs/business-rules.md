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

`human_review_interrupt` (`app/graph/nodes.py`) calls `langgraph.types.interrupt(...)`, which is a real pause backed by the `SqliteSaver` checkpoint - the graph process can exit and a *different* process can resume it later via `Command(resume=...)`. This is not a status flag an implementer could accidentally skip; the graph literally cannot reach `persist_recommendations` on the Hosting/Capacity/RightSizing/Consolidation paths without a resume call. `submit_recommendation_decision` additionally requires a non-empty, positive `reviewer_employee_id` at three independent layers: the MCP tool, the FastAPI Pydantic schema (`Field(gt=0)`), and the .NET gateway's FluentValidation - so an anonymous decision is rejected before it ever reaches the database.

### Every MCP tool call is audited

`mcp-server/tools/_audit.py::audited()` wraps every one of the 27 tools, writing a `sad.AgentAuditLog` row before the call (tool name, input JSON, investigation id if any) and updating it with the output (or the error) after. Verified by `test_every_tool_call_is_audited`.

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
