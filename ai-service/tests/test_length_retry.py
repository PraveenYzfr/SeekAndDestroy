"""A model that runs out of output budget never answered - so retrying it with
"fix your JSON" addresses a failure that did not happen.

c2 measured this against the real provider: "finish_reason=length, 24428 chars of
reasoning" on a narrator prompt, on a budget already raised from 2048 to 8192. It
is not deterministic - the identical call succeeded on retry - which is what makes
raising the ceiling a bet rather than a fix.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage

from app.agents import structured
from app.agents.http_chat_model import EmptyCompletionError


class _RunsOutOnce:
    """Fails the first call the way a real reasoning model does, then answers."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[list] = []

    def invoke(self, messages):
        self.calls.append(messages)
        if len(self.calls) == 1:
            raise EmptyCompletionError(
                "deepseek returned no content (finish_reason=length, 24428 chars of reasoning)"
            )
        return AIMessage(content=self.reply)


def _messages():
    return [SystemMessage(content="You are an infrastructure agent."),
            HumanMessage(content="Summarise the shortlist.")]


def test_a_length_failure_is_retried_once_and_succeeds():
    llm = _RunsOutOnce("the answer")
    result = structured._invoke_once(llm, _messages())
    assert result.content == "the answer"
    assert len(llm.calls) == 2


def test_the_retry_nudges_the_system_prompt_not_the_question():
    """The question was fine. What overran was the model's own reasoning, so the
    instruction belongs in the system prompt - rewriting the human prompt would
    change what was asked."""
    llm = _RunsOutOnce("{}")
    structured._invoke_once(llm, _messages())
    first, second = llm.calls

    assert second[0].content.startswith(first[0].content)
    assert "Reason briefly" in second[0].content
    # The human turn is untouched, verbatim.
    assert second[1].content == first[1].content
    assert "Reason briefly" not in second[1].content


def test_it_retries_once_and_then_gives_up():
    """Not a loop. If brevity does not help, the prompt is too large for the
    model, and burning calls quietly would hide that."""

    class _AlwaysRunsOut:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            raise EmptyCompletionError("finish_reason=length")

    llm = _AlwaysRunsOut()
    with pytest.raises(EmptyCompletionError):
        structured._invoke_once(llm, _messages())
    assert llm.calls == 2


def test_a_working_call_is_not_retried():
    class _Fine:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            return AIMessage(content="ok")

    llm = _Fine()
    assert structured._invoke_once(llm, _messages()).content == "ok"
    assert llm.calls == 1


def test_a_parse_failure_is_not_treated_as_a_length_failure():
    """The two conditions stay separate. A parse failure means the model answered
    and the answer did not fit the schema; the length branch must not swallow it,
    or a schema bug becomes an invisible extra call."""

    class _Raises:
        def invoke(self, messages):
            raise ValueError("could not parse")

    with pytest.raises(ValueError):
        structured._invoke_once(_Raises(), _messages())
