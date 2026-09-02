"""Gemini chat client.

Gemini's wire format differs from the OpenAI-compatible one in four ways, each
of which fails quietly rather than loudly if it is got wrong - a system prompt
sent as a normal turn is simply treated as user text, an "assistant" role is
silently not "model". These tests pin all four against httpx.MockTransport, so
they run with no network and no API spend.

The live-model test is separate and skipped unless a real key is configured.
"""

from __future__ import annotations

import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.gemini_chat_model import (
    DEFAULT_MODEL,
    GeminiChatModel,
    GeminiResponseError,
    _to_gemini_contents,
)


def _mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _ok(text: str = "hello"):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.request = request
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})

    return handler


# =============================================================================
# Message translation
# =============================================================================


def test_assistant_turns_use_the_model_role_not_assistant():
    """Gemini has no "assistant" role. Sending one makes the turn ambiguous."""
    contents, _ = _to_gemini_contents([HumanMessage(content="hi"), AIMessage(content="hello")])
    assert [c["role"] for c in contents] == ["user", "model"]


def test_system_prompts_go_to_system_instruction_not_into_contents():
    """A system message sent as an ordinary turn is read as user text, which
    silently weakens every prompt in the platform.
    """
    contents, system = _to_gemini_contents(
        [SystemMessage(content="be terse"), HumanMessage(content="hi")]
    )
    assert len(contents) == 1 and contents[0]["role"] == "user"
    assert system == {"parts": [{"text": "be terse"}]}


def test_multiple_system_messages_are_merged():
    """Gemini accepts exactly one systemInstruction."""
    _contents, system = _to_gemini_contents(
        [SystemMessage(content="a"), SystemMessage(content="b"), HumanMessage(content="hi")]
    )
    assert system["parts"][0]["text"] == "a\n\nb"


def test_no_system_message_means_no_system_instruction_field():
    _contents, system = _to_gemini_contents([HumanMessage(content="hi")])
    assert system is None


# =============================================================================
# Wire format
# =============================================================================


def test_auth_uses_the_google_api_key_header_not_bearer():
    handler = _ok()
    model = GeminiChatModel(api_key="secret-key", transport=_mock(handler))
    model.invoke([HumanMessage(content="hi")])

    assert handler.request.headers["x-goog-api-key"] == "secret-key"
    assert "authorization" not in {k.lower() for k in handler.request.headers}


def test_url_targets_generate_content_on_the_configured_model():
    handler = _ok()
    model = GeminiChatModel(api_key="k", model="gemini-pro-latest", transport=_mock(handler))
    model.invoke([HumanMessage(content="hi")])
    assert str(handler.request.url).endswith("/models/gemini-pro-latest:generateContent")


def test_a_fully_qualified_model_name_is_not_double_prefixed():
    handler = _ok()
    model = GeminiChatModel(api_key="k", model="models/gemini-pro-latest", transport=_mock(handler))
    model.invoke([HumanMessage(content="hi")])
    assert "/models/models/" not in str(handler.request.url)


def test_generation_config_carries_temperature_and_token_cap():
    handler = _ok()
    model = GeminiChatModel(api_key="k", temperature=0.25, max_tokens=99, transport=_mock(handler))
    model.invoke([HumanMessage(content="hi")], stop=["END"])

    body = json.loads(handler.request.content)
    assert body["generationConfig"]["temperature"] == 0.25
    assert body["generationConfig"]["maxOutputTokens"] == 99
    assert body["generationConfig"]["stopSequences"] == ["END"]


def test_response_text_is_returned():
    model = GeminiChatModel(api_key="k", transport=_mock(_ok("the answer")))
    assert model.invoke([HumanMessage(content="hi")]).content == "the answer"


def test_multi_part_responses_are_concatenated():
    """Gemini 3 models return extra parts (thought signatures); the text parts
    must be joined rather than only the first one taken.
    """

    def handler(request):
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "one "}, {"text": "two"}]}}]}
        )

    model = GeminiChatModel(api_key="k", transport=_mock(handler))
    assert model.invoke([HumanMessage(content="hi")]).content == "one two"


# =============================================================================
# The 200-with-no-answer cases
# =============================================================================


def test_a_blocked_prompt_raises_a_clear_error():
    """Gemini answers 200 with no candidates when a prompt is filtered.
    Indexing into candidates[0] would raise IndexError three frames away and
    look like a transport bug.
    """

    def handler(request):
        return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})

    model = GeminiChatModel(api_key="k", transport=_mock(handler))
    with pytest.raises(GeminiResponseError, match="SAFETY"):
        model.invoke([HumanMessage(content="hi")])


def test_an_empty_candidate_reports_its_finish_reason():
    def handler(request):
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}
        )

    model = GeminiChatModel(api_key="k", transport=_mock(handler))
    with pytest.raises(GeminiResponseError, match="MAX_TOKENS"):
        model.invoke([HumanMessage(content="hi")])


# =============================================================================
# Factory wiring
# =============================================================================


def test_gemini_is_a_selectable_provider():
    from typing import get_args

    from app.config.settings import LlmSettings

    assert "gemini" in get_args(LlmSettings.model_fields["provider"].annotation)


def test_factory_builds_a_gemini_model_and_refuses_without_a_key(monkeypatch):
    from app.agents import llm_factory

    monkeypatch.setenv("SAD_LLM__PROVIDER", "gemini")
    monkeypatch.setenv("SAD_LLM__API_KEY", "test-key")
    monkeypatch.setenv("SAD_LLM__MODEL", "gemini-flash-latest")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        model = llm_factory.build_chat_model_for_provider("gemini")
        assert isinstance(model, GeminiChatModel)
        assert model.model == "gemini-flash-latest"

        monkeypatch.setenv("SAD_LLM__API_KEY", "")
        # Clear the per-provider slot too. Credentials stopped being one field
        # when two providers had to run at once, so emptying api_key alone no
        # longer produces "no credential" - the .env supplies
        # PROVIDER_KEYS__GEMINI and the factory correctly finds it.
        monkeypatch.setenv("SAD_LLM__PROVIDER_KEYS__GEMINI", "")
        get_settings.cache_clear()
        with pytest.raises(ValueError, match="SAD_LLM__API_KEY"):
            llm_factory.build_chat_model_for_provider("gemini")
    finally:
        get_settings.cache_clear()


def test_the_mock_model_name_is_never_sent_to_gemini(monkeypatch):
    """SAD_LLM__MODEL defaults to "seek-and-destroy-mock". Forwarding that to
    Gemini 404s in a way that reads like an auth failure, so the factory
    substitutes a real default instead.
    """
    from app.agents import llm_factory
    from app.config import get_settings

    monkeypatch.setenv("SAD_LLM__PROVIDER", "gemini")
    monkeypatch.setenv("SAD_LLM__API_KEY", "test-key")
    monkeypatch.setenv("SAD_LLM__MODEL", "seek-and-destroy-mock")
    get_settings.cache_clear()
    try:
        assert llm_factory.build_chat_model_for_provider("gemini").model == DEFAULT_MODEL
    finally:
        get_settings.cache_clear()


def test_default_model_is_an_alias_not_a_pinned_version():
    """Pinned Gemini versions get closed to new keys and 404 with "no longer
    available to new users" - which is indistinguishable from a bad key.
    """
    assert DEFAULT_MODEL.endswith("-latest")
