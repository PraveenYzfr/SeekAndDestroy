"""The registry is the single declaration of a provider.

Adding one used to mean editing four places that had to stay in agreement: the
settings Literal, an eight-branch if-chain in llm_factory, a four-branch chain in
provider_models, and a hand-maintained LISTABLE tuple. Nothing connected them, so
a provider could be constructible and unlistable, or listable and
unconstructible, and neither the type system nor a test would notice.

These tests pin the invariants that replaced that coupling.
"""

from __future__ import annotations

import typing

import pytest

from app.agents.providers import REGISTRY, ProviderAdapter, adapter_for, listable_providers
from app.config.settings import LlmSettings


def _allowed() -> set[str]:
    return set(typing.get_args(LlmSettings.model_fields["provider"].annotation))


class TestTheRegistryIsTheSingleSourceOfTruth:
    def test_every_configurable_provider_has_an_adapter(self):
        """The failure this prevents: a provider selectable in configuration and
        unbuildable at runtime."""
        assert _allowed() - set(REGISTRY) == set()

    def test_every_adapter_is_configurable(self):
        """And the reverse: an adapter nobody can select is dead code that reads
        as a supported provider."""
        assert set(REGISTRY) - _allowed() == set()

    def test_listable_is_derived_not_restated(self):
        from app.agents import provider_models

        assert set(provider_models.LISTABLE) == set(listable_providers())

    def test_every_adapter_satisfies_the_protocol(self):
        for name, adapter in REGISTRY.items():
            assert isinstance(adapter, ProviderAdapter), name


class TestUnknownProviders:
    def test_an_unknown_name_names_what_is_supported(self):
        """A typo in configuration is the common case, and a message listing the
        valid values fixes it in one read."""
        with pytest.raises(ValueError) as exc:
            adapter_for("gpt5")
        assert "deepseek" in str(exc.value) and "anthropic" in str(exc.value)

    def test_lookup_is_case_insensitive(self):
        assert adapter_for("DeepSeek") is REGISTRY["deepseek"]


class TestModelResolution:
    def test_providers_with_churning_ids_carry_no_default(self):
        """Groq and Anthropic rename and retire on their own schedule. A default
        would be a guess with an expiry date - "deepseek-chat" came from a
        vendor's own docs and is not served."""
        assert not getattr(REGISTRY["groq"], "default_model", "")
        assert not getattr(REGISTRY["anthropic"], "default_model", "")

    def test_providers_with_stable_ids_do(self):
        """Because forwarding the mock sentinel to a real provider 404s in a way
        that reads like an authentication failure."""
        assert REGISTRY["gemini"].default_model
        assert REGISTRY["deepseek"].default_model

    def test_the_mock_sentinel_is_never_forwarded(self):
        from app.agents.providers import MOCK_MODEL_SENTINEL, _resolve_model

        assert _resolve_model(MOCK_MODEL_SENTINEL, REGISTRY["gemini"]) != MOCK_MODEL_SENTINEL

    def test_no_model_and_no_default_raises_naming_the_variable(self):
        from app.agents.providers import MOCK_MODEL_SENTINEL, _resolve_model

        with pytest.raises(ValueError) as exc:
            _resolve_model(MOCK_MODEL_SENTINEL, REGISTRY["groq"])
        assert "SAD_LLM__MODEL" in str(exc.value)

    def test_an_explicit_model_always_wins(self):
        from app.agents.providers import _resolve_model

        assert _resolve_model("llama-3.3-70b", REGISTRY["groq"]) == "llama-3.3-70b"


class TestAzureIsHonestAboutNotListing:
    def test_it_is_not_listable(self):
        """Azure exposes deployments, not a model catalogue. Saying so lets the
        admin screen explain an empty list instead of looking broken."""
        assert REGISTRY["azure-openai"].listable is False
        assert "azure-openai" not in listable_providers()

    def test_listing_it_explains_why_rather_than_erroring_opaquely(self):
        from app.agents import provider_models

        result = provider_models.list_models("azure-openai", refresh=True)
        assert result["available"] is False
        assert "deployment" in result["error"].lower()
