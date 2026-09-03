"""A length overflow is not a provider outage, and must not be treated as one.

Measured on the 100-case golden run: 16 calls returned no content at all, with
23,881 to 37,842 characters of reasoning against an 8,192 token budget. Each one
fell through the ENTIRE provider chain - four sequential calls for one recoverable
failure - and the brevity retry in app.agents.structured fired zero times,
because this loop sits inside the model that structured calls.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.http_chat_model import EmptyCompletionError
from app.agents.llm_factory import FallbackChatModel, _with_brevity


def _result(text: str) -> ChatResult:
    return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


class _Member(BaseChatModel):
    """One provider. Overflows a given number of times, then answers.

    A real BaseChatModel rather than a bare stub: FallbackChatModel.members is
    typed list[tuple[str, BaseChatModel]] and pydantic enforces it, so a duck
    would be rejected at construction and the test would prove nothing about the
    real chain.
    """

    name: str = "member"
    overflows: int = 0
    always_fails: bool = False
    calls: list = []

    @property
    def _llm_type(self) -> str:
        return "test-member"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(messages)
        if self.always_fails:
            raise RuntimeError(f"{self.name} unavailable")
        if len(self.calls) <= self.overflows:
            raise EmptyCompletionError(
                f"{self.name} returned no content (finish_reason=length, 34663 chars of reasoning)"
            )
        return _result(f"answered by {self.name}")


def _chain(*members) -> FallbackChatModel:
    return FallbackChatModel(members=[(m.name, m) for m in members])


def _msgs():
    return [SystemMessage(content="You are an infrastructure agent."),
            HumanMessage(content="Summarise the shortlist.")]


def test_an_overflow_is_retried_on_the_same_provider_before_falling_through():
    """The whole point. One overflow used to cost four providers."""
    primary = _Member(name="deepseek", overflows=1, calls=[])
    backup = _Member(name="openai", calls=[])
    result = _chain(primary, backup)._generate(_msgs())

    assert result.generations[0].message.content == "answered by deepseek"
    assert len(primary.calls) == 2, "primary should be retried once"
    assert backup.calls == [], "the backup must not be touched when the retry succeeds"


def test_the_retry_asks_for_brevity_on_the_system_turn_only():
    """The question was fine; what overran is the model's own reasoning."""
    primary = _Member(name="deepseek", overflows=1, calls=[])
    _chain(primary, _Member(name="openai", calls=[]))._generate(_msgs())

    first, second = primary.calls
    assert second[0].content.startswith(first[0].content)
    assert "Reason briefly" in second[0].content
    assert second[1].content == first[1].content


def test_it_falls_through_when_brevity_does_not_help():
    """One retry, not a loop. If being brief does not fix it, the prompt is too
    large for this model and burning calls would hide that."""
    primary = _Member(name="deepseek", overflows=99, calls=[])
    backup = _Member(name="openai", calls=[])
    result = _chain(primary, backup)._generate(_msgs())

    assert result.generations[0].message.content == "answered by openai"
    assert len(primary.calls) == 2, "exactly one retry, then move on"


def test_an_ordinary_failure_is_not_retried():
    """A provider being unavailable is a different condition. Retrying it wastes
    a call on something that cannot succeed - which is the mirror of the bug
    being fixed here."""
    primary = _Member(name="deepseek", always_fails=True, calls=[])
    backup = _Member(name="openai", calls=[])
    result = _chain(primary, backup)._generate(_msgs())

    assert result.generations[0].message.content == "answered by openai"
    assert len(primary.calls) == 1, "no retry for a non-length failure"


def test_a_working_provider_is_called_once():
    primary = _Member(name="deepseek", calls=[])
    result = _chain(primary, _Member(name="openai", calls=[]))._generate(_msgs())
    assert result.generations[0].message.content == "answered by deepseek"
    assert len(primary.calls) == 1


def test_brevity_is_added_even_with_no_system_turn():
    """A retry that changes nothing is a second identical failure and a doubled
    bill."""
    out = _with_brevity([HumanMessage(content="Q")])
    assert isinstance(out[0], SystemMessage)
    assert "Reason briefly" in out[0].content
