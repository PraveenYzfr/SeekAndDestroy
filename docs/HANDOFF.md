# Handoff — 2026-08-15

State: **286 tests passing** (ai-service) + **14** (MCP server) + **27** (gateway),
database consistent, all three tiers verified live.

Work since 2026-08-15 is on branch `feat/conversation-history-and-llm-observability`
(10 commits), not on `main`.

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

  Current scorecard, 141 calls: `deepseek-v4-flash` at **100% on all three
  properties** (n=1019 numbers, 205 entity mentions), p50 5.7s, p95 17.6s.

  **Both "findings" from the first run were defects in the evaluation, not in
  the model** - worth remembering before trusting any future one:
  - The required-fields list was hand-written and demanded a `summary` from
    `TradeOffSummary`, which has no such field. The same wrong names had been
    copied into the UI, which would have rendered an empty panel. Required
    fields are now derived from the Pydantic contract so they cannot drift.
  - Audit prompts were sliced at 8 KB mid-token, so rows stopped parsing,
    attribution was lost and correctly-quoted figures graded as invented -
    scoring a well-behaved provider at 62%.

  Two more measurement flaws found while hardening: cache hits were graded as
  independent samples (one answer served twenty times counted as twenty
  successes), and digits inside identifiers were counted as quoted figures
  (`nyc-p006` contributed "006", which always matched, flattering the rate).

  Not yet enterprise-grade. What is missing, in order:
  - **No golden set.** It grades whatever production happened to run, so two
    models cannot be compared unless both happened to run the same work. A
    fixed case suite runnable on demand against any provider is the gap that
    matters most for model evaluation.
  - **No stored history.** Each run recomputes and prints; drift over time -
    the thing `*-latest` aliases make inevitable - is eyeballed between runs.
  - **No token or cost accounting.** Latency is recorded, spend is not.
  - **Three properties only** - no refusal correctness, answer relevance or
    citation validity.
  - **The entity regex is estate-specific** (`[a-z]{3}-[a-z]?\d{2,4}`) and
    would silently stop matching under a new naming convention. It should be
    derived from the CMDB.
  - **Nothing runs the gate.** `--min-entities 1.0 --min-numbers 0.98` exits
    non-zero, but no pipeline calls it.

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

### 1c. Enterprise-readiness — merged priority (2026-08-18)
Two independent assessments, reconciled. Every claim below was verified against
the code, including the ones that turned out to be wrong.

Rated **B- / ~7.2 of 10**: ahead of most enterprise AI where these systems are
usually weak (a deterministic engine owns every number; the model narrates),
and behind where they are usually adequate (governance, cost, scale). The hard
half is done; the operational half is the cheaper one.

Do these in order. The first four are ceilings - nothing else counts while
they stand.

1. **`SqliteSaver` -> a SQL Server checkpointer** (`graph.py:117`). Checkpoints
   are pod-local, so a graph paused for review is only resumable on the replica
   that started it. This is a persistence rewrite, not a config change: a wall,
   not a weakness. ~2-3 days.
2. **Classification-aware model routing.** `DataClassification` /
   `ComplianceClassification` drive eligibility rules and are embedded into the
   vector documents, and are consulted *nowhere* on the path to a model
   provider. No redaction anywhere in `app/`. A Restricted application's
   metadata goes to DeepSeek and now sits in `AgentAuditLog` at up to 64 KB a
   call. The platform knows the classification and does not read it - this is
   the finding an audit review opens with, and it is the same decision already
   blocking the incident-prose work. ~1 day.
3. ~~**A guard on `POST /api/auth/dev-token`.**~~ **Done 2026-08-18.**
   `SAD_AUTH__ALLOW_DEV_TOKEN` now exists and is read; false returns 404, the
   same refusal oidc mode gives, because a disabled back door should not
   announce that it exists. Defaults true so local development is unchanged -
   and the line that has been sitting in `docker-compose.vm.yml:53` all along
   now does something.
