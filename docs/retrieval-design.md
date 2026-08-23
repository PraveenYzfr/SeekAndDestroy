# Retrieval design — from CMDB dump to real RAG

## Why this document exists

The platform has Qdrant, 3072-dimension Gemini embeddings, fingerprinted
collections and metadata filtering. It does not have RAG in any meaningful
sense, and the reason is not the retrieval code.

**Every "document" in the index is a database row rendered into a sentence.**

```python
# app/retrieval/documents.py, incident_document()
f"{incident.Severity} incident on {subject_description}, opened {...}, "
f"status {incident.Status}, root cause category {incident.RootCauseCategory}."
```

`sad.Incident` has nine columns and none of them is free text: ids, `Severity`,
`OpenedAt`, `ClosedAt`, `Status`, `RootCauseCategory`. So the index contains
2,440 template sentences generated from structured fields.

Embedding those is **strictly worse than querying them**: slower, fuzzier, and
incapable of exact filtering. Anything the retriever can answer, a `WHERE`
clause answers better and with certainty.

This is why advanced retrieval cannot be bolted on. Chunking a one-sentence
template is meaningless. Reranking reorders sentences a `WHERE` clause already
selects exactly. The sparse half of a hybrid search would match identifiers
that exist as foreign keys.

**There is no corpus.** Everything below follows from fixing that.

Two other findings from the same review, both in the same direction:

* **Retrieval runs after the decision.** The graph order is
  `calculate_candidate_scores → rank_candidates → select_candidate_nodes →
  retrieve_related_context`. By the time anything is retrieved the winner is
  chosen. Retrieval feeds the prose and cannot influence the recommendation.
* **There is no change data at all.** Nineteen tables, no changes, no
  maintenance windows, no freeze periods. The platform cannot know it is
  recommending a cluster that is mid-migration.

---

## 1. Data model — ITSM-shaped

Numbering follows ServiceNow: prefix plus a **continuous, zero-padded 7-digit
sequence, no separator**.

```
INC1005000    PRB0040118    CHG0030291    CTASK0030294
```

Continuous matters for retrieval, not just convention: `INC1005000` survives
BM25 tokenisation as a single token, so sparse search matches it exactly. A
hyphenated `INC-1005000` splits into `INC` + `1005000`, and `INC` then matches
every incident in the corpus.

`Number` is a `NVARCHAR(20)` column with a unique index, generated from a
per-table sequence rather than derived from the identity column - ITSM numbers
have to survive a re-seed, and identity values do not.

### Tables

| Table | Purpose |
|---|---|
| `sad.Incident` (extended) | + `Number`, `ShortDescription`, `Description`, `Impact`, `Urgency`, `AssignmentGroup`, `CloseCode`, `CloseNotes`, `ProblemId`, `CausedByChangeId` |
| `sad.IncidentComment` | `IncidentId, Sequence, CreatedAt, CreatedBy, Type (work_note\|additional_comment), Text` |
| `sad.Problem` | `Number, ShortDescription, Description, RootCause, Workaround, FixNotes, IsKnownError, State, PermanentFixChangeId` |
| `sad.Change` | `Number, ShortDescription, Description, Type, ImplementationPlan, BackoutPlan, RiskAssessment, PlannedStart/End, ActualStart/End, State, CloseCode, CloseNotes, FreezeUntil, ClusterId` |
| `sad.ChangeComment` | same shape as `IncidentComment` |

### The links are the point

`INC → PRB` and `INC ← CHG`. An incident caused by a change, analysed in a
problem, where the problem is a known error with no permanent fix - that chain
is a risk signal, and it is exactly what SQL alone cannot surface as a
judgement. It is also the thing a recommendation should act on.

`sad.Problem` is the highest-value corpus in the system. Problem records are
written to *explain*; incidents mostly record *what happened*.

---

## 2. Seeding

Deterministic from the existing `SEED=20240101`, so the estate stays
reproducible.

Each incident gets **3-12 comments** following real thread shapes: triage,
reassignment, investigation, finding, fix, verification. Deliberately including
**noise** - `"Assigned to Network team."`, `"Monitoring."` - because a
retriever that only works on clean text has not been tested.

