"""Anthropic's Messages API, which is not OpenAI-compatible in five ways.

Groq, DeepSeek and Ollama are each a base_url variant of one client because they
speak the OpenAI wire format. Anthropic does not, and every difference fails
differently if it is bolted onto that path:

    endpoint    /v1/messages, not /chat/completions
    auth        x-api-key, not Authorization: Bearer
    version     anthropic-version REQUIRED - the API 400s without it
    system      a top-level field, NOT a message with role "system"
    content     a LIST of typed blocks, not a string

Driven through httpx.MockTransport, so these assert the REAL request this client
would put on the wire without making a call or needing a key.
"""

from __future__ import annotations

import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.anthropic_chat_model import (
    AnthropicChatModel,
    EmptyCompletionError,
    _split_system,
    _text_from,
)


def _model(handler, **kw):
    return AnthropicChatModel(
        api_key="sk-test", model="claude-sonnet-5",
        transport=httpx.MockTransport(handler), **kw
    )


def _ok(payload):
    def handler(request):
        payload["url"] = str(request.url)
        payload["headers"] = dict(request.headers)
        payload["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "model": "claude-sonnet-5", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "atl-03 is recommended."}],
            "usage": {"input_tokens": 120, "output_tokens": 8},
        })
    return handler


class TestTheWireFormat:
    def test_it_posts_to_messages_not_chat_completions(self):
        seen = {}
        _model(_ok(seen)).invoke([HumanMessage(content="Where?")])
        assert seen["url"].endswith("/v1/messages")

    def test_it_authenticates_with_x_api_key_not_bearer(self):
        seen = {}
        _model(_ok(seen)).invoke([HumanMessage(content="Where?")])
        assert seen["headers"]["x-api-key"] == "sk-test"
        assert "authorization" not in seen["headers"]

    def test_it_sends_the_version_header(self):
        """Required. Without it the API rejects the request outright."""
        seen = {}
        _model(_ok(seen)).invoke([HumanMessage(content="Where?")])
        assert seen["headers"]["anthropic-version"] == "2023-06-01"

    def test_the_system_prompt_is_hoisted_out_of_the_messages(self):
        seen = {}
        _model(_ok(seen)).invoke([
            SystemMessage(content="You are an infra agent."),
            HumanMessage(content="Where?"),
        ])
        assert seen["body"]["system"] == "You are an infra agent."
        assert all(m["role"] != "system" for m in seen["body"]["messages"])

    def test_max_tokens_is_always_sent(self):
        """Optional for OpenAI, required here - the request 400s without it."""
        seen = {}
        _model(_ok(seen)).invoke([HumanMessage(content="Where?")])
        assert "max_tokens" in seen["body"]


class TestMessageTranslation:
    def test_assistant_turns_keep_their_role(self):
        system, turns = _split_system([
            SystemMessage(content="S"), HumanMessage(content="H"), AIMessage(content="A"),
        ])
        assert system == "S"
        assert turns == [{"role": "user", "content": "H"}, {"role": "assistant", "content": "A"}]

    def test_a_system_only_prompt_still_gets_a_message(self):
        """The API requires at least one message, and a prompt that is entirely
        system text is a real shape - the structured-output path builds one."""
        system, turns = _split_system([SystemMessage(content="Only this.")])
        assert system == "Only this."
        assert len(turns) == 1 and turns[0]["role"] == "user"

    def test_several_system_messages_join(self):
        system, _ = _split_system([SystemMessage(content="A"), SystemMessage(content="B")])
        assert system == "A\n\nB"


class TestResponseParsing:
    def test_text_is_read_out_of_the_block_list(self):
        assert _text_from([{"type": "text", "text": "hello"}]) == "hello"

    def test_non_text_blocks_are_skipped_not_stringified(self):
        """A thinking or tool_use block turned into a string would put a JSON
        fragment into a narration."""
        blocks = [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "answer"}]
        assert _text_from(blocks) == "answer"

    def test_token_counts_are_recorded_under_the_shared_vocabulary(self):
        """Anthropic says input/output where the others say prompt/completion.
        Filed under the same names, or per-provider cost cannot be compared -
        which is the only reason the provider label exists."""
        seen = {}
        result = _model(_ok(seen)).invoke([HumanMessage(content="Where?")])
        assert result.response_metadata["prompt_tokens"] == 120
        assert result.response_metadata["completion_tokens"] == 8

    def test_an_empty_response_raises_rather_than_returning_nothing(self):
        def handler(request):
            return httpx.Response(200, json={"content": [], "stop_reason": "max_tokens", "usage": {}})
        with pytest.raises(EmptyCompletionError) as exc:
            _model(handler).invoke([HumanMessage(content="Where?")])
        assert "max_tokens" in str(exc.value), "the message must say which knob to turn"
