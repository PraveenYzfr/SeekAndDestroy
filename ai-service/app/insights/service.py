"""Top-level entry point: a natural-language analytics question in, a
narrative bounded to an exact SQL result out.

    question -> spec_parser.parse_query_spec (LLM, constrained)
             -> query_builder.run_query (deterministic SQL, the only source of numbers)
             -> narrator.narrate (LLM, bounded to those rows, number-checked)

Every exception this raises - InsightValidationError, NumberDriftError,
InsightNarrationError - is meant to reach the caller, not be swallowed. A
refused question or a rejected narrative is the feature working as designed;
serving a best-effort answer instead would be the wrong number this whole
package exists to prevent.
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from app.insights.narrator import InsightNarrative, narrate
from app.insights.query_builder import run_query
from app.insights.spec_parser import parse_query_spec
from app.models.insights import InsightQuerySpec


def answer_insight_question(
    spec_llm: BaseChatModel, narrator_llm: BaseChatModel, question: str,
) -> dict:
    """Two LLM calls bracket one deterministic query. Separate models are
    accepted (rather than one) because query-spec mapping rewards strict
    vocabulary adherence and narration rewards readable prose - the same
    split app.agents.roles draws between its "extraction" and "narration"
    roles.
    """
    spec = parse_query_spec(spec_llm, question)
    result = run_query(spec)
    narrative = narrate(narrator_llm, question, result)
    return {"spec": spec.model_dump(), "result": result, "narrative": narrative.model_dump()}


def answer_from_spec(narrator_llm: BaseChatModel, question: str, spec: InsightQuerySpec) -> dict:
    """Same as answer_insight_question but skips NL parsing - for a caller
    (a test, a fixed acceptance-case query) that already knows the exact spec
    it wants run, and to keep query-layer correctness testable independently
    of an LLM's ability to map free text onto one.
    """
    result = run_query(spec)
    narrative = narrate(narrator_llm, question, result)
    return {"spec": spec.model_dump(), "result": result, "narrative": narrative.model_dump()}


__all__ = ["answer_insight_question", "answer_from_spec", "InsightNarrative"]
