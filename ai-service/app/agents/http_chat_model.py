"""A minimal OpenAI-chat-completions-compatible ``BaseChatModel``.

Deliberately dependency-light: raw ``httpx`` against
``POST {base_url}/chat/completions``, which OpenAI, Azure OpenAI (with the
right base_url/deployment) and Ollama (``/v1/chat/completions``) all speak.
No vendor SDK is required per the technology decisions in
IMPLEMENTATION_PLAN.md §3.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.utils.http_retry import arequest_with_retry, request_with_retry


class EmptyCompletionError(RuntimeError):
    """The provider answered 200 with no usable text. Distinct from a transport
    failure so the fallback chain can tell "provider is down" from "provider
    produced nothing"."""


_ROLE_MAP = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}


def _to_openai_messages(messages: list[BaseMessage]) -> list[dict]:
    out = []
    for m in messages:
        role = _ROLE_MAP.get(m.type, "user")
        content = m.content if isinstance(m.content, str) else str(m.content)
        out.append({"role": role, "content": content})
    return out


class HttpChatModel(BaseChatModel):
    base_url: str
    model: str
    api_key: str = ""
    temperature: float = 0.0
    max_tokens: int = 2048
    timeout_seconds: int = 60
    extra_headers: dict = {}
    provider_name: str = "unknown"  # metrics label only (see app.observability.metrics) - not sent on the wire
    transport: Optional[httpx.BaseTransport] = None  # test-only hook (httpx.MockTransport); None uses real networking
    # Separate async hook: httpx.MockTransport is sync-only and AsyncClient
    # requires an AsyncBaseTransport, so one field cannot serve both. Tests
    # that exercise the async path pass httpx.MockTransport's async twin here.
    async_transport: Optional[httpx.AsyncBaseTransport] = None

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "seekanddestroy-http-openai-compatible"

    # --- shared between the sync and async paths -----------------------------
    # Split out deliberately. The only real difference between _generate and
    # _agenerate is which httpx client makes the call; duplicating the budget
    # check, the payload shape, the empty-content detection and the usage
    # accounting would mean two places to fix every future provider quirk, and
    # they would drift.

    def _prepare(self, messages: list[BaseMessage], stop: Optional[list[str]]):
        from app.config import get_settings
        from app.services.spend_budget import check_and_increment

        check_and_increment("llm_chat", get_settings().llm.daily_call_budget)

        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": _to_openai_messages(messages),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if stop:
            payload["stop"] = stop
        return self.base_url.rstrip("/") + "/chat/completions", headers, payload

    def _result_from(self, data: dict) -> ChatResult:
        from app.observability.metrics import llm_calls_total, llm_tokens_total

        choice = data["choices"][0]
        content = (choice["message"].get("content") or "").strip()
        if not content:
            # Reasoning models (DeepSeek v4, and OpenAI's o-series) put their
            # thinking in a separate `reasoning_content` field, and those tokens
            # count against max_tokens. If the budget runs out mid-thought the
            # API still returns 200, with an empty `content` and
            # finish_reason="length". Passing "" up the stack turns that into a
            # confusing parse failure three frames away, so name it here.
            llm_calls_total.labels(provider=self.provider_name, outcome="error").inc()
            reasoning = (choice["message"].get("reasoning_content") or "").strip()
            raise EmptyCompletionError(
                f"{self.provider_name} returned no content "
                f"(finish_reason={choice.get('finish_reason', 'unknown')}"
                + (f", {len(reasoning)} chars of reasoning - raise SAD_LLM__MAX_OUTPUT_TOKENS" if reasoning else "")
                + ")"
            )
        llm_calls_total.labels(provider=self.provider_name, outcome="success").inc()
        # Every OpenAI-compatible provider returns token counts and we used to
        # throw them away. Without them there is no per-call cost, and a
        # platform whose purpose is comparing providers cannot answer "what did
        # that cost" for any of them.
        usage = data.get("usage") or {}
        llm_tokens_total.labels(provider=self.provider_name, kind="prompt").inc(
            usage.get("prompt_tokens") or 0)
        llm_tokens_total.labels(provider=self.provider_name, kind="completion").inc(
            usage.get("completion_tokens") or 0)
        meta = {
            "provider": self.provider_name,
            "model": self.model,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
        # Set in both places on purpose. llm_output is where LangChain puts
        # run-level metadata, but invoke() returns the message rather than the
        # ChatResult - so llm_output alone is invisible to every caller in this
        # codebase. response_metadata rides on the message and survives.
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=content, response_metadata=meta))],
            llm_output=meta,
        )

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
        """The path that actually matters under load.

        An investigation makes several of these back to back, each waiting
        seconds on a provider. Sync, that wait holds a worker thread doing
        nothing; async, the event loop serves other requests meanwhile. This is
        the whole reason for the async conversion - the LLM call is where the
        time goes.
        """
        from app.observability.metrics import llm_calls_total

        url, headers, payload = self._prepare(messages, stop)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.async_transport) as client:
                response = await arequest_with_retry(
                    lambda: client.post(url, headers=headers, json=payload)
                )
            data = response.json()
        except Exception:
            llm_calls_total.labels(provider=self.provider_name, outcome="error").inc()
            raise
        return self._result_from(data)
