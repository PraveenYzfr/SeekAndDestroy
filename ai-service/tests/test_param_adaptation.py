"""Every model callable, oldest to newest, without a table of which wants what.

Older OpenAI models take max_tokens and a temperature. The gpt-5 families reject
max_tokens by name and refuse a temperature by value. Both say so in the 400,
naming the field - so the provider teaches the client its own shape rather than
this repository carrying model knowledge that goes stale.

Fifty-one gpt-5.x ids sat in the account's catalogue and none were callable,
because the payload always sent max_tokens.
"""

from __future__ import annotations

import httpx
import pytest

from app.agents.http_chat_model import (
    _TOKEN_PARAM, _UNSUPPORTED_PARAMS, HttpChatModel,
)

OK_BODY = {
    "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def _model(handler, model="test-model"):
    return HttpChatModel(
        base_url="https://example.test/v1", model=model, api_key="k",
        max_tokens=64, provider_name="test",
        transport=httpx.MockTransport(handler),
    )


def setup_function():
    _TOKEN_PARAM.clear()
    _UNSUPPORTED_PARAMS.clear()


def test_a_model_that_wants_max_completion_tokens_gets_it():
    seen = []

    def handler(request):
        body = __import__("json").loads(request.content)
        seen.append(sorted(k for k in body if "token" in k))
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {"message":
                "Unsupported parameter: 'max_tokens' is not supported with this "
                "model. Use 'max_completion_tokens' instead."}})
        return httpx.Response(200, json=OK_BODY)

    result = _model(handler, "gpt-5").invoke([__import__("langchain_core.messages", fromlist=["HumanMessage"]).HumanMessage(content="hi")])
    assert result.content == "OK"
    assert seen == [["max_tokens"], ["max_completion_tokens"]]


def test_the_limit_is_swapped_not_dropped():
    """Removing the cap entirely would let a reasoning model spend an unbounded
    budget on one answer."""
    from langchain_core.messages import HumanMessage
    captured = {}

    def handler(request):
        body = __import__("json").loads(request.content)
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {"message":
                "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."}})
        captured.update(body)
        return httpx.Response(200, json=OK_BODY)

    _model(handler, "gpt-5.1").invoke([HumanMessage(content="hi")])
    assert captured["max_completion_tokens"] == 64


def test_a_refused_temperature_is_dropped_and_remembered():
    from langchain_core.messages import HumanMessage
    calls = []

    def handler(request):
        body = __import__("json").loads(request.content)
        calls.append("temperature" in body)
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"message":
                "Unsupported value: 'temperature' does not support 0.0 with this model."}})
        return httpx.Response(200, json=OK_BODY)

    llm = _model(handler, "reasoner-1")
    llm.invoke([HumanMessage(content="hi")])
    assert calls == [True, False]
    # Remembered, so the next call does not pay to relearn it.
    llm.invoke([HumanMessage(content="hi")])
    assert calls == [True, False, False]


def test_an_unrelated_400_is_not_retried():
    """A blind retry on 400 turns one real bad request into two."""
    from langchain_core.messages import HumanMessage
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "context_length_exceeded"}})

    with pytest.raises(Exception):
        _model(handler).invoke([HumanMessage(content="hi")])
    assert len(calls) == 1


def test_a_400_naming_messages_or_model_is_a_real_error():
    """Those are not parameters to drop - dropping them would send a request
    that cannot mean anything."""
    from langchain_core.messages import HumanMessage

    def handler(request):
        return httpx.Response(400, json={"error": {"message": "Invalid parameter: 'model'"}})

    with pytest.raises(Exception):
        _model(handler).invoke([HumanMessage(content="hi")])


def test_operator_skip_params_are_never_sent():
    from langchain_core.messages import HumanMessage
    captured = {}

    def handler(request):
        captured.update(__import__("json").loads(request.content))
        return httpx.Response(200, json=OK_BODY)

    llm = HttpChatModel(
        base_url="https://example.test/v1", model="m", api_key="k", max_tokens=8,
        provider_name="test", transport=httpx.MockTransport(handler),
        skip_params={"temperature"},
    )
    llm.invoke([HumanMessage(content="hi")])
    assert "temperature" not in captured


def test_operator_extra_params_are_sent():
    from langchain_core.messages import HumanMessage
    captured = {}

    def handler(request):
        captured.update(__import__("json").loads(request.content))
        return httpx.Response(200, json=OK_BODY)

    llm = HttpChatModel(
        base_url="https://example.test/v1", model="m", api_key="k", max_tokens=8,
        provider_name="test", transport=httpx.MockTransport(handler),
        extra_params={"reasoning_effort": "low"},
    )
    llm.invoke([HumanMessage(content="hi")])
    assert captured["reasoning_effort"] == "low"


def test_a_refused_field_cannot_be_added_back_by_config():
    """skip_params wins over extra_params, because a 400 caused by configuration
    reintroducing a refused field is unexplainable from the settings alone."""
    from langchain_core.messages import HumanMessage
    captured = {}

    def handler(request):
        captured.update(__import__("json").loads(request.content))
        return httpx.Response(200, json=OK_BODY)

    llm = HttpChatModel(
        base_url="https://example.test/v1", model="m", api_key="k", max_tokens=8,
        provider_name="test", transport=httpx.MockTransport(handler),
        extra_params={"top_p": 0.5}, skip_params={"top_p"},
    )
    llm.invoke([HumanMessage(content="hi")])
    assert "top_p" not in captured
