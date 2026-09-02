"""A minimal OpenAI-chat-completions-compatible ``BaseChatModel``.

Deliberately dependency-light: raw ``httpx`` against
``POST {base_url}/chat/completions``, which OpenAI, Azure OpenAI (with the
right base_url/deployment) and Ollama (``/v1/chat/completions``) all speak.
No vendor SDK is required per the technology decisions in
IMPLEMENTATION_PLAN.md §3.
"""

from __future__ import annotations

from typing import Any, Optional

import re

import httpx
import structlog
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


#: model id -> which token-limit parameter that model accepts.
#:
#: Populated from the provider's own 400, so nothing is written down in advance
#: and a family we have never seen corrects itself on its first call.
logger = structlog.get_logger(__name__)

_TOKEN_PARAM: dict[str, str] = {}

#: model id -> parameters this model refuses. Also learned from 400s.
#:
#: The generalisation of the token-limit fix. OpenAI's reasoning families reject
#: more than one field - max_tokens by name, and temperature by value - and each
#: refusal arrives as a 400 that names the offending parameter. Encoding a list
#: of which models dislike which fields would be one more piece of written-down
#: model knowledge, and this file has been burned twice by that: "deepseek-chat"
#: came from a vendor's own docs and was not served, and the Groq ids in our
#: notes were retired before anyone used them.
#:
#: So the provider teaches us. First call for an unknown model sends everything;
#: each complaint removes one field and is remembered.
_UNSUPPORTED_PARAMS: dict[str, set[str]] = {}

#: How many times one call may be re-shaped before giving up. Three because a
#: model can plausibly object to max_tokens, then temperature, then one more -
#: and an unbounded loop against a provider that objects to everything is a way
#: to spend money discovering nothing.
_MAX_PARAM_RETRIES = 3

#: "Unsupported parameter: 'max_tokens'", "Unsupported value: 'temperature'",
#: "Invalid parameter: 'top_p'" - the wording varies by provider and over time,
#: so the NAME is extracted rather than the sentence matched.
_BAD_PARAM_RE = re.compile(
    r"(?:unsupported|unrecognized|invalid|unknown)\s+(?:parameter|value|argument|field)s?\s*:?\s*['\"]?([a-z_]+)",
    re.IGNORECASE,
)


def _offending_param(text: str) -> str | None:
    """Which parameter the provider is objecting to, if it named one."""
    match = _BAD_PARAM_RE.search(text or "")
    return match.group(1) if match else None


def _is_token_param_complaint(text: str) -> bool:
    """Specifically the max_tokens/max_completion_tokens rename, which is a SWAP
    rather than a removal - dropping the limit entirely would let a reasoning
    model spend an unbounded budget."""
    low = (text or "").lower()
    return "max_completion_tokens" in low and "max_tokens" in low