4. ~~**Rate limiting.**~~ **Done 2026-08-18.** Per-employee token bucket
   (`app/api/rate_limit.py`) on `POST /api/investigations` and `/resume` - the
   two that always spend. 20/minute by default, `SAD_RATELIMIT__LLM_REQUESTS=0`
   disables it (the test suite does, since it drives the API far faster than a
   person would). A bucket rather than a fixed window, because a window lets a
   caller spend one allowance at the end of it and the next at the start of the
   following one - the burst it was meant to prevent, at twice the size.
   Verified over HTTP: third request in a capacity-2 window returns 429.

   Deliberately per-process: under N replicas the effective limit is N times
   the configured rate. That is a stated, bounded overshoot, and the real
   enforcement point for a public deployment is the gateway in front. It needs
   no Redis, which is why it landed today rather than being planned.

   **Still open from this item: token accounting.**
   `SAD_LLM__DAILY_CALL_BUDGET` counts *calls*, not tokens, and
   a 500-token call and a 60,000-token call are identical to it. Providers
   return usage on every response and nothing reads it. ~half a day.
5. **Distributed tracing.** Four model calls across three services per
   investigation, correlated by nothing but a correlation id. ~2 days.
6. **A golden evaluation set.** `scripts/evaluate.py` grades whatever
   production happened to run, so two models cannot be compared unless both
   happened to do the same work. Fixed investigations x N providers, scored on
   extraction accuracy, drift-guard survival, latency and cost. ~1 week.

Then: load testing (0 files today, and the ceiling is predictable without it -
**every route is sync**, so a 17.6s p95 model call holds a threadpool worker
for 17.6s), degraded-path tests (nobody has verified what happens when all four
providers are down, or Qdrant is unreachable), a restore drill actually run,
retention policy on conversation history and audit rows, SLOs on the metrics
already emitted, and a runbook.

One correction worth keeping: the drift guard is **not** a 5/5. It inspects
structured numeric fields only, so every figure the model writes inside a
sentence is unchecked - and the grounded-question path now quotes scores in
prose. Calling it perfect is how it stays unfixed.

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
- ~~`POST /api/auth/dev-token` issues a valid token with no credential check.~~
  **Done** - `SAD_AUTH__ALLOW_DEV_TOKEN=false` shuts it, returning 404.
- ~~`SAD_AUTH__LOCAL_SIGNING_KEY` defaults to a value published in this repo.~~
  **Done** - the service now *refuses to start* on the published key whenever
  `ALLOW_DEV_TOKEN=false`, and rejects any key under 32 characters. Tied to
  that flag rather than a new "is production" switch because no legitimate
  configuration disables the back door and keeps the public key, and a knob
  nobody sets protects nobody. Local development sets neither and is
  unaffected.
- ~~CORS was `allow_origins=["*"]` with `allow_credentials=True`~~ - unsafe,
  and not even legal: browsers refuse credentials to a wildcard origin, so it
  read as "anyone, authenticated" while buying nothing. Now
  `SAD_CORS__ORIGINS`, defaulting to the local dev origins; a wildcard is
  still allowed and drops credentials, which is the only honest reading of it.
- `docker/docker-compose.vm.yml` now **demands** `SAD_AUTH_SIGNING_KEY` and
  `SAD_PUBLIC_ORIGIN` from the host `.env` - compose fails rather than starting
  something insecure.
- **Still open:** no TLS. Cloudflare Tunnel + `praveenyzfr.com` was the agreed
  approach and terminates TLS for you.
- **Still open before real data:** every prompt goes to DeepSeek unredacted and
  is stored in `AgentAuditLog`, and `DataClassification` is never consulted on
  that path (item 1c #2). Irrelevant while the estate is generated seed data;
  a blocker the moment any of it is real.
- **Still open:** only `/api/investigations` and `/resume` are rate limited.
  The `explain=true` paths spend too and are not throttled.

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
