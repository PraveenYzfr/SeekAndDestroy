"""Audit logging for every MCP tool invocation and LangGraph node execution.

Security requirement (spec §20/§14): every MCP tool call is audited. Callers
use :func:`log_start` / :func:`log_complete` as a pair around the call.
"""

from __future__ import annotations

import structlog

from app.repositories.base import T, execute, execute_insert, fetch_all

logger = structlog.get_logger(__name__)


def log_start(
    *, tool_name: str, investigation_id: int | None, graph_node: str | None, input_json: str | None
) -> int:
    return execute_insert(
        T("AgentAuditLog"),
        "AuditId",
        {
            "InvestigationId": investigation_id,
            "GraphNode": graph_node,
            "ToolName": tool_name,
            "InputJson": input_json,
            "OutputJson": None,
            "StartedAt": _now(),
            "CompletedAt": None,
            "Success": None,
            "ErrorMessage": None,
        },
    )


def log_complete(
    audit_id: int, *, output_json: str | None, success: bool, error_message: str | None = None,
    usage: dict | None = None,
) -> None:
    """Close an audit row.

    ``usage`` carries the provider's token counts, normalised by the chat
    models to one vocabulary regardless of whether the provider called them
    ``prompt_tokens`` or ``promptTokenCount``. It is None for the mock model,
    for a cache hit, and for any provider that omits the block - and the
    columns stay NULL in that case rather than 0, because "no tokens recorded"
    and "zero tokens billed" are different facts and averaging them together
    would understate real cost.
    """
    # CompletedAt is computed here in Python (the same clock source as
    # StartedAt in log_start), not via SQL Server's SYSUTCDATETIME(). Mixing
    # two independent clocks for a single "start <= end" invariant risks
    # tripping CK_AgentAuditLog_CompletedAfterStarted under any skew between
    # the app host's and the DB server's clocks, however small - a single
    # clock source makes CompletedAt >= StartedAt true by construction.
    usage = usage or {}
    provider = usage.get("provider")
    model = usage.get("model")
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")

    # Priced on the MODEL alone, not on the "provider:model" identity stored in
    # ModelIdentity. sad.ModelPrice keys on the model name, so looking it up by
    # the combined string would miss every row and quietly record every call as
    # unpriced - a spend report reading zero, with nothing to indicate it was
    # wrong rather than free.
    cost = _price(model, prompt_tokens, completion_tokens)
    _observe_cost(audit_id, provider, model, cost, success)

    execute(
        f"UPDATE {T('AgentAuditLog')} SET OutputJson = :output_json, CompletedAt = :completed_at, "
        f"Success = :success, ErrorMessage = :error_message, PromptTokens = :prompt_tokens, "
        f"CompletionTokens = :completion_tokens, ModelIdentity = :model_identity, "
        f"Provider = :provider, CostUsd = :cost, UnitPriceInput = :unit_in, "
        f"UnitPriceOutput = :unit_out, "
        # Latency from the row's own StartedAt rather than a value passed in: the
        # caller does not always know when the row was opened, and the two
        # timestamps are already guaranteed ordered by CK_AgentAuditLog_CompletedAfterStarted.
        f"LatencyMs = DATEDIFF(millisecond, StartedAt, :completed_at) "
        f"WHERE AuditId = :id",
        {
            "output_json": output_json,
            "completed_at": _now(),
            "success": success,
            "error_message": error_message,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "model_identity": f"{provider}:{model}" if provider else None,
            "provider": provider,
            "cost": cost.cost,
            "unit_in": cost.input_per_million,
            "unit_out": cost.output_per_million,
            "id": audit_id,
        },
    )

    # AFTER the UPDATE, deliberately. This re-reads the row for StartedAt,
    # CompletedAt and ToolName, and CompletedAt is written by the statement
    # above - called before it, the read found NULL, the guard returned, and
    # the metric silently never appeared. Which is exactly how it behaved on
    # first write: no error, no sample, nothing to indicate why.
    _observe_timing(audit_id, provider, model, prompt_tokens, completion_tokens)


def _price(model, prompt_tokens, completion_tokens):
    """Cost of this call, or UNPRICED.

    Wrapped so that pricing can never break audit logging. The audit row is the
    record that a model was called at all; a missing price table, an unseeded
    database or a lookup error must degrade to "cost unknown" rather than losing
    the row entirely. Unknown is already a first-class value here - the daily
    spend view counts unpriced calls separately rather than treating them as
    zero - so falling back to it costs accuracy and not integrity.
    """
    from app.services.model_pricing import UNPRICED, cost_of

    if not model:
        return UNPRICED
    try:
        return cost_of(model, prompt_tokens, completion_tokens)
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning("audit.pricing_failed", model=model)
        return UNPRICED


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)



