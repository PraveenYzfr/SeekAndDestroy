"""Anthropic's Messages API as a ``BaseChatModel``.

WHY THIS IS A CLASS AND NOT A base_url
--------------------------------------
Groq, DeepSeek and Ollama are all OpenAI-compatible on the wire, so each is a
one-line variant of HttpChatModel. Anthropic is not, in five specific ways, and
every one of them would fail differently if it were bolted onto the OpenAI path:

    endpoint    POST /v1/messages, not /chat/completions
    auth        x-api-key header, not Authorization: Bearer
    version     anthropic-version is REQUIRED; without it the API 400s
    system      a top-level field, NOT a message with role "system"
    content     a LIST of typed blocks, not a string

So it gets its own client, exactly as Gemini did for generateContent - and this
file is deliberately shaped like gemini_chat_model.py so the two read alike.

max_tokens IS REQUIRED by this API. Where OpenAI treats it as an optional
ceiling, Anthropic rejects a request without it, so the settings default is not a
convenience here - it is load-bearing.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.utils.http_retry import arequest_with_retry, request_with_retry

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"

#: Pinned deliberately. The API requires this header and its value selects the
#: request/response contract, so leaving it to a default that moves would change
#: our wire format without a code change.
ANTHROPIC_VERSION = "2023-06-01"


class EmptyCompletionError(RuntimeError):
    """A 200 that carried no text. Named here for the same reason the
    OpenAI-compatible client names its own: passing "" up the stack turns a
    provider-side truncation into a confusing parse failure three frames away."""


def _split_system(messages: list[BaseMessage]) -> tuple[str, list[dict]]:
    """Anthropic takes the system prompt as a top-level field, not as a message.

    A system message left in the array is rejected - the role is not accepted
    there - so this is a translation rather than a convenience. Multiple system
    messages are joined, because the caller's intent is one instruction built
    from parts.
    """
    system_parts: list[str] = []
    turns: list[dict] = []
    for message in messages:
        role = getattr(message, "type", "")
        text = message.content if isinstance(message.content, str) else str(message.content)
        if role == "system":
            system_parts.append(text)
        elif role == "ai":
            turns.append({"role": "assistant", "content": text})
        else:
            turns.append({"role": "user", "content": text})
    if not turns:
        # The API requires at least one message. A prompt that is entirely system
        # text is a real shape - the structured-output path builds one - and
        # sending an empty array 400s.
        turns = [{"role": "user", "content": " ".join(system_parts) or "Continue."}]
    return "\n\n".join(p for p in system_parts if p), turns


def _text_from(content: Any) -> str:
    """Content arrives as a list of typed blocks; text lives in the text ones.

    Anything else - tool_use, thinking - is not prose and is skipped rather than
    stringified, which would put a JSON fragment into a narration.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


class AnthropicChatModel(BaseChatModel):
    api_key: str
    model: str
    base_url: str = DEFAULT_BASE_URL
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout_seconds: int = 60
    provider_name: str = "anthropic"  # metrics label only - not sent on the wire
    transport: Optional[httpx.BaseTransport] = None  # test-only hook
    async_transport: Optional[httpx.AsyncBaseTransport] = None

    @property
    def _llm_type(self) -> str:
        return "anthropic-messages"

    def _prepare(self, messages: list[BaseMessage], stop: Optional[list[str]]):
        system, turns = _split_system(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": turns,
        }
        if system:
            payload["system"] = system
        if stop:
            payload["stop_sequences"] = stop
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        return f"{self.base_url.rstrip('/')}/messages", headers, payload

    def _result_from(self, data: dict) -> ChatResult:
        from app.observability.metrics import llm_calls_total, llm_tokens_total

        text = _text_from(data.get("content"))
        if not text.strip():
            llm_calls_total.labels(provider=self.provider_name, outcome="error").inc()
            raise EmptyCompletionError(
                f"{self.provider_name} returned no text "
                f"(stop_reason={data.get('stop_reason', 'unknown')})"
                + (
                    " - the response hit max_tokens; raise SAD_LLM__MAX_OUTPUT_TOKENS"
                    if data.get("stop_reason") == "max_tokens"
                    else ""
                )
            )
        llm_calls_total.labels(provider=self.provider_name, outcome="success").inc()

        # Anthropic names these input_tokens/output_tokens where the
        # OpenAI-compatible providers say prompt/completion. Recorded under the
        # same metric labels so per-provider cost stays comparable - the whole
        # point of the label is to compare providers, and it cannot if each one
        # files its tokens under its own vocabulary.
        usage = data.get("usage") or {}
        llm_tokens_total.labels(provider=self.provider_name, kind="prompt").inc(
            usage.get("input_tokens") or 0)
        llm_tokens_total.labels(provider=self.provider_name, kind="completion").inc(
            usage.get("output_tokens") or 0)

        meta = {
            "provider": self.provider_name,
            "model": data.get("model") or self.model,
            "prompt_tokens": usage.get("input_tokens"),
            "completion_tokens": usage.get("output_tokens"),
            "stop_reason": data.get("stop_reason"),
        }
        message = AIMessage(content=text, response_metadata=meta)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        from app.observability.metrics import llm_calls_total

        url, headers, payload = self._prepare(messages, stop)
        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = request_with_retry(lambda: client.post(url, headers=headers, json=payload))
            data = response.json()
        except Exception:
            llm_calls_total.labels(provider=self.provider_name, outcome="error").inc()
            raise
        return self._result_from(data)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        from app.observability.metrics import llm_calls_total

        url, headers, payload = self._prepare(messages, stop)
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, transport=self.async_transport
            ) as client:
                response = await arequest_with_retry(
                    lambda: client.post(url, headers=headers, json=payload)
                )
            data = response.json()
        except Exception:
            llm_calls_total.labels(provider=self.provider_name, outcome="error").inc()
            raise
        return self._result_from(data)
