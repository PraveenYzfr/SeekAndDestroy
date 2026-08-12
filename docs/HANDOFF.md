# Handoff — 2026-08-12

State: **192 tests passing**, database consistent, all three tiers verified live.
Everything below is committed.

## What works today

Sign in at http://127.0.0.1:5173 as `E1001` with the password you set.

```bash
# three terminals, or the container stack
cd ai-service ; ..\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8088
cd api-gateway\SeekAndDestroy.Api ; dotnet run --urls http://127.0.0.1:5090
cd ui ; npm run dev

# or everything, database included, one command
docker compose -f docker/docker-compose.yml --profile core up --build
```

- **Real Gemini** drives narration (`gemini-flash-lite-latest`), with server-side
  `responseSchema` so a required field cannot be silently dropped. Free tier is
  **20 requests/day per model** - "Report narration unavailable" means quota, not
  a bug. Numbers stay correct regardless; only prose degrades.
- **Recommendations reach host level**: top 3 clusters x top 3 hosts, each with
  total/used/free CPU, memory and storage.
- **Review is a choice**: pick one cluster + host, that row is stored `Approved`,
  the rest `Superseded`.
- **Username/password auth** end to end, scrypt hashes, no default credential.
- **Containerised SQL Server** - `db-init` builds and seeds it on first start.
  Connect SSMS to `127.0.0.1,14330` (the IPv4 literal, *not* `localhost`).

## Open work, in priority order

### 1. Conversation history (not started)
Each chat message is an independent investigation. "give me the options again"
has no referent, so it routes to Question and answers that context is empty.
Needs: a conversation id threaded through the API, prior turns in state,
reference resolution ("the options", "that cluster"), and the Question path
answering from the previous investigation's results. ~half a day; touches graph
state, API contract and UI.

Partly mitigated: `quick_reply` (app/graph/nodes.py) now intercepts greetings and
vague asks before an Investigation row is created, so "hi" no longer produces a
report. It does not give follow-ups memory.

### 2. Host sizing is unrealistic (diagnosed, reverted once)
A "host" is 6-12 cores; real kit is 64-128 cores and 512 GB-2 TB.

Cause: `SIZE_RANGES` in scripts/generate_seed.py rolls a **cluster total** and a
**node count** independently, then sets `per_node = total / count` - per-host size
is a remainder, never a choice.

**Do not simply scale it.** Tried on 2026-08-12: scaling app demand 16x while
replacing cluster totals with SKU-derived values desynchronised supply and
demand, producing 363% utilisation and two test failures. Reverted.

The fix requires re-authoring the 15 hand-crafted scenario clusters, whose
designed behaviour (POOR_FIT, OVERPROVISIONED, and cmh-03 advertising Tier-1 with
too few hosts) depends on exact demand-to-capacity ratios. Treat it as one
focused piece of work with the test suite as the check.

### 3. Before anything is internet-reachable
- `POST /api/auth/dev-token` issues a valid token with **no credential check**.
  Needs a flag to disable it independently of OIDC mode.
- `SAD_AUTH__LOCAL_SIGNING_KEY` still defaults to a value published in this repo.
- No TLS. Cloudflare Tunnel + `praveenyzfr.com` was the agreed approach.

### 4. Multi-replica (AKS/OCP only)
LangGraph checkpoints go to pod-local SQLite, so a graph paused for human review
is only resumable on the replica that started it - approvals fail (N-1)/N of the
time. Irrelevant for a single VM. `langgraph-checkpoint-mssql` exists (0.1.0,
third-party, built against an older interface) and would keep checkpoints in SQL
Server; needs a real interrupt/resume test before trusting it.

## Traps worth remembering

- **LangGraph silently drops state keys** the TypedDict schema does not declare.
  A selection reached the graph and vanished; every row stayed PendingReview with
  no error anywhere.
- **`localhost,14330` hangs** against the SQL container - Docker publishes IPv4
  only, `localhost` resolves to `::1` first on Windows. Use `127.0.0.1,14330`.
- **`reset.sql` drops the schema**, taking the Employee row and its password
  hash. Re-run `scripts/set_password.py` after any reseed.
- **PowerShell 5.1 has no `&&`.** Use `;` or separate lines.
- **Gemini model names**: use `*-latest` aliases. Pinned versions get closed to
  new keys and 404 with "no longer available to new users", which reads exactly
  like a bad key.
- **MSSQLSERVER does not survive reboots.** `Start-Service MSSQLSERVER`.

## Budget

Gemini free tier is 20 req/day/model - enable billing before evaluation work.
For ~$10/month set `SAD_LLM__DAILY_CALL_BUDGET=100`; Google's Cloud budget alerts
but does not cap, so that setting is the only hard stop.
