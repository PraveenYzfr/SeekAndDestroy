# Handoff — 2026-08-15

State: **262 tests passing** (ai-service) + **14** (MCP server) + **27** (gateway),
database consistent, all three tiers verified live.

Work since 2026-08-15 is on branch `feat/conversation-history-and-llm-observability`
(5 commits), not on `main`.

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
- **Chat remembers the conversation** - "give me the options again" replays the
  stored shortlist, "why not the second one?" answers from the previous run's
  own evidence, "what about in staging?" keeps the subject. See item 1.
- **Username/password auth** end to end, scrypt hashes, no default credential.
- **Containerised SQL Server** - `db-init` builds and seeds it on first start.
  Connect SSMS to `127.0.0.1,14330` (the IPv4 literal, *not* `localhost`).

## Open work, in priority order

### 1. Conversation history — done
Chat follow-ups now have a referent. `sad.Conversation` / `sad.ConversationTurn`
(migration_002, idempotent, already applied to the dev database) thread a
conversation id through UI → gateway → AI service → graph state.

Resolution is deterministic (`app/graph/conversation.py`) - pattern matching
over the query and the previous investigation's own results, never asking the
LLM what the user meant. Same trust boundary as routing: a model that decided
"the options" meant a different cluster would produce a confident answer about
infrastructure the engineer never saw. Three kinds:

- **Recall** - "give me the options again". Restates the stored shortlist with
  no second Investigation row. A shortlist still awaiting a decision comes back
  as a live review payload, so it can still be acted on. It deliberately does
  not re-run: utilization moves between requests, and "again" answering with
  different numbers is not what "again" means.
- **About-previous** - "why not the second one?". A Question investigation
  whose grounding is the previous run's candidates - including the rules that
  failed and *which options were actually on screen, in order*. Without those
  positions DeepSeek answered "the evidence does not explicitly identify which
  cluster that one refers to"; with them it answers with the real scores.
- **Inherit-subject** - "what about in staging?". Carries the application code
  or capacity size forward. The carried text goes *after* the user's words,
  because extraction takes the first match per dimension - reversed, "and with
  128 GB RAM?" would silently resize to the previous figure.

Detection is conservative on purpose: naming an application ends the reference,
a named cluster is never a recall, and a bare preposition only continues the
previous request when it is the whole message ("in staging?" yes, "in
production, which clusters are underutilized?" no).

Conversation ids are server-generated uuid4 and ownership is checked on every
message (403) - a conversation carries what someone asked and what they were
shown.

Known limits:
- Reloading the chat starts a new conversation. Turns are in the database, but
  nothing reads them back into the UI yet.
- The Question path now quotes numbers in prose that it previously did not.
  `assert_no_number_drift` only inspects *numeric fields* of structured output,
  so prose figures are unchecked here exactly as they are in every other
  narration path - a prose-level check is still missing platform-wide.
- `db-init.sh` now runs migrations on **every** start, not only first init.
  They are idempotent, and the old guard meant the databases that needed a
  migration were the ones that never got it.

### 1b. Widen the LLM's role — assessed, 4 of 9 scopes delivered
Agreed pattern, reached by arguing it out on 2026-08-15: **the LLM reads
unstructured text and emits bounded, cited, structured findings; Python maps
those findings to points.** Never text -> score. Findings cite incident/change
ids so a claim can be checked, `confidence: Low` counts as no finding, and the
result is persisted with a timestamp rather than inferred per request - which
is what makes it reproducible between runs.

Done since (2026-08-16):
- **Every model call is audited.** `run_structured` is the choke point every
  chain funnels through, so one hook covers all six; rows land in
  `sad.AgentAuditLog` tagged with the investigation and graph node via
  `app/observability/audit_context.py` (a ContextVar, because a module global
  would let one investigation's node name land on another's row). The wrapper
  is applied centrally in `_build_graph`, so a node added later cannot forget
  it. Cache hits are audited too and flagged - otherwise the log has a hole
  exactly where "what did investigation 74 actually report?" gets asked.
  Fail-open by design: a broken audit table logs loudly and does not take an
  investigation down with it, which is the right trade only because this
  platform never executes a change. Verified live - investigation 87 produced
  4 rows naming `parse_user_request` and `generate_recommendation_explanations`.
- **The three orphaned chains are wired.** `explain_forecast` on
  `/api/forecast`, `summarize_tradeoffs` on all three placement endpoints,
  `explain_application_right_sizing` on `/api/right-sizing/applications` - all
  behind an opt-in `explain` flag (`_ExplainableRequest`), because these
  endpoints can return 500 clusters and narrating each is a model call apiece.
  Bounded to `narration.MAX_NARRATED`, and a narration failure costs the prose
  and nothing else. A forecast explains only the *binding* resource: narrating
  all three costs three calls to say two things nobody asked about.

- **The screens ask for it.** Forecast and comparison offer narration as an
  opt-in second request, so the numbers are never held up by a model call and
  a reader who only wants figures never pays for prose. `Explain` threads
  through the gateway DTOs.
- **The model is graded against the platform's own answer key.**
  `app/evaluation/` scores three properties per recorded call - are the numbers
  traceable to the evidence, do the cluster codes exist in it, do the fields
  that carry the answer carry one - per model, off `sad.AgentAuditLog`, so a
  full run costs a table scan rather than a provider bill. Graders are pure
  functions, never models: an LLM-as-judge would introduce the failure being
  measured. `python scripts/evaluate.py`.

  First run, 97 calls: `deepseek-v4-flash` at **100% number and entity
  fidelity**, 96.2% completeness, 5 failures. Two `TradeOffSummary` calls
  returned an empty `summary` - schema-valid and useless, which is exactly the
  class the type system cannot see.

  It also caught a bug in the audit code from the day before: slicing the
  prompt at 8 KB cut mid-token, so rows stopped parsing, model attribution was
  lost, and figures quoted correctly graded as invented - a well-behaved
  provider scored 62% entity fidelity. Fixed; truncated rows are now flagged
  and excluded from fidelity rather than judged wrongly.

Still unwired: `extract_hosting_requirement` (free-text intake for
applications not yet in the CMDB) - scope 07 of the assessment.

Two contained pieces already identified:
- `historical_performance_subscore` ignores `RootCauseCategory` entirely and
  weights by severity alone, so two Sev1s cost 40 points whether it was a
  storage controller or a bad app deploy. Category + chronicity weighting is
  deterministic, independent of everything else, and worth doing first. It
  moves every score, so the scenario clusters are the check.
- `sad.Incident` has no text columns. Description, work notes, resolution notes
  and the fix applied are where incident *nature* actually lives; the
  structured fields are a lossy summary of it. Seed data would need plausible
  notes for the suite to test against.

Unresolved before building the extraction path: work notes become a placement
input (injection surface), and shipping real incident prose to a hosted
provider is a governance call - a local model for that path only may be the
answer.

Full nine-scope assessment, with effort and dependency order:
https://claude.ai/code/artifact/d175bd78-a786-4390-88e9-accf633a8724

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
