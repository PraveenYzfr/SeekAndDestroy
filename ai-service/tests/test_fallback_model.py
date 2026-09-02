"""The backup was guaranteed to fail at the moment it was needed.

build_chat_model() built every member of the chain with settings.model, so an
OpenAI fallback behind a DeepSeek primary requested "deepseek-v4-flash" and 404d.
Nothing caught it because SAD_LLM__FALLBACK_PROVIDERS shipped empty, so the chain
was never constructed - a safety net that had never been unfolded.

A fallback should also be of COMPARABLE capability to the primary. One that is
markedly weaker turns an outage into a quiet drop in answer quality, and nobody
investigates that, because nothing errored.
"""

from __future__ import annotations

import pytest

from app.agents import llm_factory
from app.agents import roles as roles_mod
from app.agents.roles import ROLES
from app.config.settings import LlmSettings


def _settings(**kw):
    base = dict(
        provider="deepseek", model="deepseek-v4-flash",
        provider_keys={"deepseek": "d", "openai": "o", "groq": "g"},
    )
    return LlmSettings(_env_file=None, **{**base, **kw})


@pytest.fixture
def built(monkeypatch):
    """build_chat_model() against explicit settings, with caches cleared."""
    def _build(settings):
        from app.config import get_settings

        monkeypatch.setattr(get_settings, "cache_clear", lambda: None, raising=False)
        monkeypatch.setattr(llm_factory, "get_settings", lambda: type("S", (), {"llm": settings})())
        monkeypatch.setattr(llm_factory, "resolve_role", lambda r: {"source": "config"})
        llm_factory.reset_role_model_cache()
        return llm_factory.build_chat_model()
    return _build


class TestTheFallbackCarriesItsOwnModel:
    def test_it_does_not_inherit_the_primarys_model(self):
        """The bug, stated as a test: an OpenAI client must never be built asking
        for a DeepSeek model name."""
        model = llm_factory.build_chat_model_for_provider.__wrapped__ if hasattr(
            llm_factory.build_chat_model_for_provider, "__wrapped__") else None
        s = _settings()
        assert s.fallback_model and s.fallback_model != s.model

    def test_the_default_fallback_is_openai_at_comparable_capability(self):
        s = _settings()
        assert s.fallback_provider_list == ["openai"]
        assert s.fallback_model == "gpt-4o"

    def test_the_chain_is_primary_then_fallback_each_with_its_own_model(self, built):
        chain = built(_settings())
        assert [(n, c.model) for n, c in chain.members] == [
            ("deepseek", "deepseek-v4-flash"),
            ("openai", "gpt-4o"),
        ]


class TestTheChainCannotBreakThePrimary:
    def test_an_unbuildable_fallback_is_skipped_not_fatal(self, built):
        """No credential for the BACKUP must not take the primary offline. That
        would be a safety net that causes the fall."""
        result = built(_settings(provider_keys={"deepseek": "d"}))  # no openai key
        assert not hasattr(result, "members"), "should degrade to the primary alone"
        assert result.model == "deepseek-v4-flash"

    def test_a_provider_is_not_its_own_backup(self, built):
        result = built(_settings(fallback_providers="deepseek"))
        assert not hasattr(result, "members")

    def test_no_fallback_configured_returns_the_primary_alone(self, built):
        result = built(_settings(fallback_providers=""))
        assert not hasattr(result, "members")


class TestEveryRoleHasItsOwnFallback:
    """Per role, not one global spare.

    The roles do not fail alike. Extraction wants strict schema adherence;
    reporting wants readable prose. A single estate-wide substitute is the right
    answer for at most one of them, and being wrong for the others shows up as a
    quiet change in output quality rather than as an error.
    """

    def test_each_role_has_an_assignable_fallback_key(self):
        for role in ROLES:
            assert roles_mod.fallback_role_name(role.name) in roles_mod.ASSIGNABLE_ROLE_NAMES

    def test_the_key_maps_back_to_its_role(self):
        assert roles_mod.primary_role_name("planning.fallback") == "planning"
        assert roles_mod.primary_role_name("planning") == "planning"

    def test_a_fallback_key_is_not_itself_a_role(self):
        """It is a slot on the planning row, not an eighth stage of an
        investigation."""
        assert "planning.fallback" not in roles_mod.ROLE_NAMES

    def test_an_overridden_role_with_a_fallback_gets_a_chain(self, monkeypatch):
        """Configuring a role used to REMOVE its resilience - an overridden role
        had no backup at all, so the more deliberately the platform was set up,
        the more fragile it became."""
        def fake_resolve(name):
            if name == "planning":
                return {"source": "override", "provider": "deepseek", "model": "deepseek-v4-flash"}
            if name == "planning.fallback":
                return {"source": "override", "provider": "openai", "model": "gpt-4o"}
            return {"source": "config"}

        monkeypatch.setattr(llm_factory, "resolve_role", fake_resolve)
        monkeypatch.setattr(llm_factory, "get_settings",
                            lambda: type("S", (), {"llm": _settings()})())
        llm_factory.reset_role_model_cache()
        chain = llm_factory.get_chat_model_for_role("planning")
        assert [n for n, _ in chain.members] == ["deepseek", "openai"]

    def test_no_fallback_chosen_means_no_chain(self, monkeypatch):
        """A fallback nobody selected is a model nobody evaluated. Filling one in
        automatically is how an outage becomes a silent change in behaviour."""
        def fake_resolve(name):
            if name == "planning":
                return {"source": "override", "provider": "deepseek", "model": "deepseek-v4-flash"}
            return {"source": "config"}

        monkeypatch.setattr(llm_factory, "resolve_role", fake_resolve)
        monkeypatch.setattr(llm_factory, "get_settings",
                            lambda: type("S", (), {"llm": _settings()})())
        llm_factory.reset_role_model_cache()
        result = llm_factory.get_chat_model_for_role("planning")
        assert not hasattr(result, "members")

    def test_an_unbuildable_role_fallback_does_not_break_the_role(self, monkeypatch):
        def fake_resolve(name):
            if name == "planning":
                return {"source": "override", "provider": "deepseek", "model": "deepseek-v4-flash"}
            if name == "planning.fallback":
                return {"source": "override", "provider": "groq", "model": "x"}
            return {"source": "config"}

        monkeypatch.setattr(llm_factory, "resolve_role", fake_resolve)
        # no groq credential
        monkeypatch.setattr(llm_factory, "get_settings",
                            lambda: type("S", (), {"llm": _settings(provider_keys={"deepseek": "d"})})())
        llm_factory.reset_role_model_cache()
        result = llm_factory.get_chat_model_for_role("planning")
        assert not hasattr(result, "members"), "the role must still answer"
