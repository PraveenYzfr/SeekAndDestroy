"""Two providers at once, which is what the multi-model switch actually needs.

Every role could already choose a different MODEL. None could choose a different
PROVIDER, because settings.llm.api_key is process-wide: with DeepSeek as the
default, pointing `planning` at Groq built a Groq client using DEEPSEEK'S KEY and
got a 401. That reads as a bad credential rather than a design limit, which is why
it would have been expensive to find later.

The point of the switch is to stop paying reasoning-model time for schema-filling
work - extraction and planning taking 5-40 seconds each inside a 68-98 second
investigation - so it has to be possible to run a fast provider for one role and a
reasoning provider for another, simultaneously, with their own credentials.

api_key stays the fallback so a single-provider deployment needs no change.
"""

from __future__ import annotations

import pytest

from app.agents import llm_factory, provider_models
from app.config import get_settings


#: Built directly, never through get_settings(). These assert the RESOLUTION
#: RULE, and reading the real .env would make them depend on whichever keys
#: happen to be configured on the machine - which is how two of them failed the
#: first time, and how a live credential ended up in a pytest diff.
#:
#: Nothing here compares a key VALUE. Assertions are on which source won, so a
#: failure prints a field name rather than a secret.
def _llm(**kw):
    from app.config.settings import LlmSettings

    # _env_file=None keeps the developer's .env out of it; explicit kwargs
    # outrank environment variables in pydantic-settings.
    return LlmSettings(_env_file=None, **kw)


class TestCredentialResolution:
    def test_a_provider_uses_its_own_key(self):
        s = _llm(api_key="shared", provider_keys={"groq": "groq-key"})
        assert s.key_for("groq") == "groq-key"

    def test_the_default_provider_falls_back_to_api_key(self):
        """The compatibility guarantee: one provider and one key behaves exactly
        as it did before provider_keys existed."""
        s = _llm(provider="deepseek", api_key="shared", provider_keys={"groq": "groq-key"})
        assert s.key_for("deepseek") == "shared"

    def test_a_NON_default_provider_does_not_borrow_it(self):
        """api_key belongs to the default provider. Lending it to another made a
        MISSING credential look like a WRONG one - Groq with no key sent
        DeepSeek's and reported 401, and the natural response is to re-issue a
        credential that was never broken."""
        s = _llm(provider="deepseek", api_key="shared", provider_keys={})
        assert s.key_for("groq") == ""

    def test_two_providers_resolve_differently(self):
        """The property the whole feature exists for."""
        s = _llm(api_key="", provider_keys={"groq": "a", "deepseek": "b"})
        assert s.key_for("groq") != s.key_for("deepseek")

    def test_lookup_is_case_insensitive(self):
        s = _llm(provider_keys={"groq": "groq-key"})
        assert s.key_for("GROQ") == "groq-key"

    def test_an_empty_entry_falls_back_rather_than_returning_empty(self):
        """A placed-but-unfilled slot is how a key arrives before somebody sets
        it. It must behave as absent, not as a credential of zero length."""
        s = _llm(provider="groq", api_key="shared", provider_keys={"groq": ""})
        assert s.key_for("groq") == "shared"

    def test_no_credential_anywhere_is_empty_not_an_error(self):
        """Resolution reports absence; the factory decides what to do about it."""
        assert _llm(api_key="", provider_keys={}).key_for("groq") == ""

    def test_the_default_provider_with_no_slot_still_resolves(self):
        """azure-openai as the default, configured the old way, keeps working."""
        assert _llm(provider="azure-openai", api_key="shared").key_for("azure-openai") == "shared"


class TestGroqIsAFirstClassProvider:
    def test_it_is_enumerable_by_the_admin_screen(self):
        """Listed rather than typed. "deepseek-chat" came straight from a
        vendor's own documentation and that account does not serve it."""
        assert "groq" in provider_models.LISTABLE

    def test_it_builds_with_its_own_key(self, monkeypatch):
        monkeypatch.setenv("SAD_LLM__API_KEY", "deepseek-key")
        monkeypatch.setenv("SAD_LLM__PROVIDER_KEYS__GROQ", "groq-key")
        get_settings.cache_clear()
        llm_factory.reset_role_model_cache()
        model = llm_factory.build_chat_model_for_provider("groq", model_override="llama-3.3-70b-versatile")
        assert model.api_key == "groq-key", "built with the default provider's key"
        assert "groq.com" in model.base_url
        get_settings.cache_clear()

    def test_a_missing_credential_says_which_variable_to_set(self, monkeypatch):
        monkeypatch.setenv("SAD_LLM__API_KEY", "")
        monkeypatch.setenv("SAD_LLM__PROVIDER_KEYS__GROQ", "")
        get_settings.cache_clear()
        llm_factory.reset_role_model_cache()
        with pytest.raises(ValueError) as exc:
            llm_factory.build_chat_model_for_provider("groq", model_override="x")
        assert "SAD_LLM__PROVIDER_KEYS__GROQ" in str(exc.value)
        get_settings.cache_clear()

    def test_no_default_model_is_guessed(self):
        """Groq renames and retires ids on its own schedule, so the adapter
        carries no default and RAISES rather than sending a guess. Gemini and
        DeepSeek do carry one, because their names are stable and forwarding the
        mock sentinel to them 404s in a way that reads like an auth failure."""
        from app.agents.providers import REGISTRY

        assert not getattr(REGISTRY["groq"], "default_model", "")
        assert not getattr(REGISTRY["anthropic"], "default_model", "")
        assert REGISTRY["gemini"].default_model
        assert REGISTRY["deepseek"].default_model