Problems are written as genuine multi-paragraph analysis referencing their
incidents by number.

---

## 3. Chunking

**A ticket is not a chunk.** The unit is a semantic section, because the
sections answer different questions.

| Chunk | Source |
|---|---|
| Header | `ShortDescription` + `Description` |
| One per substantive comment | `IncidentComment.Text` |
| Resolution | `CloseNotes` + final work note |
| Problem sections, separately | `RootCause`, `Workaround`, `FixNotes` |

Structured records (cluster, node, application, hosting) stay whole. They are
field lists; splitting them destroys meaning and gains nothing.

### Noise filtering before embedding

Comments under ~80 characters matching reassignment or acknowledgement patterns
are stored and available as metadata, but **not embedded**. Embedding
`"Assigned to Network team"` four thousand times poisons the vector
neighbourhood around every real query - the nearest neighbours of anything
become the most common boilerplate.

### Contextual prefixes

A comment on its own is unmoored. `"Confirmed the ballooning driver is
disabled on this host"` is useless without knowing which host. Every chunk is
prefixed with its entity identity and chain before embedding:

```
[INC1005432 · P1 · cmh-p212 · APP-PAYMENTS · 2026-03-14
 · PRB0040118 (known error) · caused by CHG0030291 · comment 7/11 · L3 Infra]
Confirmed the ballooning driver is disabled on this host...
```

Every chunk also carries temporal metadata. A 2021 incident should not outrank
last month's.

---

## 4. Retrieval pipeline

**Hybrid.** Dense (Gemini 3072) and sparse (BM25) as two named vectors in one
Qdrant collection, fused with Reciprocal Rank Fusion. Dense finds *"memory
exhaustion after failover"*; sparse finds `INC1005432` and `cmh-p212`, which
dense embeddings are genuinely bad at. The embedder fingerprint must cover
**both**, or a config change silently mixes similarity spaces.

**Rerank.** Retrieve 50 fused, cross-encoder to 8. A local model
(bge-reranker) rather than a hosted API: small, CPU-adequate, and it stays off
the provider budget, which is tiered DeepSeek -> Groq with the premium keys
reserved for benchmarking.

**Query transformation, deterministic.** `\b(INC|PRB|CHG|CTASK)\d{7}\b` and
cluster/node patterns are extracted into metadata filters; the remaining prose
goes to hybrid search. No LLM call - this is regex, and it is more reliable
than asking a model to identify an identifier.

**Problem records get a retrieval boost** on "why"-shaped questions, because
that is what they are written to answer.

---

## 5. Reaching the decision

`retrieve_related_context` moves **before** `calculate_candidate_scores`.

An extraction step produces **cited claims only**:

> `cmh-p212` - 3 P1 incidents in 90 days, all linked to `PRB0040118`, a known
> error with no permanent fix.
> Cited: INC1005432, INC1005610, INC1005788, PRB0040118.

A **deterministic rule** turns that into a risk penalty. The model extracts and
cites; Python decides the number.

This is the whole reason the trust boundary survives adding retrieval to the
decision path. `assert_no_number_drift` still holds, because no model ever
produces a figure - a reranker only *orders* documents, and an extractor only
*cites* records that exist.

---

## 6. Evaluation

Retrieval is currently unmeasured. `graders.py` measures `number_fidelity` and
`entity_fidelity` - both about *generation*. Nothing measures whether the
retriever found the right documents.

* **Golden set** of 30 queries with labelled relevant chunk ids
* **recall@k, MRR, NDCG@10**, baseline committed to the repo
* Every pipeline change measured against it - hybrid and reranking have to
  *earn* their place with numbers, not assertions
* **End to end**: does the recommendation actually change when incident history
  says it should?

---

## Phasing

| # | Work | Days |
|---|---|---|
| 1 | Schema + realistic seed (gates everything) | 2 |
| 2 | Chunking, contextual prefixes, noise filter | 1 |
| 3 | Hybrid + RRF + reranking | 1 |
| 4 | Retrieval into the decision path, cited claims | 1.5 |
| 5 | Golden set + retrieval metrics | 0.5 |

Step 1 cannot be skipped or parallelised. Everything after it is retrieval over
data that does not exist yet.
