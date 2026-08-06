# SeekAndDestroy — Setup

## Prerequisites

| Tool | Verified version | Notes |
|---|---|---|
| Python | 3.14.0 | Only interpreter tested; 3.12+ should also work. |
| SQL Server | 2025 (any recent edition) | A reachable instance with a database to hold the `sad` schema. |
| ODBC Driver 17 or 18 for SQL Server | either | Required by `pyodbc`. |
| .NET SDK | 8.0+ (gateway targets `net8.0`) | Tested with SDK 10.0.302 building a net8.0 target. |
| Node.js / npm | 18+ | **Not required to build/run the AI service, MCP server or .NET gateway** - only needed for the React UI. |
| Docker (optional) | any recent | Only needed if you want a real Qdrant instance; the platform runs fully without it (in-memory vector store). |

## 1. SQL Server setup

Point `ai-service/.env` (copy from `.env.example`) at your instance. The default assumes Windows Integrated Security against a local named instance:

```
SAD_DB__SERVER=YOUR-SERVER-NAME
SAD_DB__DATABASE=PraveenDB
SAD_DB__SCHEMA=sad
SAD_DB__INTEGRATED_SECURITY=true
```

Apply the schema and seed data (idempotent - `reset.sql` only ever touches the `sad` schema, nothing else in the database):

```bash
sqlcmd -S YOUR-SERVER-NAME -d PraveenDB -E -C -i database\schema.sql
sqlcmd -S YOUR-SERVER-NAME -d PraveenDB -E -C -i database\seed.sql
```

To regenerate `database/seed.sql` (byte-for-byte reproducible - see `scripts/generate_seed.py`):

```bash
.venv\Scripts\python.exe scripts\generate_seed.py
```

## 2. Python setup

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r ai-service\requirements.txt
copy ai-service\.env.example ai-service\.env
```

The platform runs with **zero external API keys** by default (`SAD_LLM__PROVIDER=mock`). To use a real model, set `SAD_LLM__PROVIDER` to `openai`, `azure-openai` or `ollama` and the matching credentials in `.env`.

Run the AI service:
```bash
cd ai-service
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8088
```

Run the MCP server (stdio transport):
```bash
.venv\Scripts\python.exe mcp-server\server.py
```

Run the interactive MCP client:
```bash
.venv\Scripts\python.exe mcp-client\interactive_client.py
.venv\Scripts\python.exe mcp-client\interactive_client.py --demo   # runs all 10 spec demo queries
.venv\Scripts\python.exe mcp-client\interactive_client.py --query "Find the best clusters for hosting APP-PAYMENTS."
```

## 3. .NET setup

```bash
cd api-gateway
dotnet restore
dotnet build SeekAndDestroy.slnx
```

`SeekAndDestroy.Api/appsettings.json` already contains the connection string from the specification and `AiService:BaseUrl=http://127.0.0.1:8088`. Adjust for your environment; for production secrets, use `appsettings.Development.json`, user secrets, or environment variables - never commit real credentials.

Run the gateway:
```bash
cd SeekAndDestroy.Api
dotnet run --urls http://127.0.0.1:5090
```

## 4. Qdrant setup (optional)

The platform defaults to `SAD_RETRIEVAL__BACKEND=memory` (no server required). To use real Qdrant:

```bash
docker run -p 6333:6333 qdrant/qdrant
```
then set in `.env`:
```
SAD_RETRIEVAL__BACKEND=qdrant
SAD_RETRIEVAL__QDRANT_URL=http://localhost:6333
```
If Qdrant is configured but unreachable, the AI service automatically falls back to the in-memory store rather than failing to start.

## 5. React UI setup

Requires Node 18+.

```bash
cd ui
npm install
npm run dev          # http://localhost:5173, proxies /api to the gateway on :5090
npm run build         # type-checks (tsc) and produces a production build in ui/dist
```

Set `VITE_GATEWAY_URL` if the gateway isn't on `http://127.0.0.1:5090`. The default route (`/`) is the chat interface - see [architecture.md § Chat UI role](architecture.md#10-chat-ui-role); the 8 structured screens (Dashboard, Hosting Recommendation, Comparison, Right-Sizing, Placement, Forecast, Investigation Detail, Approval) are reachable from the sidebar.

## 6. Environment variables reference

See `ai-service/.env.example` for the full list with defaults and comments (database, LLM provider, retrieval, capacity policy, scoring weights, forecast settings, service limits).

## 7. Database initialization summary

```bash
sqlcmd -S <server> -d <database> -E -C -i database\schema.sql   # creates schema `sad`, 17 tables
sqlcmd -S <server> -d <database> -E -C -i database\seed.sql     # deterministic seed data
sqlcmd -S <server> -d <database> -E -C -i database\reset.sql    # drops schema `sad` only - safe on a shared DB
```

## 8. Run instructions (all services)

```bash
# Terminal 1 - AI service
cd ai-service && ..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8088

# Terminal 2 - .NET gateway
cd api-gateway\SeekAndDestroy.Api && dotnet run --urls http://127.0.0.1:5090

# Terminal 3 - React UI (requires Node.js)
cd ui && npm run dev
```
Or use `scripts/run-ai-service.ps1`, `scripts/run-mcp-server.ps1`, `docker/docker-compose.yml` (see `docs/demo-scenarios.md`).

## 9. Test instructions

```bash
# Python (36 tests: capacity, rules, forecasting, scoring, seed determinism, 13 critical tests, API)
cd ai-service && ..\.venv\Scripts\python.exe -m pytest tests\ -v

# MCP server (8 tests)
cd mcp-server && ..\.venv\Scripts\python.exe -m pytest tests\ -v

# .NET gateway (21 tests)
cd api-gateway && dotnet test SeekAndDestroy.slnx
```

All 65 tests pass against the seeded database as of this writing - see `scripts/run-tests.ps1` to run everything in one pass.

## 10. Demo instructions

See `docs/demo-scenarios.md`.
