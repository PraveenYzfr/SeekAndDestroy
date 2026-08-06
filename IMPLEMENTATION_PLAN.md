# SeekAndDestroy — Implementation Plan (handoff document)

This is the authoritative build plan. It is written so an implementer starting
with **no prior conversation context** can execute it end to end.

---

## 0. Verified environment (probed on this machine — do not re-assume)

| Item | Reality |
|---|---|
| Python | **3.14.0** at `D:\Programs\Python\Python314\python.exe`. No 3.12 present. |
| venv | Already created at `D:\Praveen\Projects\SeekandDestroy\.venv` with all packages installed. |
| SQL Server | **SQL Server 2025** on `LAPTOP-R6U8H616`, `PraveenDB` exists and is **empty** (0 tables). |
| sqlcmd | Present: `C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\170\Tools\Binn\SQLCMD.EXE` |
| .NET | SDK 6.0.421 + **10.0.302**; runtimes include `Microsoft.NETCore.App 8.0.29` → `net8.0` targeting works. |
| Docker | **NOT installed.** Qdrant cannot run here. |
| Node/npm | **NOT installed.** The React UI can be authored but not built or run here. |
| git | Present, but the project folder is **not** a git repo. |

### Critical API facts confirmed by inspection (not from memory)

- `mcp==2.0.0` **removed `FastMCP`**. `mcp.server.fastmcp` does not exist.
  The server class is **`from mcp.server import MCPServer`**, with the same
  decorator surface: `@server.tool(...)`, `@server.resource("uri://{param}")`,
  `@server.prompt(...)`, `server.run(transport="stdio")`, `run_stdio_async()`.
- Client side: **`from mcp import Client`**. `Client(server_instance)` gives an
  in-process transport (best for tests + the interactive client);
  `Client(url_string)` gives streamable HTTP. For stdio subprocesses use
  `mcp.client.stdio.stdio_client(StdioServerParameters(...))` + `ClientSession`.
  There is **no** `StdioTransport` class.
- `langchain-core==1.5.3`: `BaseChatModel.__abstractmethods__ == {"_generate", "_llm_type"}`.
  `_generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult`.
  `langchain_core.embeddings.Embeddings` requires `embed_query` + `embed_documents`.
- `langgraph==1.2.10`: `from langgraph.graph import StateGraph, START, END`,
  `from langgraph.types import interrupt, Command`,
  `from langgraph.checkpoint.sqlite import SqliteSaver`.

---

## 1. Architecture

```
React + TypeScript UI (8 screens)
        │ HTTP
ASP.NET Core 8 Gateway (Api / Application / Domain / Infrastructure / Tests)
   ├─ auth, DTO mapping, audit, FluentValidation, Swagger, health checks
   ├─ Dapper + Microsoft.Data.SqlClient → read-only CMDB queries
   └─ typed HttpClient → Python AI service
        │ HTTP
FastAPI AI Service (Python)
   ├─ api/          REST endpoints, ProblemDetails errors
   ├─ graph/        LangGraph InfrastructureRecommendationGraph (18 nodes, SqliteSaver)
   ├─ agents/       LangChain chains: intent parse, requirement extraction, explain, report
   ├─ rules/        RULE-001..010 deterministic eligibility
   ├─ scoring/      weighted candidate scoring (Decimal, deterministic)
   ├─ forecasting/  OLS trend forecaster
   ├─ services/     capacity, right-sizing, consolidation, placement, investigation
   ├─ repositories/ SQLAlchemy Core + pyodbc, parameterized only
   ├─ retrieval/    doc builders, embedder, vector store (Qdrant | in-memory)
   └─ models/       Pydantic contracts + enums
        │ in-process / stdio
MCP Server (25 tools, 7 resources) — wraps the SAME service layer, no execute_sql
        │
SQL Server PraveenDB schema `sad`          Qdrant (optional, in-memory fallback)
```

**Trust boundary — the single most important design rule.** The LLM never
produces a number. Every figure in a recommendation originates in `rules/`,
`scoring/`, `forecasting/` or `services/capacity`. The explanation chain
receives a frozen evidence dict; a post-check rejects any explanation whose
quoted score/utilisation differs from the computed value.

---

## 2. Business assumptions (implement exactly these)

1. All tables live in schema **`sad`** inside `PraveenDB`. `reset.sql` drops only
   `sad` objects.
2. Availability tiers ordered `Tier-1 > Tier-2 > Tier-3`. Candidate qualifies when
   `rank(candidate) <= rank(required)` where rank Tier-1=1, Tier-2=2, Tier-3=3.
