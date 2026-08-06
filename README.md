# SeekAndDestroy

**SeekAndDestroy** is an AI-powered infrastructure recommendation platform for infrastructure engineers. It analyzes CMDB, hosting, capacity, utilization, cost, dependency, resiliency and compatibility data to recommend where to host applications, right-size clusters, consolidate workloads, and forecast when capacity will run out — with every number computed deterministically and every recommendation requiring human approval before it means anything.

It is **not** a deletion or cleanup tool. There is no provisioning, decommissioning or migration capability anywhere in the platform — see [docs/business-rules.md § Guardrails](docs/business-rules.md#guardrails).

## What's here

| Component | Path | Tech |
|---|---|---|
| Deterministic engines + FastAPI service | `ai-service/` | Python 3.14, FastAPI, LangChain, LangGraph, SQLAlchemy |
| MCP server (27 tools, 7 resources) | `mcp-server/` | MCP Python SDK |
| MCP client (interactive CLI + LangChain adapter) | `mcp-client/` | MCP Python SDK |
| API gateway | `api-gateway/` | ASP.NET Core 8, Dapper |
| UI (Chat is the primary interface) | `ui/` | React + TypeScript + Vite |
| Database | `database/` | SQL Server (schema, deterministic seed, reset) |
| Docs | `docs/` | architecture, business rules & guardrails, scoring model, API contracts, setup, demo scenarios |

## Quick start

```bash
# 1. Database
sqlcmd -S LAPTOP-R6U8H616 -d PraveenDB -E -C -i database\schema.sql
sqlcmd -S LAPTOP-R6U8H616 -d PraveenDB -E -C -i database\seed.sql

# 2. AI service (runs with zero API keys - mock LLM by default)
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r ai-service\requirements.txt
cd ai-service && ..\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8088

# 3. Try it
.venv\Scripts\python.exe mcp-client\interactive_client.py --query "Find the best clusters for hosting APP-PAYMENTS."
```

Full instructions: [docs/setup.md](docs/setup.md). Ten worked demo queries with real output: [docs/demo-scenarios.md](docs/demo-scenarios.md).

## How a recommendation is made

```
requirement (from an app or a raw ask)
  → hard eligibility rules (RULE-001..010, deterministic)
  → capacity + projected-utilization calculation (deterministic)
  → weighted scoring across 7 sub-scores (deterministic, reproducible)
  → ranking (total order, no ties)
  → LLM narrates the already-computed result
  → app.agents.guards rejects any explanation whose numbers don't match the evidence
  → human review (LangGraph interrupt - a real pause, checkpointed to SQLite)
  → persisted only after a human approves, rejects, or asks for more analysis
```

**The LLM never computes a number and never chooses infrastructure.** Every score, cost, utilization percentage and forecast comes from `app/rules`, `app/scoring`, `app/services/capacity.py` and `app/forecasting/engine.py`. Full detail: [docs/architecture.md](docs/architecture.md) and [docs/business-rules.md](docs/business-rules.md).

## Status

All layers are built and verified working end to end against a live SQL Server instance, seeded at production-representative scale: 256 clusters across 64 neighborhoods in 8 (fictional) US-city data centers, 15 lines of business, 40 applications.

- **115 automated tests passing**: 83 Python (`ai-service/tests`, including the 13 critical tests named in the specification), 11 MCP server tests, 21 .NET tests.
- FastAPI ↔ LangGraph ↔ LangChain ↔ deterministic engines ↔ SQL Server: verified live (hosting recommendations, right-sizing, consolidation, forecasting, human-review interrupt/resume).
- MCP server: all 27 tools and 7 resources verified via an in-process client, including audit logging.
- .NET gateway ↔ AI service ↔ SQL Server: verified live, including error propagation and validation.
- React UI: all 9 screens (Chat + 8 structured screens) runtime-verified live in a browser against the running gateway/AI service, including the chat interface's full investigation → human-review → approval thread.
- Real vs. simulated is a single config value per component, all defaulting to a fully offline mode that needs zero API keys/Docker: `SAD_LLM__PROVIDER` (mock/openai/azure-openai/ollama, plus `SAD_LLM__FALLBACK_PROVIDERS` for multi-LLM fallback), `SAD_RETRIEVAL__BACKEND` (memory/qdrant) with `SAD_RETRIEVAL__EMBEDDING_PROVIDER` (hash/sentence-transformers/api/gemini — `api` speaks any OpenAI-compatible `/v1/embeddings` endpoint, `gemini` speaks Google's native embeddings API), `SAD_CACHE__BACKEND` (memory/redis) — each real backend falls back to its offline default automatically if unreachable. Every persisted vector index is fingerprinted by (embedding provider, model, dimensions); switching `SAD_RETRIEVAL__EMBEDDING_PROVIDER` automatically discards a now-incompatible index rather than silently returning nonsense similarity scores.

## Documentation

- [docs/architecture.md](docs/architecture.md) — component diagram, end-to-end flow, per-component responsibilities.
- [docs/business-rules.md](docs/business-rules.md) — RULE-001..010, RIGHTSIZE-001..006, capacity formulas, and the full **guardrails** section.
- [docs/scoring-model.md](docs/scoring-model.md) — score formula, weights, every sub-score's exact math, a worked example.
- [docs/api-contracts.md](docs/api-contracts.md) — every FastAPI and gateway endpoint.
- [docs/setup.md](docs/setup.md) — prerequisites, install, run, test instructions for every component.
- [docs/demo-scenarios.md](docs/demo-scenarios.md) — the 10 specification demo queries with real output.
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — the build plan this project was implemented from, including the verified environment facts and standing rules.
