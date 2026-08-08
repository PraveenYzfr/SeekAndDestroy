# SeekAndDestroy — Architecture

## 1. Component diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│  React + TypeScript UI (Vite)                                        │
│  Chat (primary interface) + 8 structured screens: Dashboard,          │
│  Hosting Recommendation, Comparison, Right-Sizing, Placement,         │
│  Forecast, Investigation Detail, Approval                            │
└───────────────────────────────┬───────────────────────────────────────┘
                                 │ HTTP (JSON)
┌───────────────────────────────▼───────────────────────────────────────┐
│  ASP.NET Core 8 Gateway (api-gateway/)                                │
│  Domain → Application → Infrastructure → Api (clean architecture)     │
│  - CmdbController: read-only Dapper queries straight to SQL Server    │
│  - RecommendationsController / InvestigationsController: typed        │
│    HttpClient pass-through to the AI service, FluentValidation on     │
│    every request, RFC 7807 error propagation, Serilog, health checks  │
└───────────┬─────────────────────────────────────────────┬─────────────┘
            │ Dapper (read-only)                            │ HTTP (JSON)
┌───────────▼─────────────┐                    ┌────────────▼────────────────────────────────┐
│  SQL Server              │                    │  FastAPI AI Service (ai-service/)             │
│  PraveenDB, schema `sad` │◄───SQLAlchemy──────┤  api/        REST endpoints, ProblemDetails    │
│  17 tables, full audit   │      Core +        │  graph/      LangGraph (19 nodes)              │
│  trail (AgentAuditLog)   │      pyodbc,       │  agents/     LangChain chains + multi-LLM      │
└───────────────────────────┘  parameterized     │              fallback (explain + extract)      │
            ▲                   only              │  rules/      RULE-001..010 (deterministic)     │
            │                                    │  scoring/    weighted candidate scoring        │
            │ same repositories,                 │  forecasting/ OLS trend forecaster              │
            │ same deterministic engines         │  services/   capacity/right-sizing/consolidation│