def _observe_timing(audit_id, provider, model, prompt_tokens, completion_tokens) -> None:
    """Export how long this call took and what it spent, labelled by TASK.

    Emitted here rather than from the three chat-model classes for two reasons.
    It is one site instead of three, so a provider added later cannot forget to
    do it - the same argument _audited makes for graph nodes. And the task is
    only knowable here: a chat model sees messages, not the schema it is filling
    or the node that asked for it.

    The row is re-read to get StartedAt and ToolName. That is one indexed
    primary-key lookup on the completion path of a call that just spent seconds
    talking to a provider, and it avoids threading a start timestamp and a tool
    name through every caller of log_complete.

    Never raises. A metric is a comment on work already done, and this file's
    own rule is that recording what a call cost must not be able to fail it.
    """
    try:
        from app.observability.metrics import llm_duration_seconds, llm_task_tokens_total

        rows = fetch_all(
            f"SELECT ToolName, StartedAt, CompletedAt FROM {T('AgentAuditLog')} WHERE AuditId = :id",
            {"id": audit_id},
        )
        if not rows:
            return
        row = rows[0]
        started, completed = row.get("StartedAt"), row.get("CompletedAt")
        # A cache hit closes with no measurable elapsed time and no provider.
        # Recording it as a 0-second model call would drag every percentile
        # toward zero and describe a call that never happened.
        if started is None or completed is None or not provider:
            return
        seconds = (completed - started).total_seconds()
        if seconds < 0:
            return

        # "llm:FinalRecommendationReport" -> "FinalRecommendationReport". The
        # prefix is a storage detail; a label carrying it would read as noise on
        # every panel and every alert.
        task = str(row.get("ToolName") or "unknown").removeprefix("llm:")
        labels = {"provider": provider, "model": model or "unknown", "task": task}
        llm_duration_seconds.labels(**labels).observe(seconds)
        for kind, count in (("prompt", prompt_tokens), ("completion", completion_tokens)):
            # None means NOT RECORDED, which is not zero - the same distinction
            # the columns themselves keep. A provider that omits the usage block
            # must not look like one that used no tokens.
            if count is not None:
                llm_task_tokens_total.labels(kind=kind, **labels).inc(float(count))
    except Exception:  # noqa: BLE001 - never fail a call to describe it
        pass


def _task_of(audit_id: int) -> str:
    """The task label for an audit row: "llm:JudgeVerdict" -> "JudgeVerdict".

    One indexed primary-key lookup, and _observe_timing does its own because it
    needs StartedAt and CompletedAt from the same row AFTER the UPDATE writes
    them. Two lookups on the completion path of a call that just spent seconds
    talking to a provider is not a cost worth restructuring log_complete to
    avoid - and merging them would mean moving the cost emit after the UPDATE,
    so a failing UPDATE would stop recording spend that was genuinely incurred.

    Never raises, and never returns None. A metric label that is sometimes
    absent splits one series into two, and the gap looks like the task stopped
    running rather than like the lookup failed.
    """
    try:
        rows = fetch_all(
            f"SELECT ToolName FROM {T('AgentAuditLog')} WHERE AuditId = :id", {"id": audit_id}
        )
        if not rows:
            return "unknown"
        return str(rows[0].get("ToolName") or "unknown").removeprefix("llm:")
    except Exception:  # noqa: BLE001 - see docstring
        return "unknown"


def _observe_cost(audit_id: int, provider: str | None, model: str | None, cost,
                  success: bool) -> None:
    """Export what this call cost and that it happened, labelled by TASK.

    Here rather than in a nightly job over the audit table, because a dashboard
    that lags the spend it reports cannot answer the question people actually ask
    of it - "is something expensive happening right now".

    An unpriced model is counted under model="UNPRICED" with zero dollars rather
    than dropped. A spend graph reading zero because a model was unknown is worse
    than one reading low: the second is visibly incomplete.

    WHY TASK IS ON BOTH SERIES. Ranking operations by tokens gets the answer
    wrong. Over one 100-case golden run, generate_final_report took 50.2% of the
    tokens and 33% of the spend; JudgeVerdict took 24.7% of the tokens and 51%
    of the spend, because it runs on a model whose output price is 8.3x the
    cheapest in use. The token counter has carried a task label for a while and
    the cost counter did not, so the cheaper question was answerable from
    metrics and the expensive one was not.

    The call count is the denominator for both. "Expensive because it runs
    often" and "expensive because each run is huge" look identical in a total
    and have opposite fixes.
    """
    try:
        from app.observability.metrics import llm_cost_usd_total, llm_task_calls_total

        task = _task_of(audit_id)
        amount = float(getattr(cost, "cost", 0) or 0)
        priced = getattr(cost, "cost", None) is not None
        llm_cost_usd_total.labels(
            provider=provider or "unknown",
            model=(model or "unknown") if priced else "UNPRICED",
            task=task,
        ).inc(amount)
        # Counted whether or not it was priced, and whether or not it succeeded.
        # A failed call still consumed a provider round trip, and a call counter
        # that silently omits failures makes an outage look like idleness.
        llm_task_calls_total.labels(
            provider=provider or "unknown", model=model or "unknown", task=task,
            outcome="success" if success else "error",
        ).inc()
    except Exception:  # noqa: BLE001 - never fail a call to record what it cost
        pass
