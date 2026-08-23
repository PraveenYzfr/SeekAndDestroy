"""Google Gemini's native chat API as a ``BaseChatModel``.

Kept out of :class:`app.agents.http_chat_model.HttpChatModel` for the same
reason :mod:`app.retrieval.gemini_embedder` is kept out of ``HttpEmbedder``:
Gemini's REST shape is not OpenAI-wire-compatible, so it cannot be another
config value on that class. Same dependency-light style - raw ``httpx``, no
vendor SDK.

Request/response shape (Generative Language API, ``v1beta``)::

    POST {base_url}/models/{model}:generateContent
    {
      "contents":           [{"role": "user"|"model", "parts": [{"text": "..."}]}],
      "systemInstruction":  {"parts": [{"text": "..."}]},
      "generationConfig":   {"temperature": 0.0, "maxOutputTokens": 2048}
    }

    -> {"candidates": [{"content": {"parts": [{"text": "..."}]},
                        "finishReason": "STOP"}]}

Four differences from the OpenAI wire format, each of which breaks silently
if ignored:

* ``x-goog-api-key`` header, not ``Authorization: Bearer``.
* Assistant turns use role ``model``, not ``assistant``.
* There is no ``system`` role - system prompts go in a separate top-level
  ``systemInstruction`` field. Sending one as a normal turn makes Gemini treat
  it as user text.
* A response can come back with **no** ``parts`` at all - when the model emits
  nothing, or when output is blocked by a safety filter. ``finishReason``
  carries the why. Indexing straight into ``parts[0]`` would raise KeyError and
  look like a transport bug, so that case is turned into an explicit error.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.utils.http_retry import arequest_with_retry, request_with_retry

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
#: A moving alias on purpose. Pinned versions get retired *and* closed to new
#: keys: gemini-2.0-flash 404s outright, and gemini-2.5-flash answers
#: "no longer available to new users" - which also arrives as a 404 and reads
#: exactly like a wrong model name or a bad key. The aliases stay reachable.
#:
#: To see what a given key can actually reach:
#:   GET {base_url}/models   (x-goog-api-key header)
#: filtered on supportedGenerationMethods containing "generateContent".
DEFAULT_MODEL = "gemini-flash-latest"

#: LangChain message type -> Gemini role. Gemini only knows "user" and "model";
#: tool results are folded into the user turn.
_ROLE_MAP = {"human": "user", "ai": "model", "tool": "user"}


#: Keys Pydantic emits that Gemini's responseSchema (an OpenAPI 3.0 subset)
#: rejects outright. Sending them makes the whole request 400.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {"title", "default", "additionalProperties", "$defs", "definitions", "examples", "const"}
)


def to_gemini_schema(schema: dict, defs: Optional[dict] = None) -> dict:
    """Convert a Pydantic JSON schema into Gemini's responseSchema dialect.

    Three incompatibilities have to be handled or the request 400s:

    * ``$ref``/``$defs`` - Gemini has no reference resolution, so definitions
      are inlined.
    * ``anyOf`` - Pydantic renders ``Optional[X]`` as ``anyOf: [X, null]``.
      Gemini expresses the same thing as the non-null branch plus
      ``nullable: true``.
    * Annotation keys like ``title`` and ``default``, which are simply not in
      the accepted subset.
    """
    defs = defs if defs is not None else schema.get("$defs", {})

    if "$ref" in schema:
        ref_name = schema["$ref"].rsplit("/", 1)[-1]
        return to_gemini_schema(defs.get(ref_name, {}), defs)

    if "anyOf" in schema:
        branches = [b for b in schema["anyOf"] if b.get("type") != "null"]
        nullable = len(branches) != len(schema["anyOf"])
        converted = to_gemini_schema(branches[0], defs) if branches else {"type": "string"}
        if nullable:
            converted["nullable"] = True
        return converted

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED_SCHEMA_KEYS:
            continue
        if key == "properties":
            out["properties"] = {k: to_gemini_schema(v, defs) for k, v in value.items()}
        elif key == "items":
            out["items"] = to_gemini_schema(value, defs)
        else:
            out[key] = value
    return out


class GeminiResponseError(RuntimeError):
    """Gemini returned 200 with no usable text - empty output or a blocked
    candidate. Distinct from a transport error so callers (and the fallback
    chain) can tell "the provider is down" from "the provider refused".
    """


def _to_gemini_contents(messages: list[BaseMessage]) -> tuple[list[dict], Optional[dict]]:
    """Split LangChain messages into Gemini's ``contents`` plus its separate
    ``systemInstruction``. Multiple system messages are concatenated, since
    Gemini accepts only one.
    """
    contents: list[dict] = []
    system_parts: list[str] = []

    for message in messages:
        text = message.content if isinstance(message.content, str) else str(message.content)
        if message.type == "system":
            system_parts.append(text)
            continue
        contents.append({"role": _ROLE_MAP.get(message.type, "user"), "parts": [{"text": text}]})

    system_instruction = {"parts": [{"text": "\n\n".join(system_parts)}]} if system_parts else None
    return contents, system_instruction


class GeminiChatModel(BaseChatModel):
    api_key: str
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    temperature: float = 0.0
    max_tokens: int = 2048
    timeout_seconds: int = 60
    provider_name: str = "gemini"  # metrics label only - not sent on the wire
    #: When set, Gemini is asked to return JSON conforming to this schema and
    #: enforces it server-side. See bind_response_schema().
    response_schema: Optional[dict] = None
    transport: Optional[httpx.BaseTransport] = None  # test-only hook (httpx.MockTransport)
    async_transport: Optional[httpx.AsyncBaseTransport] = None

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "seekanddestroy-gemini"

    def bind_response_schema(self, output_model: type) -> "GeminiChatModel":
        """Return a copy that constrains output to ``output_model``'s schema.

        Prompt-based format instructions are a request; this is a constraint.
        Asked politely, gemini-flash returned valid JSON for
        FinalRecommendationReport while silently dropping the required
        ``human_action_required`` field - parse failure, narration lost, and a
        fallback message where the report should be. Server-side schema
        enforcement makes that class of failure impossible rather than
        unlikely, which is what a platform meant for model *evaluation* needs:
        a model should be judged on the quality of its content, not on whether
        it happened to obey a formatting instruction.

        Mirrors MockChatModel.bind_response_schema so app.agents.structured can
        treat both the same way.
        """
        return self.model_copy(update={"response_schema": to_gemini_schema(output_model.model_json_schema())})

    def _model_path(self) -> str:
        return self.model if self.model.startswith("models/") else f"models/{self.model}"

    def _url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self._model_path()}:generateContent"

    def _prepare(self, messages: list[BaseMessage], stop: Optional[list[str]]):
        from app.config import get_settings
        from app.services.spend_budget import check_and_increment

        # Same spend guardrail as every other real provider - a runaway graph
        # loop must not be able to burn a day's quota unnoticed.
        check_and_increment("llm_chat", get_settings().llm.daily_call_budget)

        contents, system_instruction = _to_gemini_contents(messages)
        generation_config: dict[str, Any] = {
            "temperature": self.temperature,
            "maxOutputTokens": self.max_tokens,
        }
        if stop:
            generation_config["stopSequences"] = stop
        if self.response_schema:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = self.response_schema

        payload: dict[str, Any] = {"contents": contents, "generationConfig": generation_config}
        if system_instruction:
            payload["systemInstruction"] = system_instruction

        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}
        return self._url(), headers, payload

    def _result_from(self, data: dict) -> ChatResult:
        from app.observability.metrics import llm_calls_total, llm_tokens_total

        content = self._extract_text(data)
        llm_calls_total.labels(provider=self.provider_name, outcome="success").inc()
        usage = data.get("usageMetadata") or {}
        llm_tokens_total.labels(provider=self.provider_name, kind="prompt").inc(
            usage.get("promptTokenCount") or 0)
        llm_tokens_total.labels(provider=self.provider_name, kind="completion").inc(
            usage.get("candidatesTokenCount") or 0)
        meta = {
            "provider": self.provider_name,
            "model": self.model,
            # Gemini names these differently from the OpenAI shape. Normalised
            # here so an audit row reads the same whichever provider produced
            # it - otherwise comparing cost across providers means special-casing
            # the field names at every read site.
            "prompt_tokens": usage.get("promptTokenCount"),
            "completion_tokens": usage.get("candidatesTokenCount"),
            "total_tokens": usage.get("totalTokenCount"),
        }
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

    @staticmethod
    def _extract_text(data: dict) -> str:
        """Pull the text out of a candidate, or say clearly why there isn't one.

        Gemini answers 200 with an empty/blocked candidate rather than an error
        status, so the failure has to be detected here or it surfaces as a
        confusing KeyError three frames away.
        """
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates returned")
            raise GeminiResponseError(f"Gemini returned no candidates ({reason})")

        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        if not text:
            raise GeminiResponseError(
                f"Gemini returned an empty response (finishReason={candidate.get('finishReason', 'unknown')})"
            )
        return text
