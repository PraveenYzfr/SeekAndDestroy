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
from app.config import get_settings, settings as settings_module


@pytest.fixture
def llm(monkeypatch):
    """A fresh LlmSettings built from env, with the caches cleared around it."""
    def _build(**env):
        for k in list(env):
            monkeypatch.setenv(k, env[k])
        get_settings.cache_clear()
        return get_settings().llm
    yield _build
    get_settings.cache_clear()


class TestCredentialResolution:
    def test_a_provider_uses_its_own_key(self, llm, monkeypatch):
        monkeypatch.setenv("SAD_LLM__API_KEY", "shared")
        monkeypatch.setenv("SAD_LLM__PROVIDER_KEYS__GROQ", "groq-key")
        s = llm()
        assert s.key_for("groq") == "groq-key"

    def test_a_provider_without_one_falls_back(self, llm, monkeypatch):
        """The compatibility guarantee: a deployment with one provider and one
        key behaves exactly as it did before provider_keys existed."""
        monkeypatch.setenv("SAD_LLM__API_KEY", "shared")
        monkeypatch.setenv("SAD_LLM__PROVIDER_KEYS__GROQ", "groq-key")
        assert llm().key_for("deepseek") == "shared"

    def test_two_providers_resolve_differently(self, llm, monkeypatch):
        """The property the whole feature exists for."""
        monkeypatch.setenv("SAD_LLM__API_KEY", "")
        monkeypatch.setenv("SAD_LLM__PROVIDER_KEYS__GROQ", "groq-key")
        monkeypatch.setenv("SAD_LLM__PROVIDER_KEYS__DEEPSEEK", "deepseek-key")
        s = llm()
        assert s.key_for("groq") != s.key_for("deepseek")

    def test_lookup_is_case_insensitive(self, llm, monkeypatch):
        monkeypatch.setenv("SAD_LLM__PROVIDER_KEYS__GROQ", "groq-key")
        assert llm().key_for("GROQ") == "groq-key"

    def test_no_credential_anywhere_is_empty_not_an_error(self, llm, monkeypatch):
        """Resolution reports absence; the factory decides what to do about it."""
        monkeypatch.setenv("SAD_LLM__API_KEY", "")
        assert llm().key_for("groq") == ""


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

    def test_no_default_model_is_guessed(self, monkeypatch):
        """Groq renames and retires models often. The admin screen enumerates
        live ids; this must not invent one."""
        import inspect
        source = inspect.getsource(llm_factory.build_chat_model_for_provider)
        groq_branch = source.split('provider == "groq"', 1)[1].split("if provider ==", 1)[0]
        assert "model_override or settings.model" in groq_branch