3. Data classification ordered `Public(0) < Internal(1) < Confidential(2) < Restricted(3)`.
   Cluster qualifies when `level(cluster.ComplianceClassification) >= level(app.DataClassification)`.
4. Environment: `Production` apps only on `Production` clusters. Non-prod apps must
   match the cluster environment **exactly**.
5. **Consumed capacity = max(allocated, measured)** over a 30-day window.
   Conservative on purpose: prevents both over-commit and phantom free capacity.
6. Growth applied over a 1-year horizon: `required × (1 + growth%/100)`.
7. Then a **10 % safety margin**: `required_effective = required_with_growth × 1.10`.
8. Headroom thresholds: CPU < 75 %, memory < 80 %, storage < 85 % (configurable).
9. Resiliency: Tier-1 apps need cluster `AvailabilityTier = Tier-1` **and** ≥ 3 nodes;
   Tier-2 apps need ≥ 2 nodes.
10. Dependency locality: same data centre = best; same region = acceptable;
    cross-region = penalty; cross-region **and** `LatencySensitivity=High` **and**
    `IsCritical=1` = **hard fail** (RULE-008).
11. Node reduction only if remaining nodes hold all workloads under threshold with
    N-1 failure tolerance preserved.
12. Cost = `cluster.MonthlyCost × max(cpu_share, memory_share)` of the request.
13. Everything is advisory. No provisioning / decommissioning / migration is ever
    executed. Approval is human and records reviewer identity.

---

## 3. Technology decisions

| Concern | Decision | Reason |
|---|---|---|
| Python | 3.14.0 | only interpreter present; all deps resolved |
| LLM default | `MockChatModel`, deterministic, seeded by prompt hash | whole platform runs with no API key |
| Other providers | OpenAI-compatible / Azure OpenAI / Ollama via raw `httpx` behind `BaseChatModel` | no vendor SDK, no extra deps |
| Embeddings | `DeterministicHashEmbedder` (384-d char-ngram hashing, L2-normalised) default; `sentence-transformers` optional | torch has no py3.14 wheel here; hash embedder is offline + reproducible so tests are stable |
| Vector store | `QdrantVectorStore` when reachable else `InMemoryVectorStore`, one interface | Docker unavailable; compose file still shipped |
| SQL | SQLAlchemy Core + pyodbc, `Trusted_Connection=yes`, parameterized only | no secrets at rest |
| Checkpointer | LangGraph `SqliteSaver` at `.state/checkpoints.db` | pause/resume across processes |
| .NET | net8.0, Dapper, Microsoft.Data.SqlClient, FluentValidation, Serilog, Swashbuckle | as specified |
| UI | React 18 + TypeScript + Vite | authored only — npm absent on this box |

### Pinned versions (already installed in `.venv`)

```
fastapi 0.141.1     uvicorn 0.52.1        pydantic 2.13.4    pydantic-settings 2.14.2
sqlalchemy 2.0.51   pyodbc 5.3.0          httpx 0.28.1       structlog 26.1.0
numpy 2.5.1         python-dotenv 1.2.2   python-multipart 0.0.32
langchain 1.3.14    langchain-core 1.5.3
langgraph 1.2.10    langgraph-checkpoint 4.1.1   langgraph-checkpoint-sqlite 3.1.1
mcp 2.0.0           qdrant-client 1.18.0
pytest 9.1.1        pytest-asyncio 1.4.0
```
.NET: `Dapper 2.1.66`, `Microsoft.Data.SqlClient 5.2.2`, `FluentValidation 11.11.0`,
`FluentValidation.DependencyInjectionExtensions 11.11.0`, `Serilog.AspNetCore 8.0.3`,
`Swashbuckle.AspNetCore 6.9.0`, `xunit 2.9.2`, `xunit.runner.visualstudio 2.8.2`,
`Microsoft.NET.Test.Sdk 17.12.0`, `Microsoft.AspNetCore.Mvc.Testing 8.0.11`.

---

## 4. Database design

16 tables in `sad`, identity PKs, FKs on every relationship, `CreatedAt`/`UpdatedAt`
audit columns with `SYSUTCDATETIME()` defaults.

`Employee`, `SupportGroup`, `CmdbApplication`, `InfrastructureCluster`, `ClusterNode`,
`ApplicationHosting`, `ClusterUtilization`, `NodeUtilization`, `ApplicationUsage`,
`ApplicationDependency`, `Incident`, `CapacityRequest`, `InfrastructureRecommendation`,
`RecommendationDecision`, `Investigation`, `AgentAuditLog`.

Field lists are exactly as given in the original specification, §5.

