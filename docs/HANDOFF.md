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

### 1. Conversation history (in progress, ~35% - paused 2026-08-13)
Each chat message is an independent investigation. "give me the options again"
has no referent, so it routes to Question and answers that context is empty.

**Uncommitted working tree.** Nothing below is wired into a running code path
yet: the two new modules are not imported by anything, so the service behaves
exactly as it did before. `test_critical.py` + `test_api.py` pass (24) against
the migrated database.

Done:
- `database/schema.sql` section 18 + `migration_002_conversations.sql`
  (idempotent, **already applied to the dev PraveenDB**): `sad.Conversation`,
  `sad.ConversationTurn`, `sad.Investigation.ConversationId`. Turn order is
  TurnId order - there is deliberately no TurnIndex column.
- `reset.sql` drops both (turns first, Conversation *after* Investigation - the
  FKs form a cycle that is broken by declaring the column in section 14 and the
  constraint in section 18).
- `docker/db-init.sh`: grants for the two tables, and migrations now run on
  **every** start rather than only on first init. They are written to be
  idempotent, and the old guard meant exactly the databases that needed a
  migration were the ones that never got it.
- `app/repositories/conversation_repository.py` - create/get/touch/add_turn/
  recent_turns/last_investigation_id. `investigation_repository.create` takes
  an optional `conversation_id`.
- `app/models/entities.py` - `Conversation`, `ConversationTurn`, and
  `Investigation.ConversationId`.
- `app/graph/conversation.py` - deterministic follow-up resolution (no LLM;
  same trust boundary as routing). Three kinds: RECALL ("give me the options
  again" - restate the stored shortlist, no second Investigation row and no
  re-run that could answer "again" with different numbers), ABOUT_PREVIOUS
  ("why was that rejected?" - Question path grounded in the prior
  investigation's candidates instead of a vector search), INHERIT_SUBJECT
  ("what about in staging?" - carries the app code or capacity size forward).

Next, in order:
1. **Finish `looks_like_follow_up`.** Two known holes, both left mid-edit: the
   trailing `if referential: return ABOUT_PREVIOUS` fallback and the
   ABOUT_PREVIOUS question branch must both require `not has_own_subject`, or a
   query naming its own application ("find hosting for APP-CRM like it") gets
   treated as a follow-up. RECALL must also reject queries carrying a cluster
   code ("forecast CL-NYC-03 again" is not a recall). `_FRAGMENT_RE` is written
   but not yet used - it needs the `<= _FRAGMENT_MAX_WORDS` guard applied in
   code, because "in production, which clusters are underutilized?" is a
   complete question, not a continuation.
2. **State keys** in `app/graph/state.py`: `conversation_id`, `resolved_query`,
   `prior_investigation_id`, `prior_context_docs`, `follow_up_kind`. LangGraph
   silently drops undeclared keys - see the trap below, it has cost a day once.
3. **`run_investigation(conversation_id=...)`**: record the user turn, resolve
   against `PriorInvestigation.from_state(get_investigation_state(...))`, handle
   RECALL and the no-referent reply *before* the Investigation row is created
   (same reasoning as `quick_reply`), then record the assistant turn via
   `conversation.turn_summary`. Recall of a still-paused investigation should
   re-emit the review payload (rebuild with `nodes._review_option`) so the
   engineer can still decide; recall of a closed one returns the prior id and
   lets the UI re-fetch the recommendation rows, whose Status is authoritative.
4. **Nodes**: classify/extract from `resolved_query` while `user_query` stays
   the literal text for display; prepend `prior_context_docs` in
   `retrieve_related_context`.
5. **API + gateway**: `conversation_id` in `CreateInvestigationRequest` and in
   every investigation response; ownership check in the route (403 on
   mismatch - conversation ids are server-generated uuid4 precisely so that
   check protects something); `CreateInvestigationRequestDto` in .NET.
6. **UI**: `Chat.tsx` holds the returned id and sends it on every message, plus
   a "New chat" control.
7. **Tests**: `tests/test_conversation.py` (detection, inheritance ordering -
   the carried subject goes *after* the user's words so a new "128 GB RAM" wins
   over the old figure - recall, and the end-to-end follow-up).

Already mitigated separately: `quick_reply` (app/graph/nodes.py) intercepts
greetings and vague asks before an Investigation row is created, so "hi" no
longer produces a report. It does not give follow-ups memory.

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
