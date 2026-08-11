"""Structured-output helper shared by every chain in app/agents.

Two code paths:
- MockChatModel gets the target schema bound directly (see app.agents.mock_llm)
  so it can extract real numbers straight out of the evidence embedded in the
  prompt.
- Any other BaseChatModel goes through a standard PydanticOutputParser with
  format instructions appended to the prompt, with one repair retry on a
  parse failure.

Results are cached (see app.cache.store) keyed by the exact prompt text and
target schema - this only ever caches LLM *narration*, never a deterministic
number computed elsewhere, so a cache hit is safe: the same evidence would
always have produced the same narration anyway (SAD_LLM__TEMPERATURE
defaults to 0.0). This mainly saves real-provider API calls/latency when the
same candidate/cluster is explained more than once within the cache TTL.
"""

from __future__ import annotations

import hashlib
from typing import TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel

from app.agents.gemini_chat_model import GeminiChatModel
from app.agents.mock_llm import MockChatModel
from app.cache.store import get_cache_store
from app.config import get_settings

T = TypeVar("T", bound=BaseModel)


def _cache_key(system_prompt: str, human_prompt: str, output_model: type[BaseModel]) -> str:
    digest = hashlib.sha256(f"{output_model.__name__}\n{system_prompt}\n{human_prompt}".encode("utf-8")).hexdigest()
    return f"llm-structured:{output_model.__name__}:{digest}"


def run_structured(llm: BaseChatModel, system_prompt: str, human_prompt: str, output_model: type[T]) -> T:
    from app.observability.metrics import narration_cache_total

    store = get_cache_store()
    cache_key = _cache_key(system_prompt, human_prompt, output_model)
    cached = store.get(cache_key)
    if cached is not None:
        narration_cache_total.labels(result="hit").inc()
        return output_model.model_validate_json(cached)
    narration_cache_total.labels(result="miss").inc()

    parsed = _invoke(llm, system_prompt, human_prompt, output_model)
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