**Unique constraints:** `ApplicationCode`, `ClusterCode`, `HostName`, `EmployeeNumber`,
`SupportGroup.GroupName`, `(ClusterId, MetricDateTime)`, `(NodeId, MetricDateTime)`,
`(ApplicationId, UsageDateTime)`, `(SourceApplicationId, TargetApplicationId, TargetClusterId, DependencyType)`.

**CHECK constraints** on every enumeration listed in
`ai-service/app/models/enums.py` (already written — mirror it), plus non-negative
checks on all capacity/cost columns, percentages in `[0,100]`, scores in `[0,100]`,
`ClosedAt >= OpenedAt`, and `ApplicationDependency` requiring exactly one of
`TargetApplicationId` / `TargetClusterId`.

**Indexes:** `IX_ClusterUtilization_Cluster_Date`, `IX_NodeUtilization_Node_Date`,
`IX_ApplicationUsage_App_Date`, `IX_ApplicationHosting_Cluster`,
`IX_ApplicationHosting_Application`, `IX_ClusterNode_Cluster`,
`IX_Incident_Cluster_OpenedAt`, `IX_Incident_Application_OpenedAt`,
`IX_Recommendation_Investigation`, `IX_AgentAuditLog_Investigation`.

---

## 5. Scoring model

Hard rules run first. A failure ⇒ `EligibilityStatus='Rejected'` with the rule id
and human-readable reason. Rejected candidates are **never scored**, only explained.

Eligible candidates get seven 0–100 sub-scores combined as:

```
Overall = 0.30·Capacity + 0.15·Compatibility + 0.15·Resiliency
        + 0.15·CostEfficiency + 0.10·DependencyLocality
        + 0.10·HistoricalPerformance + 0.05·(100 − OperationalRisk)
```

- Computed with `decimal.Decimal`, `ROUND_HALF_UP`, 2 dp ⇒ byte-identical across runs.
- Ranking: score **desc**, then `EstimatedMonthlyCost` **asc**, then `ClusterCode`
  **asc** — a total order, so no ties and no nondeterminism.
- Persist all seven sub-scores + projected CPU/mem/storage utilisation + headroom
  + cost + `EvidenceJson`.

Sub-score definitions:
- **Capacity** = min headroom across the three resources, normalised
  `clamp(headroom_pct / target_headroom_pct × 100, 0, 100)`.
- **Compatibility** = 100 exact platform+OS match, 82 compatible-but-not-exact,
  minus 10 if location is a *preferred* (not mandatory) mismatch.
- **Resiliency** = f(node count vs required, cluster tier vs required tier, N-1 survivability).
- **CostEfficiency** = normalised inverse of `estimated_monthly_cost` across the
  eligible set (cheapest = 100).
- **DependencyLocality** = 100 same DC, 75 same region, 40 cross-region, 0 unreachable.
- **HistoricalPerformance** = 100 − weighted incident rate over the last 90 days
  (`Sev1=10, Sev2=5, Sev3=2, Sev4=1`), clamped.
- **OperationalRisk** (higher = worse, inverted in the formula) = utilisation
  volatility + forecast slope + lifecycle `Deprecated` penalty + open Sev1/Sev2 count.

---

## 6. Build order — do these in sequence, run tests after each

Each phase must leave the tree runnable. No placeholders, no pseudocode, no `TODO`.

### Phase 1 — structure + config ✅ **ALREADY DONE**
Files already written and working:
- `ai-service/requirements.txt`, `requirements-optional.txt`, `.env.example`
- `ai-service/app/config/settings.py` — **single source of truth** for the SQL
  connection (`DatabaseSettings.odbc_connection_string` / `.sqlalchemy_url` /
  `.dotnet_connection_string`). Nested settings groups: `db`, `llm`, `retrieval`,
  `policy`, `scoring`, `forecast`, `service`. Env prefix `SAD_<GROUP>__<FIELD>`.
- `ai-service/app/config/__init__.py`
- `ai-service/app/models/enums.py` — controlled vocabularies **plus** the four
  rule helper functions `environment_compatible`, `platform_compatible`,
  `availability_satisfies`, `classification_permits`, `os_is_compatible`.
- Full directory tree.

Remaining in this phase: `.gitignore`, `__init__.py` for every package dir.

### Phase 2 — `database/schema.sql`, `reset.sql`
Apply with `sqlcmd -S LAPTOP-R6U8H616 -d PraveenDB -E -C -i database\schema.sql`.
Verify by querying `sys.tables` / `sys.check_constraints` counts.

### Phase 3 — deterministic seed
Write `scripts/generate_seed.py` that emits `database/seed.sql` using a **fixed
`random.Random(20240101)` seed and a fixed anchor date** (`ANCHOR_DATE =
date(2026, 8, 4)`), so regeneration is byte-identical. Never use `datetime.now()`.