class HttpChatModel(BaseChatModel):
    base_url: str
    model: str
    api_key: str = ""
    temperature: float = 0.0
    max_tokens: int = 2048
    timeout_seconds: int = 60
    extra_headers: dict = {}
    #: Parameters to leave OUT of every call to this model, set by an operator.
    #: The escape hatch for a provider that objects to a field in a way the
    #: learned path cannot parse - it names nothing, or names it differently.
    skip_params: set = set()
    #: Extra body fields to send - reasoning_effort, top_p, a provider-specific
    #: flag. Applied before the skip list, so a refused field cannot be added
    #: back by configuration.
    extra_params: dict = {}
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
            # WHICH TOKEN-LIMIT PARAMETER, decided per model at runtime.
            #
            # OpenAI's newer families reject the older name outright:
            #
            #     gpt-5 + max_tokens             400 "'max_tokens' is not
            #                                    supported with this model. Use
            #                                    'max_completion_tokens' instead."
            #     gpt-5 + max_completion_tokens  200
            #
            # Fifty-one gpt-5.x ids sit in this account's catalogue and NONE of
            # them could be called, because this payload always sent max_tokens.
            # It was invisible twice over: the models never reached the dropdown
            # (the callability probe read that 400 as "model does not exist"),
            # and anyone selecting one by hand would have got a 400 that reads
            # like a bad request rather than a missing feature.
            #
            # LEARNED, NOT LISTED. A name prefix - gpt-5, o1, o3 - would be a
            # list that goes stale, and this file has already been burned twice
            # by written-down model knowledge: "deepseek-chat" came from a
            # vendor's own docs and was not served, and the Groq ids in our .env
            # notes were retired before anyone used them. So the first call for
            # an unknown model uses the old name, and the provider's own error
            # message teaches us the new one. See _generate.
            _TOKEN_PARAM.get(self.model, "max_tokens"): self.max_tokens,
        }
        if stop:
            payload["stop"] = stop
        # Anything this model has told us it will not accept, and anything the
        # operator has chosen to add or pin. skip_params wins over extra_params:
        # a field the provider has REFUSED must not be reintroduced by config,
        # because the result is a 400 nobody can explain from the settings alone.
        payload.update(self.extra_params or {})
        for name in set(_UNSUPPORTED_PARAMS.get(self.model, ())) | set(self.skip_params or ()):
            payload.pop(name, None)
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

    def __repr_args__(self):
        """Never render the credential.

        A pytest assertion printed a live API key into a terminal - twice - by
        formatting one of these objects in a failure diff. Nothing in the code
        asked for that; the default repr shows every field, and one of the fields
        is a secret.

        Redacting here rather than in the tests is the fix that holds: the leak
        came from a diff nobody wrote, so a rule about how to write assertions
        would not have prevented it. Anything that formats this object - a
        traceback, a log line, a debugger - is now safe by construction.
        """
        return [
            (k, "***redacted***" if k == "api_key" and v else v)
            for k, v in super().__repr_args__()
        ]

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
                # RESHAPE UNTIL THE MODEL ACCEPTS IT.
                #
                # The goal is that every model in a provider's catalogue is
                # callable, from the oldest to whatever shipped this week,
                # without anyone maintaining a table of which vintage wants
                # which fields. Older models take max_tokens and a temperature;
                # OpenAI's reasoning families reject max_tokens by name and
                # refuse a temperature by value. Both say so in the 400, naming
                # the field.
                #
                # So each refusal removes or renames exactly what was named, and
                # the answer is remembered per model - the second call is the
                # last time it costs a round trip.
                for _ in range(_MAX_PARAM_RETRIES):
                    try:
                        response = request_with_retry(
                            lambda: client.post(url, headers=headers, json=payload)
                        )
                        break
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 400:
                            raise
                        body = exc.response.text
                        if _is_token_param_complaint(body):
                            # A SWAP, not a removal: dropping the limit entirely
                            # would let a reasoning model spend an unbounded
                            # budget on one answer.
                            swapped = "max_completion_tokens" if "max_tokens" in payload else "max_tokens"
                            payload.pop("max_tokens", None)
                            payload.pop("max_completion_tokens", None)
                            payload[swapped] = self.max_tokens
                            _TOKEN_PARAM[self.model] = swapped
                            logger.info("llm.token_param_learned", model=self.model, parameter=swapped)
                            continue
                        offender = _offending_param(body)
                        # Only drop something we actually sent, and never the
                        # messages or the model id - a 400 naming those is a real
                        # bad request and re-raises rather than being "fixed".
                        if not offender or offender not in payload or offender in ("model", "messages"):
                            raise
                        payload.pop(offender, None)
                        _UNSUPPORTED_PARAMS.setdefault(self.model, set()).add(offender)
                        logger.info("llm.param_dropped", model=self.model, parameter=offender)
                else:
                    # Exhausted the retries: re-raise the last refusal rather
                    # than returning something that looks like a success.
                    raise
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
