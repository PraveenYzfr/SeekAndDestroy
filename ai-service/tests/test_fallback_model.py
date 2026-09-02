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
from app.agents.roles import FALLBACK_ROLE, ROLES
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


class TestItIsChangeableOnTheModelSettingsScreen:
    def test_fallback_is_listed_as_a_role(self):
        """A backup nobody can see or change is a backup nobody checks."""
        assert FALLBACK_ROLE in {r.name for r in ROLES}

    def test_it_routes_no_chains(self):
        """It is not a stage of an investigation - it is where every stage goes
        when the primary is down. Listing chains would misdescribe it."""
        role = next(r for r in ROLES if r.name == FALLBACK_ROLE)
        assert role.chains == ()

    def test_an_override_beats_configuration(self, monkeypatch):
        """Shown on that screen and unchangeable by it would be worse than not
        showing it at all."""
        s = _settings()
        monkeypatch.setattr(llm_factory, "get_settings", lambda: type("S", (), {"llm": s})())
        monkeypatch.setattr(
            llm_factory, "resolve_role",
            lambda r: {"source": "override", "provider": "groq", "model": "llama-3.3-70b"},
        )
        llm_factory.reset_role_model_cache()
        chain = llm_factory.build_chat_model()
        assert [n for n, _ in chain.members] == ["deepseek", "groq"]
        assert chain.members[1][1].model == "llama-3.3-70b"