Volumes: 40 apps, 15 clusters, 75 nodes, 20 employees, 8 support groups,
180 days × 15 clusters of `ClusterUtilization`, 180 days × 75 nodes of
`NodeUtilization`, 180 days × 40 apps of `ApplicationUsage`, hosting rows,
dependencies, incidents, capacity requests.

Engineered scenarios that the tests assert on (**tag each cluster in a comment**):
3 overprovisioned, 2 nearing CPU, 2 nearing memory, 2 high-cost/low-util,
3 suitable for new workloads, 2 insufficient resiliency, 2 compliance mismatch,
5 apps on poor-fit infra, 4 apps consolidatable, 3 apps needing expansion,
4 apps with strong alternatives, 3 clusters forecast to exhaust within 90 days.

`NodeUtilization` volume is ~13.5k rows and `ClusterUtilization` ~2.7k — emit
`INSERT ... VALUES` in batches of 1000 rows.

### Phase 4 — repositories
`app/repositories/`: `base.py` (engine factory, `fetch_all/fetch_one/execute`,
row limit from `settings.service.max_rows`, SQL duration logging),
`application_repository.py`, `cluster_repository.py`, `node_repository.py`,
`hosting_repository.py`, `utilization_repository.py`, `usage_repository.py`,
`dependency_repository.py`, `incident_repository.py`, `capacity_request_repository.py`,
`recommendation_repository.py`, `investigation_repository.py`, `audit_repository.py`.
**Parameterized statements only** — `sqlalchemy.text()` with bind params. The only
identifier interpolated is the validated schema name.

### Phase 5 — capacity engine (`app/services/capacity.py`)
```
effective_cpu   = TotalCpuCores  × (1 − ReservedCpuPercent/100)
effective_mem   = TotalMemoryGb  × (1 − ReservedMemoryPercent/100)
effective_stor  = TotalStorageGb                       # no storage reservation column
allocated_*     = Σ ApplicationHosting.Allocated*      # HostingStatus in (Active, Migrating)
measured_*      = Total* × avg(*UsedPercent, last 30d)/100
consumed_*      = max(allocated_*, measured_*)
available_*     = effective_* − consumed_*
required_grown  = required_* × (1 + growth/100) ** horizon_years
required_eff    = required_grown × (1 + safety_margin/100)
projected_util  = (consumed_* + required_eff) / effective_* × 100
headroom_pct    = 100 − max(projected_cpu, projected_mem, projected_stor)
```

### Phase 6 — `app/rules/eligibility.py`
RULE-001 … RULE-010 as individual functions returning
`RuleResult(rule_id, passed, reason, evidence)`. `evaluate_all()` returns an
ordered list; first failure marks the candidate rejected but **all** rules still
run so the explanation can list every failure.

### Phase 7 — `app/scoring/`
`weights.py`, `subscores.py`, `engine.py`. Decimal arithmetic, `ROUND_HALF_UP`.
`rank_candidates()` implements the total order from §5.

### Phase 8 — `app/services/rightsizing.py` + `consolidation.py`
RIGHTSIZE-001…006 and the consolidation constraint checks. Savings =
`monthly_delta` and `monthly_delta × 12`.

### Phase 9 — `app/forecasting/`
OLS on daily-mean utilisation: slope, intercept, R², residual standard error.
Predict at 30/60/90/180 d. Exhaustion date = first day the fitted line crosses the
threshold (`None` if slope ≤ 0). Confidence band = `± z × se × sqrt(1 + 1/n + (x−x̄)²/Sxx)`.
Recommended action derived from predicted value vs threshold.

### Phase 10 — FastAPI (`app/api/`)
All endpoints from spec §16. `ProblemDetails` exception handlers
(RFC 7807 shape: `type/title/status/detail/instance/errors`). Correlation-ID
middleware. `/api/health` (liveness) and `/api/ready` (DB + vector store probe).

### Phase 11 — retrieval (`app/retrieval/`)
`embedder.py` (`DeterministicHashEmbedder`, `SentenceTransformerEmbedder`),
`vector_store.py` (`VectorStore` protocol, `InMemoryVectorStore` with JSON
persistence + cosine + metadata filters, `QdrantVectorStore`),
`documents.py` (builders for all 8 entity kinds — cluster doc must read like the
worked example in spec §12), `indexer.py` (index/update/delete/rebuild).

