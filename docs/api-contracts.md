# SeekAndDestroy — API Contracts

Two HTTP surfaces exist: the **FastAPI AI service** (port 8088, source of truth for every computed value) and the **ASP.NET Core gateway** (port 5090 in local dev, the UI's actual entry point). The gateway proxies most operations to the AI service and forwards its errors verbatim; it serves CMDB reads directly from SQL Server.

Full interactive docs: `http://127.0.0.1:8088/docs` (FastAPI/Swagger) and `http://127.0.0.1:5090/swagger` (gateway, Development environment only).

## Error shape (both services)

RFC 7807 `application/problem+json`:
```json
{
  "type": "about:blank",
  "title": "Cluster not found",
  "status": 404,
  "detail": "No cluster with code 'CL-DOES-NOT-EXIST'.",
  "instance": "/api/hosting/recommendations",
  "correlationId": "..."
}
```

## FastAPI AI service (`ai-service/app/api/`)

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/api/health` | — | Liveness only. |
| GET | `/api/ready` | — | Checks SQL Server + vector store; 503 if either fails. |
| POST | `/api/index/rebuild` | — | Rebuilds the retrieval index from current CMDB/capacity data. |
| POST | `/api/investigations` | `{query, created_by_employee_id}` | Runs `InfrastructureRecommendationGraph`; returns `AwaitingReview` or `Completed`. |
| GET | `/api/investigations/{id}` | — | Raw `Investigation` row. |
| POST | `/api/investigations/{id}/resume` | `{decision, reviewer_employee_id, comments}` | Resumes a paused graph via `Command(resume=...)`. |
| GET | `/api/investigations/{id}/recommendations` | — | All `InfrastructureRecommendation` rows for the investigation. |
| POST | `/api/hosting/recommendations` | `{application_code}` | Scenario A - ranked candidates for an existing application. |
| POST | `/api/capacity/recommendations` | `{environment, cpu_cores, memory_gb, storage_gb, platform, availability_tier, data_classification, ...}` | Scenario B - ranked candidates for a raw requirement; also creates a `CapacityRequest` row. |
| POST | `/api/right-sizing/clusters` | `{cluster_code?}` | One cluster, or all if omitted. |
| POST | `/api/right-sizing/applications` | `{application_code?}` | One application, or all if omitted. |
| POST | `/api/consolidation/analyze` | `{environment?}` | Consolidation candidates. |
| POST | `/api/forecast` | `{cluster_code, horizon_days}` | `horizon_days` must be 30/60/90/180. |
| GET | `/api/applications/{id}/hosting` | — | All hosting records for an application. |
| GET | `/api/clusters/{id}/capacity` | — | Full `ClusterCapacitySnapshot`. |
| GET | `/api/clusters/{id}/utilization?days=N` | — | Raw utilization series. |
| POST | `/api/recommendations/{id}/decision` | `{decision, reviewer_employee_id, reason?}` | `reviewer_employee_id` must be `> 0`. |

Field naming: request/response bodies use the exact `snake_case` field names of their Pydantic models (e.g. `application_code`, `cpu_cores`) unless the payload is a pass-through of a SQL-mapped entity, in which case it uses the entity's `PascalCase` column names (e.g. `ApplicationCode`, `CpuRequirement`) - see `docs/architecture.md` and `ui/src/types/index.ts` for exactly which shape each endpoint returns.

## ASP.NET Core gateway (`api-gateway/SeekAndDestroy.Api/Controllers/`)

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | ASP.NET health check (SQL Server connectivity). |
| GET | `/api/cmdb/applications?environment=` | Direct Dapper query, `camelCase` JSON. |
| GET | `/api/cmdb/clusters?environment=` | Direct Dapper query, `camelCase` JSON. |
| GET | `/api/cmdb/clusters/{id}` | 404 via ProblemDetails if missing. |
| POST | `/api/recommendations/hosting` | Proxies to `/api/hosting/recommendations`. |
| POST | `/api/recommendations/capacity` | Proxies to `/api/capacity/recommendations`. |
| POST | `/api/recommendations/right-sizing` | `{clusterCode?, applicationCode?}` - routes to the cluster or application AI-service endpoint. |
| POST | `/api/recommendations/consolidation` | Proxies to `/api/consolidation/analyze`. |
| POST | `/api/recommendations/forecast` | Proxies to `/api/forecast`; `horizonDays` validated to {30,60,90,180}. |
| POST | `/api/recommendations/{id}/approve` | `{reviewerEmployeeId, reason?}`, `reviewerEmployeeId` must be `> 0`. |
| POST | `/api/recommendations/{id}/reject` | Same shape as approve. |
| POST | `/api/investigations` | `{query, createdByEmployeeId}`. |
| GET | `/api/investigations/{id}` | Proxies to the AI service. |
| POST | `/api/investigations/{id}/resume` | `{decision, reviewerEmployeeId, comments?}`. |
| GET | `/api/investigations/{id}/recommendations` | Proxies to the AI service. |

Gateway request bodies use `camelCase` (ASP.NET Core's default); `AiServiceClient` re-cases them to `snake_case` on the wire to the AI service via `SnakeCaseNamingPolicy` before forwarding.

## MCP tools and resources

See `docs/business-rules.md` and `mcp-server/server.py` for the full list of 27 tools / 7 resources; every tool's docstring is its MCP-visible description.
