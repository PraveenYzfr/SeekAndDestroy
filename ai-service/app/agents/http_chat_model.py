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
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

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

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "seekanddestroy-http-openai-compatible"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
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
        url = self.base_url.rstrip("/") + "/chat/completions"
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])