### Phase 12 — LangChain (`app/agents/`, `app/prompts/`)
`llm_factory.py` (mock / openai / azure-openai / ollama behind `BaseChatModel`),
`mock_llm.py` (deterministic: SHA-256 of the prompt selects from templated
structured responses; must satisfy every Pydantic parser in the platform),
chains for intent parsing, requirement extraction, candidate explanation,
right-sizing explanation, forecast explanation, trade-off summary, grounded Q&A,
final report. Pydantic output models exactly as spec §11.
**Guard:** `app/agents/guards.py::assert_no_number_drift(explanation, evidence)`.

### Phase 13 — MCP server (`mcp-server/`)
`server.py` builds `MCPServer("seek-and-destroy")`; tool modules under `tools/`.
All 25 tools and 7 resources from spec §14. Pydantic-validated inputs, row limits,
every invocation written to `sad.AgentAuditLog`. **No `execute_sql` tool. No
mutation tool that touches infrastructure.** `submit_recommendation_decision`
requires a non-empty `reviewer` argument.

### Phase 14 — MCP client (`mcp-client/`)
`client.py` (programmatic + `langchain_tools()` adapter returning
`StructuredTool` list), `interactive_client.py` (REPL printing interpreted
requirements, tools invoked, eligible/rejected candidates, capacity maths, scores,
projected utilisation, forecast, cost, risks, AI explanation, required human
action). Ship the 10 demo queries from spec §15.

### Phase 15 — LangGraph (`app/graph/`)
`state.py` (the 17 state fields), `nodes.py` (18 nodes), `router.py` (conditional
edges), `graph.py` (compile with `SqliteSaver`). `human_review_interrupt` uses
`langgraph.types.interrupt`; resume via `Command(resume=...)`.
Change-execution intent routes to a refusal node that emits a recommendation only.

### Phase 16 — persistence + human review
Persist recommendations and decisions; wire `/api/investigations/{id}/resume`.

### Phase 17 — .NET gateway
`dotnet new sln`; 5 projects targeting `net8.0`. Domain (entities/enums),
Application (interfaces, DTOs, validators), Infrastructure (Dapper repos, typed
`HttpClient` to the AI service), Api (controllers, Swagger, Serilog, health checks),
Tests (xUnit). Connection string in `appsettings.json` only.

### Phase 18 — React UI
Vite + TS. 8 screens from spec §18. `src/api/client.ts` typed against the gateway.
State that npm is unavailable locally — provide `npm install && npm run dev` docs.

### Phase 19 — tests
`ai-service/tests/` with the **13 critical tests** from spec §21 named explicitly,
plus schema, seed-determinism, repository, capacity, rules, scoring, right-sizing,
consolidation, forecasting, MCP tool, graph node, routing, pause/resume, FastAPI
endpoint, mock-LLM and audit tests. `mcp-server/tests/`. `SeekAndDestroy.Tests/`.

### Phase 20 — docs
`README.md`, `docs/architecture.md`, `business-rules.md`, `scoring-model.md`,
`api-contracts.md`, `setup.md`, `demo-scenarios.md`.

### Phase 21 — docker + scripts
`docker/docker-compose.yml` (qdrant, ai-service, gateway, ui),
`scripts/init-db.ps1`, `run-ai-service.ps1`, `run-mcp-server.ps1`, `run-tests.ps1`.

---

## 6b. Deferred / optional enhancements (not in scope now)

- **Redis cache**: not part of the original specification and not needed at
  this data scale (15 clusters, 40 apps - single SQL Server queries are
  already sub-50ms). Revisit only in a final "hardening" pass if profiling
  under real load shows the FastAPI layer re-computing the same
  cluster-capacity/forecast snapshots on every request; if added, cache
  `capacity.compute_cluster_capacity`, `forecasting.engine.forecast_cluster`
  and the Qdrant retrieval results keyed by `(entity_id, data_version)` with a
  short TTL (seconds, not minutes) so recommendations never serve stale
  capacity numbers. Do not cache anything that feeds a hard-rule decision
  without an explicit invalidation story.

---

## 7. Standing rules for the implementer

1. **Never let the LLM emit a number.** Numbers come from the deterministic
   engines; the LLM only narrates a frozen evidence object.
2. **No `execute_sql`-style tool**, ever. Parameterized queries only.
3. **No placeholder methods, no pseudocode.** Every phase ends runnable.
4. **Determinism is a test requirement** — fixed RNG seed, fixed anchor date,
   `Decimal` + `ROUND_HALF_UP`, total-order ranking.
5. Run the suite after every phase and fix failures before moving on:
   ```
   .venv\Scripts\python.exe -m pytest ai-service/tests mcp-server/tests -q
   ```
6. Docker and npm are absent here — say so plainly rather than claiming the UI or
   Qdrant were verified.