┌───────────┴─────────────────────────┐          │  repositories/ SQLAlchemy Core, parameterized   │
│  MCP Server (mcp-server/)             │          │  retrieval/  embedder + vector store            │
│  27 tools, 7 resources                │◄─────────┤  cache/      LLM-narration cache (memory|redis) │
│  no execute_sql, no infra-mutation    │  in-process │
│  every call audited                   │  or stdio,└────────────┬────────────────────────────────────┘
└───────────┬───────────────────────────┘  shares app/*          │
            │ MCP protocol (stdio / in-process)                  │
┌───────────▼───────────────────────────┐          ┌─────────────┴────────────┐  ┌──────────────────┐
│  MCP Client (mcp-client/)              │          │  Qdrant (optional)         │  │  Redis (optional)  │
│  interactive_client.py (10 demo        │          │  or in-memory vector store  │  │  or in-memory cache │
│  queries), LangChain tool adapter      │          │  (default - no Docker req'd)│  │  (default - no      │
└─────────────────────────────────────────┘          └──────────────────────────┘  │  Docker req'd)      │
                                                                                     └──────────────────────┘
```

Every "optional real backend" above (LLM provider, vector store, cache) is a single config value away from its in-process/offline default - see `.env.example` in each service directory. None require Docker or external credentials to run the platform end to end.

## 2. End-to-end hosting recommendation flow (Scenario A)

1. Engineer asks (UI, MCP client, or API): *"Find the best infrastructure candidates for hosting APP-PAYMENTS."*
2. **LangGraph** `parse_user_request` classifies the request deterministically (keyword/regex routing - never an LLM decision) as `Hosting`.
3. `load_application_requirements` resolves `APP-PAYMENTS` from CMDB and builds a `HostingRequirement`, including resolved dependency locations (for RULE-008).
4. `create_investigation_plan` persists an `Investigation` row.
5. `identify_candidate_infrastructure` lists all non-retired clusters.
6. `apply_hard_eligibility_rules` runs RULE-001..010 against every candidate (`app/rules/eligibility.py`), splitting eligible vs rejected.
7. `calculate_current_capacity` / `calculate_projected_utilization` compute the exact `ClusterCapacitySnapshot` / `ProjectedUtilization` (`app/services/capacity.py`) for every eligible candidate.
8. `run_capacity_forecast` runs the OLS forecaster (`app/forecasting/engine.py`) on the top candidates, feeding the operational-risk sub-score.
9. `analyze_dependencies` / `calculate_candidate_scores` / `rank_candidates` produce the seven sub-scores and the final weighted, totally-ordered ranking (`app/scoring/`).
9b. `select_candidate_nodes` drills the leading clusters down to individual hosts (`app/services/node_placement.py`): NODE-001..004 plus a four-term node score, so the answer is *"cluster nyc-p006, host nyc-p006-NODE-15"* rather than stopping at the cluster boundary. Bounded to the top 3 clusters × top 3 hosts (`SAD_POLICY__TOP_CLUSTERS` / `TOP_NODES_PER_CLUSTER`) - a failure here degrades to cluster-only results rather than losing the investigation.
10. `retrieve_related_context` pulls grounding documents from the vector store.
11. `generate_recommendation_explanations` calls the LLM (`app/agents/chains.explain_candidate`) to narrate the **already-computed** top candidates. `app/agents/guards.assert_no_number_drift` rejects any explanation whose numbers don't match the evidence.
12. `assess_risk_and_confidence` sets `human_review_required = True`.
13. `human_review_interrupt` calls `langgraph.types.interrupt(...)`, checkpointing to SQLite and pausing execution. The API returns `status: "AwaitingReview"`.
14. An engineer reviews the ranked candidates (UI's Recommendation Approval screen, or `submit_recommendation_decision`) and resumes the graph with `Command(resume=...)`.
15. `generate_final_report` → `persist_recommendations` → `complete_investigation` write the final `InfrastructureRecommendation` and `RecommendationDecision` rows - one `'Cluster'` row per shortlisted cluster, plus a `'Node'` row per recommended host inside it.

Nothing in steps 1-10 or 13-15 is decided by the LLM. Step 11 is the *only* LLM involvement, and its output is verified against the same evidence before it is trusted.

## 3. LangChain responsibilities

- Prompt templates (`app/prompts/templates.py`) and structured-output parsing (`app/agents/structured.py`, cached per exact prompt+schema - see `app/cache/`) shared by every chain.
- Natural-language → structured extraction: `parse_investigation_plan`, `extract_capacity_requirement` (wired into `load_application_requirements` for Scenario B free-text capacity requests - only when a real provider is configured; the offline mock model falls back to regex, since it has no real language understanding). `extract_hosting_requirement` exists as the same pattern for a hosting-shaped ask but is not currently reachable from graph routing.
- Narration only: `explain_candidate`, `explain_cluster_right_sizing`, `explain_application_right_sizing`, `explain_forecast`, `summarize_tradeoffs`, `answer_grounded_question`, `generate_final_report`.
- LLM provider abstraction (`app/agents/llm_factory.py`): `mock` (default, fully offline and deterministic), `openai`, `azure-openai`, `ollama` - all via one `HttpChatModel` speaking the OpenAI chat-completions wire format, no vendor SDK. Multi-LLM: `SAD_LLM__FALLBACK_PROVIDERS` chains an ordered list of backup providers behind the primary (`FallbackChatModel`) - on any exception the next provider in the list is tried, so one API outage doesn't take narration offline.

## 4. LangGraph responsibilities

`app/graph/graph.py` compiles `InfrastructureRecommendationGraph`: 19 named nodes (`app/graph/nodes.py` - the 18 from the original specification plus `select_candidate_nodes`, added when recommendations were extended from clusters to individual hosts), two conditional-edge routers (`app/graph/router.py`), 17 state fields (`app/graph/state.py`), and a `SqliteSaver` checkpointer so a run can pause at `human_review_interrupt` and resume in a different process. Routing (`classify_investigation_type`) is deterministic keyword matching, not an LLM call - see [business-rules.md](business-rules.md#guardrails).

## 5. MCP responsibilities

`mcp-server/server.py` exposes the exact same `app/services` engines used by the FastAPI service and the LangGraph nodes as 27 typed, Pydantic-validated tools and 7 resources, with every invocation audited to `sad.AgentAuditLog`. There is no `execute_sql` tool and no tool that provisions, decommissions or migrates infrastructure - the only write tools touch governance tables (`CapacityRequest`, `Investigation`, `InfrastructureRecommendation`, `RecommendationDecision`), and the three that carry an identity claim require the same JWT (`access_token`) validated everywhere else in the platform (`mcp-server/tools/_auth.py`, sharing `app.security.jwt_service` with the FastAPI and .NET layers) - see [business-rules.md § Human review is structural](business-rules.md#human-review-is-structural-not-a-convention).

## 6. SQL Server responsibilities

Schema `sad` inside `PraveenDB` (see `database/schema.sql`) is the single source of truth for CMDB, capacity, hosting, utilization, dependency, incident and recommendation data - 17 tables, including the `DataCenter → Neighborhood → InfrastructureCluster` hierarchy (256 clusters across 64 neighborhoods in 8 data centers). All access - from the Python repositories, the MCP tools, and the .NET gateway's Dapper queries - is parameterized; the schema name itself is validated once (`app.config.DatabaseSettings`) and never interpolated from request input.

## 7. Qdrant responsibilities

`app/retrieval/` builds natural-language documents for every entity kind (application, cluster, node, hosting, incident, dependency, standard, recommendation) and indexes them for cosine-similarity retrieval. `QdrantVectorStore` is used when `SAD_RETRIEVAL__BACKEND=qdrant` and Qdrant is reachable; otherwise `InMemoryVectorStore` (the default) provides the identical interface with optional JSON-file persistence, so the platform never requires Docker to run.

**Embedding provider** (`app/retrieval/embedder.py`, `SAD_RETRIEVAL__EMBEDDING_PROVIDER`): `hash` (default) is an offline, deterministic character-trigram hashing embedder - zero dependencies, but lexical (surface-text) similarity only, not semantic. `sentence-transformers` runs a local dense encoder (optional install, no CPython 3.14 wheel yet). `api` calls any OpenAI-compatible `/v1/embeddings` endpoint (OpenAI, Azure OpenAI, Ollama) via plain `httpx` - no vendor SDK, mirroring `HttpChatModel`. `gemini` calls Google's native `embedContent`/`batchEmbedContents` API (`app/retrieval/gemini_embedder.py`) - a genuinely different request/response shape and auth header (`x-goog-api-key`, not `Authorization: Bearer`) from the OpenAI-compatible providers, so it's a separate small client rather than another config value on `HttpEmbedder`; both real providers share the same startup-probe/fallback contract (`_probe_or_fallback` in `embedder.py`). Every real provider is probed once at process startup (`embed_query` on a short string); if unreachable, it logs a warning and falls back to the hash embedder for the life of the process - it never falls back per call, since mixing hash and API vectors in one similarity space would silently corrupt retrieval. A successful probe whose returned vector length doesn't match `SAD_RETRIEVAL__EMBEDDING_DIMENSIONS` is a hard config error (raised, not swallowed).

**Index fingerprinting**: every persisted index (the JSON file behind `SAD_RETRIEVAL__MEMORY_STORE_PATH`, or a Qdrant collection) is tagged with a fingerprint of the embedder that built it (provider + model + dimensions - reflecting what was *actually* selected, including any startup fallback to hash). On load, a fingerprint mismatch means the index is discarded rather than queried with an incompatible embedder; for Qdrant the fingerprint is folded into the collection name, so switching embedders transparently starts a fresh collection rather than reusing an incompatible one. This makes `SAD_RETRIEVAL__EMBEDDING_PROVIDER` safe to change in either direction with zero manual steps - the cost is simply re-running `POST /api/index/rebuild`.

Retrieval is optional grounding for narration, never a hard dependency: if an embedding call fails at runtime (e.g. an `api` provider going down after a successful startup probe), `retrieve_related_context` (`app/graph/nodes.py`) catches the failure and degrades to no retrieved context rather than failing the investigation.

## 8. Cache responsibilities

`app/cache/` (`SAD_CACHE__BACKEND=memory|redis`) caches only LLM narration text, keyed by a hash of the exact prompt and target schema - never a computed number (see [business-rules.md § No caching layer between a hard-rule decision and its data](business-rules.md#no-caching-layer-between-a-hard-rule-decision-and-its-data)). `RedisCacheStore` is used when configured and reachable; otherwise `InMemoryCacheStore` (the default) provides the identical interface, so - like retrieval - the platform never requires Docker to run.

## 9. .NET gateway role

`api-gateway/` is a thin, validated front door: it authenticates/authorizes (extension point), serves CMDB reads directly from SQL Server via Dapper (bypassing the AI service for simple lookups), and proxies every recommendation/investigation/decision operation to the FastAPI service via a typed `HttpClient`, mapping the AI service's own RFC 7807 errors straight through rather than masking them behind a generic 500. It never computes a score, rule result or forecast itself.

## 10. Chat UI role

The React UI's default route (`/`) is a chat interface (`ui/src/pages/Chat.tsx`), the primary way an engineer interacts with the platform: free-text in, threaded conversation out. Each message maps to one `POST /api/investigations` call - the same LangGraph pipeline described in §2, not a separate chat-specific code path - so a chat answer carries the identical guarantees (deterministic numbers, human-review gate, audit trail) as the structured screens. Investigations that pause for human review render inline Approve/Reject actions in the same thread; the 8 structured screens (Dashboard, Hosting Recommendation, Comparison, Right-Sizing, Placement, Forecast, Investigation Detail, Approval) remain available as supporting, form-first views for the same underlying endpoints.

## 11. Security controls

See [business-rules.md § Guardrails](business-rules.md#guardrails) for the full, itemized list.

## 12. Observability

Both HTTP surfaces expose Prometheus metrics at `GET /metrics`, unauthenticated (a scraper is another standard infra probe, same posture as `/api/health`/`/health`): the AI service via `prometheus-fastapi-instrumentator` (standard request rate/latency/status, auto-instrumented) plus platform-specific counters in `app/observability/metrics.py` - real LLM/embedding provider calls and outcomes, fallback-chain activations, narration cache hit rate, spend-budget denials, investigations created by type; the .NET gateway via `prometheus-net.AspNetCore` (`UseHttpMetrics`/`MapMetrics`) for the same standard request metrics on its own surface. `docker/docker-compose.yml` can run Prometheus (scraping both `/metrics` endpoints) and Grafana (pre-provisioned with Prometheus as a datasource and a starter dashboard) - see `docker/prometheus/` and `docker/grafana/`. Both sit behind `--profile observability`, separate from `--profile core` (the app itself), so they're only running when you're actually looking at them - see [setup.md § Run instructions (Docker, optional)](setup.md#9-run-instructions-docker-optional).

Structured logging (`structlog`, JSON in production via `SAD_SERVICE__LOG_JSON=true`) and optional LangSmith tracing (`LANGSMITH_TRACING=true`) remain the two other observability channels, unchanged by this - metrics are for "is something wrong right now," tracing is for "why did this one LLM call behave that way," logs are for everything else.
