"""One provider, one adapter, registered once.

WHAT THIS REPLACES AND WHY
--------------------------
Adding a provider used to mean editing FOUR places that had to stay in agreement:

    settings.py         the `provider` Literal
    llm_factory.py      an eight-branch if-chain on the provider string
    provider_models.py  a four-branch if-chain on the same strings
    provider_models.py  the LISTABLE tuple

Two parallel chains keyed on the same values, plus a tuple naming a subset of
them. Nothing connected the three, so a provider could be constructible and
unlistable, or listable and unconstructible, and the type system had no opinion
either way. Adding Groq and Anthropic in one night was enough to hit it twice.

This is the Open/Closed problem in its ordinary form: the behaviour varies by
provider, so the code that varies belongs WITH each provider rather than in a
chain every provider has to be threaded through. A new provider is now a new
class and one line in REGISTRY; no existing function changes.

WHAT EACH ADAPTER OWNS
----------------------
Its wire format, its credential, its default endpoint, and how to enumerate its
models. Those four things vary together - Anthropic's x-api-key header goes with
its /v1/messages endpoint and its /v1/models listing - and splitting them across
three files is what let them drift.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
Model names. Not one adapter carries a default model id, and that is a rule
rather than an oversight: "deepseek-chat" came straight from a vendor's own
documentation and that account does not serve it. Ids are enumerated live from
each provider or supplied by the operator; a hardcoded one is a guess with an
expiry date.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from langchain_core.language_models.chat_models import BaseChatModel


@runtime_checkable
class ProviderAdapter(Protocol):
    """What the factory and the admin screen both need from a provider.

    Small on purpose. A caller that only lists models should not have to know how
    one is constructed, and a caller that only builds should not carry listing
    concerns - so the two methods are independent and `listable` says which
    providers answer the second.
    """

    name: str
    #: Whether the admin screen can enumerate real model ids from this provider.
    #: False means the operator types a name, which is the weaker position and is
    #: why it is stated per provider rather than assumed.
    listable: bool

    def build(self, settings: Any, model: str, label: str) -> BaseChatModel:
        """A configured client. Raises ValueError with the variable to set when
        the credential is missing - naming the fix at the point of failure."""

    def list_models(self, settings: Any) -> list[str]:
        """Live model ids. Raises if the provider cannot be reached; the caller
        turns that into an 'unavailable' entry with the reason attached."""


#: What SAD_LLM__MODEL defaults to. It is a sentinel, not a model - forwarding it
#: to a real provider 404s in a way that reads like an authentication failure,
#: which sends an operator to check their key when the problem is their model.
MOCK_MODEL_SENTINEL = "seek-and-destroy-mock"


# =============================================================================
# What belongs in a model dropdown
# =============================================================================
#: Substrings that mean "this is not a text chat model, whatever else it is".
#:
#: Measured against the live listings rather than imagined: openai returned 124
#: models of which 42 were transcription, speech, image, embedding or moderation;
#: gemini 38 of which 10; groq 14 of which 5. Gemini already filters on
#: generateContent and STILL returns gemini-3-pro-image and *-preview-tts,
#: because those genuinely do implement that method - the capability flag says
#: what the endpoint accepts, not what the model is for.
#:
#: This is safe to write down in a way a model-id list is not. Whisper is not a
#: chat model today and will not become one; "tts" names what the thing IS. The
#: hardcoded model ids removed from this file went stale twice because they named
#: which instances existed, which changes weekly.
#:
#: The cost of getting one wrong is asymmetric and that is why the list is short.
#: A non-chat model left in is selectable and fails at runtime on somebody's
#: investigation; a chat model wrongly excluded is invisible and unfixable from
#: the screen. So: no "vision" and no "preview". Vision models take text and
#: answer in text - deepseek-v4-flash-vision-exp is a chat model - and "preview"
#: is a release stage rather than a capability, covering most of what gemini
#: currently ships.
_NOT_CHAT = (
    "embed", "embedding",
    "tts", "whisper", "transcribe", "speech", "orpheus", "audio",
    "image", "dall-e",
    "moderation", "guard",
    "realtime", "computer-use",
)

#: Retired FAMILIES, not retired ids.
#:
#: Neither OpenAI's nor Groq's /models carries a deprecation flag, so this cannot
#: be derived and something has to be written down. A family is the right unit: a
#: retired family does not come back, so this list only ever ages in one
#: direction. An allowlist of current ids is the thing that goes stale, and it is
#: what this file correctly stopped carrying.
_RETIRED = ("babbage", "davinci", "curie", "ada-", "gpt-3.5", "text-davinci")


def usable_chat_models(names: list[str]) -> list[str]:
    """The subset an operator could sensibly assign to a role.

    Applied by every adapter, so one provider cannot quietly offer a
    text-to-speech model as a narration model. Sorted for a stable dropdown -
    providers return their own order and openai's opens on babbage-002.
    """
    out = []
    for name in names:
        low = name.lower()
        if any(bad in low for bad in _NOT_CHAT):
            continue
        if any(dead in low for dead in _RETIRED):
            continue
        out.append(name)
    return sorted(out)


def _resolve_model(model: str, adapter: Any) -> str:
    """The model to send, refusing to forward the mock sentinel.

    Two providers can substitute a safe default because their names are stable:
    Gemini and DeepSeek. The others cannot, and that is deliberate rather than
    an omission - Groq and Anthropic rename and retire ids on their own schedule,
    so a hardcoded default is a guess with an expiry date. "deepseek-chat" came
    straight from a vendor's own documentation and that account does not serve
    it.

    So where there is no safe default, this RAISES and names the variable. An
    operator who has selected a provider without choosing a model has made a
    configuration mistake, and telling them so beats sending a placeholder and
    letting the vendor's 404 explain it badly.
    """
    if model and model != MOCK_MODEL_SENTINEL:
        return model
    default = getattr(adapter, "default_model", "")
    if default:
        return default
    raise ValueError(
        f"No model configured for provider {adapter.name!r}. Set SAD_LLM__MODEL, or "
        f"choose one on the Model Settings screen - {adapter.name} model ids change "
        f"often enough that this code will not guess one."
    )


def _require_key(settings: Any, provider: str) -> str:
    """The credential, or a failure that says which variable to set.

    Uniform across adapters because the failure is uniform: an operator who
    selects a provider on the admin screen and gets "invalid API key" has been
    told the wrong thing. What they need is the name of the empty variable.
    """
    key = settings.key_for(provider)
    if not key:
        raise ValueError(
            f"No {provider} credential. Set SAD_LLM__PROVIDER_KEYS__{provider.upper()}, "
            f"or SAD_LLM__API_KEY if {provider} is the only provider in use."
        )
    return key


@dataclass(frozen=True)
class OpenAICompatible:
    """OpenAI, DeepSeek and Groq differ only by endpoint and credential.

    They speak the same wire format, so one adapter parameterised by base URL
    covers all three - and a fourth costs one REGISTRY line. This is the reason
    the registry is worth having: the shape that varies is data, not control
    flow.
    """

    name: str
    default_base_url: str
    listable: bool = True
    #: Only where the vendor's ids are stable. Empty for Groq, which renames
    #: often - it raises instead of guessing.
    default_model: str = ""

    def build(self, settings: Any, model: str, label: str) -> BaseChatModel:
        from app.agents.http_chat_model import HttpChatModel

        return HttpChatModel(
            base_url=settings.base_url or self.default_base_url,
            model=_resolve_model(model, self),
            api_key=_require_key(settings, self.name),
            temperature=settings.temperature,
            max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds,
            provider_name=label,
        )

    def list_models(self, settings: Any) -> list[str]:
        from app.agents.provider_models import http_get

        base = (settings.base_url or self.default_base_url).rstrip("/")
        key = _require_key(settings, self.name)
        data = http_get(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
        return usable_chat_models([m["id"] for m in data.get("data", [])])


@dataclass(frozen=True)
class Anthropic:
    """Not OpenAI-compatible: /v1/messages, x-api-key, a required version header,
    system as a top-level field, and content as a list of typed blocks."""

    name: str = "anthropic"
    listable: bool = True
    version: str = "2023-06-01"
    default_base_url: str = "https://api.anthropic.com/v1"

    def build(self, settings: Any, model: str, label: str) -> BaseChatModel:
        from app.agents.anthropic_chat_model import AnthropicChatModel

        return AnthropicChatModel(
            api_key=_require_key(settings, self.name),
            model=_resolve_model(model, self),
            base_url=settings.base_url or self.default_base_url,
            temperature=settings.temperature,
            # Required by this API rather than optional, so the settings default
            # is load-bearing here in a way it is not for the others.
            max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds,
            provider_name=label,
        )

    def list_models(self, settings: Any) -> list[str]:
        from app.agents.provider_models import http_get

        base = (settings.base_url or self.default_base_url).rstrip("/")
        data = http_get(
            f"{base}/models",
            headers={"x-api-key": _require_key(settings, self.name), "anthropic-version": self.version},
        )
        # Anthropic already returns chat models only, so this filters nothing
        # today. Applied anyway for the ordering, and so a future non-chat
        # model on their catalogue does not become the one provider that
        # offers it.
        return usable_chat_models([m["id"] for m in data.get("data", [])])


@dataclass(frozen=True)
class Gemini:
    """Google's generateContent API - a different shape again, and the reason
    this codebase already had a second client before Anthropic arrived."""

    name: str = "gemini"
    listable: bool = True
    default_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    default_model: str = "gemini-flash-latest"

    def build(self, settings: Any, model: str, label: str) -> BaseChatModel:
        from app.agents.gemini_chat_model import GeminiChatModel

        return GeminiChatModel(
            api_key=_require_key(settings, self.name),
            model=_resolve_model(model, self),
            base_url=settings.base_url or self.default_base_url,
            temperature=settings.temperature,
            max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds,
            provider_name=label,
        )

    def list_models(self, settings: Any) -> list[str]:
        from app.agents.provider_models import http_get

        base = (settings.base_url or self.default_base_url).rstrip("/")
        # Header, not ?key=. A credential in a query string is logged by proxies,
        # kept in browser history and echoed into error text - and this error text
        # is rendered on the admin screen, so a failed listing was disclosing the
        # key to whoever was looking at it. Google accepts x-goog-api-key.
        data = http_get(f"{base}/models", headers={"x-goog-api-key": _require_key(settings, self.name)})
        # Gemini lists every model with the methods it supports; one that cannot
        # generateContent cannot narrate, whatever its name suggests.
        return usable_chat_models([
            m["name"].split("/", 1)[-1]
            for m in data.get("models", [])
            if "generateContent" in (m.get("supportedGenerationMethods") or [])
        ])


@dataclass(frozen=True)
class Ollama:
    """Local, so no credential and a configured base URL rather than a default."""

    name: str = "ollama"
    #: Not offered either. Nothing serves ollama on the deployment, so listing it
    #: produces a permanent red line, and a health panel with a line that is
    #: always red teaches people to stop reading the panel.
    listable: bool = False

    def build(self, settings: Any, model: str, label: str) -> BaseChatModel:
        from app.agents.http_chat_model import HttpChatModel

        return HttpChatModel(
            base_url=settings.ollama_base_url.rstrip("/") + "/v1",
            model=_resolve_model(model, self),
            api_key="ollama",  # accepted and ignored by ollama; not a secret
            temperature=settings.temperature,
            max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds,
            provider_name=label,
        )

    def list_models(self, settings: Any) -> list[str]:
        from app.agents.provider_models import http_get

        base = settings.ollama_base_url.rstrip("/")
        data = http_get(f"{base}/v1/models", headers={"Authorization": "Bearer ollama"})
        return usable_chat_models([m["id"] for m in data.get("data", [])])


@dataclass(frozen=True)
class AzureOpenAI:
    """OpenAI's wire format at a per-deployment URL, authenticated by header.

    NOT listable. Azure exposes deployments rather than models, and the deployment
    name is chosen by whoever provisioned it - there is no catalogue to enumerate,
    so the operator supplies it. Saying so here is the point of the flag: the
    admin screen can show why the list is empty instead of looking broken.
    """

    name: str = "azure-openai"
    listable: bool = False

    def build(self, settings: Any, model: str, label: str) -> BaseChatModel:
        from app.agents.http_chat_model import HttpChatModel

        if not settings.azure_endpoint or not settings.azure_deployment:
            raise ValueError(
                "SAD_LLM__AZURE_ENDPOINT and SAD_LLM__AZURE_DEPLOYMENT are required "
                "for azure-openai"
            )
        key = _require_key(settings, self.name)
        return HttpChatModel(
            base_url=(
                f"{settings.azure_endpoint.rstrip('/')}/openai/deployments/"
                f"{settings.azure_deployment}"
            ),
            model=_resolve_model(model, self),
            api_key=key,
            temperature=settings.temperature,
            max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds,
            # Azure authenticates with its own header, not a Bearer token.
            extra_headers={"api-key": key},
            provider_name=label,
        )

    def list_models(self, settings: Any) -> list[str]:
        raise NotImplementedError(
            "azure-openai exposes deployments, not a model catalogue - "
            "set SAD_LLM__AZURE_DEPLOYMENT to the deployment name."
        )


@dataclass(frozen=True)
class Mock:
    """Offline. Listable so the admin screen shows it as a real choice rather
    than a gap - selecting it is how you run without a provider at all."""

    name: str = "mock"
    #: Not offered on the admin screen. It is an offline test double, and a
    #: production settings screen that lists it invites somebody to select it.
    #: Still selectable through configuration, which is where a developer running
    #: without a provider sets it.
    listable: bool = False
    default_model: str = MOCK_MODEL_SENTINEL

    def build(self, settings: Any, model: str, label: str) -> BaseChatModel:
        from app.agents.mock_llm import MockChatModel

        # Takes no model - it is the offline stub, and the sentinel IS its name.
        return MockChatModel()

    def list_models(self, settings: Any) -> list[str]:
        return ["seek-and-destroy-mock"]


#: The single place a provider is declared. Everything else - construction,
#: listing, which providers the admin screen enumerates - is derived from this,
#: so the three can no longer disagree.
REGISTRY: dict[str, ProviderAdapter] = {
    a.name: a
    for a in (
        Mock(),
        OpenAICompatible(name="openai", default_base_url="https://api.openai.com/v1"),
        OpenAICompatible(
            name="deepseek",
            default_base_url="https://api.deepseek.com/v1",
            # Verified against GET /models, not assumed.
            default_model="deepseek-v4-flash",
        ),
        OpenAICompatible(name="groq", default_base_url="https://api.groq.com/openai/v1"),
        Anthropic(),
        Gemini(),
        Ollama(),
        AzureOpenAI(),
    )
}


def adapter_for(provider: str) -> ProviderAdapter:
    """The adapter, or a failure naming what is actually supported.

    Not a KeyError: an unknown provider is nearly always a typo in configuration,
    and a message listing the valid values fixes it in one read.
    """
    try:
        return REGISTRY[(provider or "").lower()]
    except KeyError:
        raise ValueError(
            f"Unsupported LLM provider {provider!r}. Supported: {', '.join(sorted(REGISTRY))}."
        ) from None


def listable_providers() -> tuple[str, ...]:
    """Providers the admin screen can enumerate, derived rather than restated."""
    return tuple(sorted(name for name, a in REGISTRY.items() if a.listable))
