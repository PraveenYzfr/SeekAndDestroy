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

count_routing_total = Counter(
    "sad_count_routing_total",
    "Estate count questions ('how many servers') routed away from the graph, by outcome",
    # deterministic  answered from SQL with no model call at all
    # parsed         answered via the spec parser (two model calls)
    # refused        the parser declined and the reader got a usable explanation
    # fell_through   nothing here could answer, the graph ran as it did before
    #
    # fell_through is the one that matters and it is why this counter exists:
    # that path returns the reader to the OLD bad answer, so a regression here
    # is indistinguishable from the original defect. Without a counter the only
    # trace is a log line, and container logs on this platform are destroyed by
    # every deploy.
    ["outcome"],
)

judge_not_applicable_total = Counter(
    "sad_judge_not_applicable_total",
    "Answers deliberately not graded because no investigation stood behind them",
    # A greeting, a capability refusal, a recall, an estate count. None of them
    # narrates evidence, so groundedness is not merely unmeasured - it does not
    # apply.
    #
    # SEPARATE FROM judge_failures_total ON PURPOSE. These used to be counted
    # as reason="no_evidence" failures, which made correct refusals read as a
    # broken judge and put them under JudgeNotProducingVerdicts. A failure is
    # something that should have worked; this is something that was never
    # asked. Filtering them out inside the alert would have hidden them from
    # every other reader too - a rise here is real signal, it means more of
    # what the platform says is being intercepted before it investigates.
    ["kind"],
)

#: HOW LONG A MODEL CALL TOOK, which nothing measured until now.
#:
#: Before this there were exactly two Histograms in the platform - fidelity_score
#: and judge_score - and both are quality. Nothing timed a call. Latency lived in
#: AgentAuditLog.LatencyMs, queryable in SQL and invisible in Grafana, so "what is
#: our p95" could only be answered by someone with a database prompt open.
#:
#: LABELLED BY TASK, which is the label the token counter still lacks and the
#: reason this is worth more than a single global timer. Measured over two days,
#: the tasks differ by more than 4x:
#:
#:     FinalRecommendationReport   avg 2.5s   max 7.0s   avg 16,864 prompt tokens
#:     JudgeVerdict                avg 1.6s   max 2.0s   avg  1,187
#:     CandidateExplanation        avg 1.0s   max 1.2s   avg  2,067
#:     RightSizingExplanation      avg 0.6s   max 0.7s   avg    914
#:
#: A single number over all of them describes none of them.
#:
#: Buckets run to 120s deliberately. A cold investigation was taking 150 seconds
#: a week ago, and buckets that stop at 10 would have put every one of those in
#: +Inf and reported nothing about the problem being fixed.
#:
#: Cardinality: ~6 providers x ~10 models x ~8 task shapes, all bounded by
#: configuration rather than by user input.
llm_duration_seconds = Histogram(
    "sad_llm_duration_seconds",
    "Wall-clock duration of a completed model call, by provider, model and task",
    ["provider", "model", "task"],
    buckets=(0.25, 0.5, 1, 2, 3, 5, 8, 13, 21, 34, 60, 120),
)

#: THE SAME CALL'S TOKENS, LABELLED THE SAME WAY.
#:
#: sad_llm_tokens_total carries {provider, kind} only, so "which operation burns
#: tokens" is unanswerable from it - and sad_llm_cost_usd_total already carries
#: {provider, model}, which made the two inconsistent: spend was sliceable by
#: model and the tokens underneath it were not.
#:
#: A SECOND SERIES RATHER THAN A RELABELLING of the original. Adding labels to a
#: live counter resets it and silently rebases every rate() and increase() that
#: reads it, including the ones on the dashboard. This one is emitted from the
#: audit-completion path where the task is known; the original keeps emitting
#: from the model classes, where it is not.
llm_task_tokens_total = Counter(
    "sad_llm_task_tokens_total",
    "Tokens consumed by a completed model call, by provider, model, task and kind",
    ["provider", "model", "task", "kind"],
)

#: END TO END, WHICH IS WHAT A PERSON ACTUALLY WAITS FOR.
#:
#: Model time is only part of it and frequently the smaller part. Investigation
#: 132 took 59.7 seconds wall with 6.9 seconds of model time; investigation 49
#: took 30 seconds wall with 0.1. Timing only the model calls would have reported
#: both as fast.
#:
#: Labelled by type because a Count answers in milliseconds and a Hosting run
#: does real work, and mixing them makes a percentile that describes neither.
investigation_duration_seconds = Histogram(
    "sad_investigation_duration_seconds",
    "Wall-clock duration of a whole investigation, from request to answer",
    ["investigation_type"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 90, 120, 180, 300),
)

#: THE ENTERPRISE HALLUCINATION NUMBER: did this answer contain ANY claim that
#: could not be traced to the evidence the model was given.
#:
#: One counter, one question, a stated denominator. The existing panel called
#: "Hallucination rate" divides sad_narration_drift_total{outcome="drift"} by its
#: total - and outcome="drift" has NEVER had a single series, because
#: assert_no_number_drift BLOCKS: a drifted figure raises and the answer never
#: ships. That panel can only ever draw a flat line at zero. It measures a guard
#: that cannot fail, not an outcome.
#:
#: Meanwhile the failure that actually happened was invisible to it. Cluster
#: msp-p194 - which has ZERO incidents - was told about four belonging to
#: msp-p204 and dal-p044. Every figure was real and every code appeared in the
#: evidence, so number_fidelity, entity_fidelity and the drift guard all passed.
#:
#: FOUR OUTCOMES, and the third is the one every previous version of this got
#: wrong by folding into one of the others:
#:
#:     clean          every applicable check scored 1.0
#:     hallucinated   at least one applicable check scored below 1.0
#:     not_applicable nothing was checkable - a greeting, a refusal, a count,
#:                    or evidence that can ground nothing. NOT a pass and NOT a
#:                    failure, and it must stay out of both halves of the rate.
#:     ungradeable    a truncated prompt or unrecoverable evidence. The answer
#:                    may be perfect or invented and this platform cannot say
#:                    which - which is a different thing from having nothing to
#:                    check.
#:
#: So the rate a reader should quote is
#:     hallucinated / (hallucinated + clean)
#: and the honest panel shows the denominator beside it, because 3% of 400 and
#: 3% of 4 are different statements.
hallucination_total = Counter(
    "sad_hallucination_total",
    "Delivered answers by whether any claim failed to trace to its evidence",
    ["outcome"],
)

#: WHICH check caught it, so a rise is diagnosable rather than merely alarming.
#: An answer failing several increments several - this counts CHECKS FAILED, not
#: answers, and must never be used as the numerator of the rate above.
hallucination_by_check_total = Counter(
    "sad_hallucination_by_check_total",
    "Individual grader checks that failed on a delivered answer, by check",
    ["check"],
)


#: Queries that tried to talk the platform out of its own rules, refused before
#: anything ran. Labelled by WHICH SHAPE was recognised, not by the query text -
#: the text is attacker-controlled and would be unbounded cardinality, and the
#: shape is the part an operator can act on: a rise in `role_reassignment` and a
#: rise in `disregard_instructions` are different campaigns.
#:
#: ABSENT IS NOT ZERO here, and this counter is more prone to it than most. A
#: labelled Counter emits no series at all until the first increment, and the
#: healthy state of this one is *never having fired*. So a panel over it needs
#: `or vector(0)` or a quiet platform and a missing code path render identically
#: - see the same defect on the judge panels in the plan.
override_framing_total = Counter(
    "sad_override_framing_total",
    "Queries refused for trying to override the platform's own instructions, by shape",
    ["shape"],
)
