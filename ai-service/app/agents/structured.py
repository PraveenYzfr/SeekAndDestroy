"""Structured-output helper shared by every chain in app/agents.

Two code paths:
- MockChatModel gets the target schema bound directly (see app.agents.mock_llm)
  so it can extract real numbers straight out of the evidence embedded in the
  prompt.
- Any other BaseChatModel goes through a standard PydanticOutputParser with
  format instructions appended to the prompt, with one repair retry on a
  parse failure.

Every chain in app.agents funnels through :func:`run_structured`, which makes
it the one place that can record what was sent to a model and what came back.
Each call writes a sad.AgentAuditLog row - the same table the MCP tools use -
tagged with the investigation and graph node from app.observability.audit_context.

Results are cached (see app.cache.store) keyed by the exact prompt text,
target schema *and the model that produced it* - this only ever caches LLM
*narration*, never a deterministic
number computed elsewhere, so a cache hit is safe: the same evidence would
always have produced the same narration anyway (SAD_LLM__TEMPERATURE
defaults to 0.0). This mainly saves real-provider API calls/latency when the
same candidate/cluster is explained more than once within the cache TTL.
"""

from __future__ import annotations

import hashlib
import json
from typing import TypeVar

import structlog
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel

from app.agents.gemini_chat_model import GeminiChatModel
from app.agents.mock_llm import MockChatModel
from app.cache.store import get_cache_store
from app.config import get_settings
from app.repositories import audit_repository

logger = structlog.get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


def _model_identity(llm: BaseChatModel) -> str:
    """Which model produced a cached answer, for the cache key.

    Read off the instance rather than settings: with SAD_LLM__FALLBACK_PROVIDERS
    the model that actually answered may not be the configured primary, and
    caching a fallback's output under the primary's name would be the same lie
    in a different place.
    """
    provider = getattr(llm, "provider_name", None) or type(llm).__name__
    return f"{provider}:{getattr(llm, 'model', '') or ''}"


def _cache_key(system_prompt: str, human_prompt: str, output_model: type[BaseModel], model_identity: str) -> str:
    """Keyed on the model as well as the prompt.

    Without the model in the key, switching provider and re-running the same
    investigation is a cache *hit*: you get the previous model's text back and
    conclude the two agree, having paid for one call while believing you
    measured two. It fails inside the TTL and works outside it, so it fails
    intermittently - worse than failing always, and fatal for a platform whose
    purpose is comparing models.

    Repeat runs on the *same* model still hit cache, which is the behaviour
    worth keeping: prompts are deterministic and temperature is 0, so paying
    twice buys nothing.
    """
    digest = hashlib.sha256(
        f"{model_identity}\n{output_model.__name__}\n{system_prompt}\n{human_prompt}".encode("utf-8")
    ).hexdigest()
    return f"llm-structured:{output_model.__name__}:{digest}"


#: Audit rows are the substrate for evaluation (app.evaluation), so the cap has
#: to be generous enough to keep a full report prompt intact - a final report
#: carries five scored candidates and runs well past 8 KB.
AUDIT_LIMIT = 64_000


