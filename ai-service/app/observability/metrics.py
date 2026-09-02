"""Platform-specific Prometheus metrics, scraped at ``GET /metrics``
(wired up in app.main via prometheus-fastapi-instrumentator, which also adds
the standard HTTP request-rate/latency/status metrics automatically).

Two kinds of signal live here, and the split is deliberate.

OPERATIONAL - what an on-call engineer needs mid-incident. Is a provider failing
or falling back, is the cache doing anything, is the budget about to bite, is
volume normal.

QUALITY - whether the answers were any good. This half did not exist. The guards
that catch a fabricated figure fired live on every request and were recorded only
as structured log lines, so "how often does this platform state a number it was
never given" was answerable by grepping logs and by nothing else. The checks were
real; the signal was invisible.

That is the gap these metrics close. Nothing here RECOMPUTES a verdict - each
counter is emitted at the point where an existing check already reached one, so
the dashboard and the runtime can never disagree about what happened.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

llm_calls_total = Counter(
    "sad_llm_calls_total", "Real (non-mock) LLM chat-model calls attempted", ["provider", "outcome"]
)

#: Tokens billed, split prompt vs completion and labelled by provider. The call
#: counter answers "how often"; this answers "how much", which is the axis that
#: actually differs between providers - a reasoning model can spend ten times
#: the tokens of a non-reasoning one on the same prompt.
llm_tokens_total = Counter(
    "sad_llm_tokens_total", "Tokens consumed by real LLM calls", ["provider", "kind"]
)
llm_fallback_total = Counter(
    "sad_llm_fallback_total", "Times the LLM fallback chain moved on to the next configured provider",
    ["from_provider"],
)
embedding_calls_total = Counter(
    "sad_embedding_calls_total", "Real (non-hash) embedding provider calls attempted", ["provider", "outcome"]
)
narration_cache_total = Counter(
    "sad_narration_cache_total", "LLM narration cache lookups in app.agents.structured.run_structured",
    ["result"],  # hit | miss
)
budget_denied_total = Counter(
    "sad_budget_denied_total", "Calls denied by a SAD_LLM__DAILY_CALL_BUDGET / "
    "SAD_RETRIEVAL__EMBEDDING_DAILY_CALL_BUDGET spend guardrail", ["namespace"],
)
investigations_total = Counter(
    "sad_investigations_total", "Investigations created, by type", ["investigation_type"]
)


# =============================================================================
# Quality - was the answer any good, not just how many were there
# =============================================================================

#: Narrations checked by assert_no_number_drift, and whether the check passed.
#:
#: This is the hallucination rate in its most load-bearing form: outcome="drift"
#: means a model stated a figure that was not in the evidence it was given, and
#: the platform refused the narration rather than showing it. It was already
#: happening on every request; it was simply not counted, so the answer to "how
#: often" was a log grep.
#:
#: Emitted inside the guard rather than at its five call sites - a counter that
#: has to be remembered at each caller is a counter that will be missed at one.
narration_drift_total = Counter(
    "sad_narration_drift_total",
    "Narrations checked for number drift, by schema and outcome (ok|drift)",
    ["schema", "outcome"],
)

#: Money, as a counter rather than a gauge, so rate() gives spend per second and
#: increase() gives spend over a window.
#:
#: Priced from sad.ModelPrice at the moment of the call, which matters because a
#: price change should not retroactively rewrite what last month cost. Calls whose
#: model has no price row are counted under model="UNPRICED" instead of being
#: dropped - a spend dashboard reading zero because a model was unknown is worse
#: than one reading low, since the second is visibly incomplete.
llm_cost_usd_total = Counter(
    "sad_llm_cost_usd_total", "Cost in USD of completed model calls", ["provider", "model"]
)

#: The deterministic graders, as a distribution rather than a mean.
#:
#: A mean fidelity of 0.97 can be 97 perfect answers and 3 broken ones, or 100
#: answers each with a wrong figure. Those need different responses and a single
#: number cannot tell them apart. Buckets are dense near 1.0 because that is
#: where the interesting movement is - the difference between 0.99 and 1.0 is a
#: fabricated number in one answer out of a hundred.
fidelity_score = Histogram(
    "sad_fidelity_score",
    "Deterministic grader scores over model prose, by grader",
    ["grader"],
    buckets=(0.5, 0.8, 0.9, 0.95, 0.98, 0.99, 0.995, 1.0),
)

#: The LLM judge, by the dimension it scored.
#:
#: Deliberately separate from fidelity_score. The graders are arithmetic and the
#: judge is an opinion, and averaging the two would produce a "quality score" that
#: cannot be acted on - a drop could mean a fabricated figure or a model being
#: less chatty, and only one of those is an incident.
judge_score = Histogram(
    "sad_judge_score",
    "LLM-as-judge scores, by dimension (relevance|groundedness|actionability)",
    ["dimension"],
    buckets=(1, 2, 3, 4, 5),
)

#: Judge verdicts that could not be produced. A judge that silently stops running
#: leaves a dashboard showing the last good score forever, which reads as health.
judge_failures_total = Counter(
    "sad_judge_failures_total", "Judge invocations that failed, by reason", ["reason"]
)


#: Verdicts that were PRODUCED and deliberately not exported.
#:
#: This exists because the exclusion rule silently ate every score. Every role
#: defaults to the same model, so the judge is the author, every verdict is
#: self-judged, and judge_score is never observed at all - leaving the dashboard
#: panels empty and indistinguishable from a judge that was never wired up.
#:
#: Excluding a self-graded score from the headline is right: a model grades its
#: own work high, and averaging that with independent verdicts produces a line
#: nobody can read. Excluding it INVISIBLY is not - "no data" and "47 verdicts,
#: all disqualified" call for completely different actions, and the panel showed
#: the same thing for both.
#:
#: So the exclusion is now counted. An empty judge panel beside a rising
#: exclusion count says exactly what is wrong and what to do about it: point the
#: judge role at a different provider.
judge_excluded_total = Counter(
    "sad_judge_excluded_total",
    "Judge verdicts produced but excluded from headline scores, by reason",
    ["reason"],
)


#: Verdicts that were computed and could not be stored.
#:
#: The write is deliberately best-effort - it grades an answer that has already
#: been handed to a user, so failing to store a comment must never turn a
#: completed investigation into an error. That is right, and it made a real
#: failure invisible: migrations 018 and 019 were deployed without INSERT grants
#: (the schema-wide grant covers SELECT only), so every write failed, every
#: failure was logged at warning, and the platform reported nothing wrong. It was
#: found by someone checking the table by hand.
#:
#: A swallowed exception needs a counter for exactly this reason. "Best effort"
#: describes what the code should do about a failure, not whether anyone should
#: be told it happened.
evaluation_persist_failures_total = Counter(
    "sad_evaluation_persist_failures_total",
    "Evaluation verdicts that were computed but could not be stored",
    ["table"],
)


#: Failures the graph used to drop, now enqueued - and the ones that could not
#: be enqueued.
#:
#: The "lost" outcome is the point of having a label at all. A queue that
#: quietly fails to record failures reproduces the exact bug it exists to fix,
#: and that is not hypothetical: sad.AnswerEvaluation sat empty for hours behind
#: a missing INSERT grant while every write appeared to succeed, because the
#: repository swallows write errors by design so a verdict cannot break a
#: delivered answer.
remediation_enqueued_total = Counter(
    "sad_remediation_enqueued_total",
    "Graph failures enqueued for remediation, by drop site and whether the row was stored",
    ["site", "outcome"],
)