def _audit_payload(model_identity: str, system_prompt: str, human_prompt: str,
                   cache_hit: bool, limit: int = AUDIT_LIMIT) -> str:
    """The prompt record, always as valid JSON.

    Slicing the serialised string was the obvious way to cap this and it was
    wrong twice over: it cut mid-token, so the row no longer parsed and the
    model attribution was lost; and it silently removed evidence, so numbers
    the model had quoted correctly were graded as ungrounded. The evaluation
    harness reported a real provider at 62% entity fidelity on the strength of
    it.

    Truncating the prompts themselves keeps the envelope parseable, and the
    flag tells a grader that this row cannot be judged for fidelity rather
    than letting it judge wrongly.
    """
    payload = {
        "model": model_identity, "cache_hit": cache_hit, "truncated": False,
        "system": system_prompt, "human": human_prompt,
    }
    text = json.dumps(payload, default=str)
    if len(text) <= limit:
        return text

    budget = max(1000, (limit - 500) // 2)
    payload["truncated"] = True
    payload["system"] = system_prompt[:budget]
    payload["human"] = human_prompt[:budget]
    return json.dumps(payload, default=str)


def _audit_start(model_identity: str, system_prompt: str, human_prompt: str,
                 output_model: type[BaseModel], cache_hit: bool) -> int | None:
    """Open an audit row for this model call, or return None if the audit
    write itself failed.

    Deliberately fail-open. This platform produces recommendations and never
    executes an infrastructure change, so losing an investigation to protect
    its log would be the worse trade - but the failure is logged loudly rather
    than swallowed, because "every invocation is audited" stops being true the
    moment one of these is silently skipped.
    """
    from app.observability import audit_context

    scope = audit_context.current()
    try:
        return audit_repository.log_start(
            tool_name=f"llm:{output_model.__name__}"[:100],
            investigation_id=scope.investigation_id,
            graph_node=scope.graph_node,
            input_json=_audit_payload(model_identity, system_prompt, human_prompt, cache_hit),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit.llm_call_start_failed", error=str(exc), schema=output_model.__name__)
        return None


def _audit_complete(audit_id: int | None, *, output_json: str | None, success: bool,
                    error_message: str | None = None) -> None:
    if audit_id is None:
        return
    try:
        audit_repository.log_complete(
            audit_id, output_json=output_json, success=success, error_message=error_message
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit.llm_call_complete_failed", error=str(exc), audit_id=audit_id)


def run_structured(llm: BaseChatModel, system_prompt: str, human_prompt: str, output_model: type[T]) -> T:
    from app.observability.metrics import narration_cache_total

    store = get_cache_store()
    model_identity = _model_identity(llm)
    cache_key = _cache_key(system_prompt, human_prompt, output_model, model_identity)
    cached = store.get(cache_key)
    if cached is not None:
        narration_cache_total.labels(result="hit").inc()
        # Cache hits are audited too. Without them the log has a hole exactly
        # where a question like "what did investigation 74's report actually
        # say?" gets asked - the text was served, so it belongs in the record,
        # flagged as served rather than generated.
        audit_id = _audit_start(model_identity, system_prompt, human_prompt, output_model, cache_hit=True)
        _audit_complete(audit_id, output_json=cached[:AUDIT_LIMIT], success=True)
        return output_model.model_validate_json(cached)
    narration_cache_total.labels(result="miss").inc()

    audit_id = _audit_start(model_identity, system_prompt, human_prompt, output_model, cache_hit=False)
    try:
        parsed = _invoke(llm, system_prompt, human_prompt, output_model)
    except Exception as exc:
        _audit_complete(audit_id, output_json=None, success=False, error_message=str(exc)[:2000])
        raise
    _audit_complete(audit_id, output_json=parsed.model_dump_json()[:AUDIT_LIMIT], success=True)

    store.set(cache_key, parsed.model_dump_json(), ttl_seconds=get_settings().cache.default_ttl_seconds)
    return parsed


def _invoke(llm: BaseChatModel, system_prompt: str, human_prompt: str, output_model: type[T]) -> T:
    # Providers that can enforce the schema server-side do so. Format
    # instructions in a prompt are a request the model may quietly ignore -
    # gemini-flash returned valid JSON for FinalRecommendationReport while
    # dropping a required field, which parses as a failure and costs the whole
    # narration. Native enforcement removes that failure mode instead of
    # retrying it.
    if isinstance(llm, (MockChatModel, GeminiChatModel)):
        bound = llm.bind_response_schema(output_model)
        result = bound.invoke([SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)])
        return output_model.model_validate_json(result.content)

    parser = PydanticOutputParser(pydantic_object=output_model)
    full_human = f"{human_prompt}\n\n{parser.get_format_instructions()}"
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=full_human)]

    result = llm.invoke(messages)
    try:
        return parser.parse(result.content)
    except Exception:
        repair_prompt = (
            f"{full_human}\n\nYour previous response could not be parsed as valid JSON matching the "
            f"required schema. Respond again with ONLY the JSON object, no other text."
        )
        result2 = llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=repair_prompt)])
        return parser.parse(result2.content)
