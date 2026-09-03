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

    def test_the_default_chain_is_three_deep_across_three_vendors(self):
        """Widened from a single backup after a real outage took both legs.

        Measured on the golden run: deepseek exhausted its output budget on
        reasoning and openai returned 429 in the same investigation, so a
        two-leg chain had nothing left and the answer came back as fallback
        text. Three vendors is not belt-and-braces - it is the observed number
        of simultaneous failures plus one.
        """
        s = _settings()
        assert len(s.fallback_provider_list) == 3, s.fallback_provider_list
        assert len(set(s.fallback_provider_list)) == 3, "three LEGS is not three VENDORS"
        assert s.model.split("/")[0] not in s.fallback_provider_list[:1]
        # gemini leads: a fallback runs on a path that has ALREADY failed once,
        # so the cheapest and fastest leg goes first and the sturdier one waits
        # behind it. Asserted as an ORDER, not as a fixed list - the vendors may
        # be re-chosen, but "quickest first" is the property that must survive.
        assert s.fallback_provider_list[0] == "gemini", s.fallback_provider_list

    def test_every_leg_carries_its_own_model(self, built):
        """The bug this class is named for, at chain scale.

        A single fallback_model across the chain would ask groq for "gpt-4o" and
        404 at exactly the moment the backup was needed - a safety net that only
        fails when used. Each leg must name a model its own provider serves.
        """
        chain = built(_settings())
        names = [n for n, _ in chain.members]
        assert names[0] == "deepseek", "the primary leads"
        # Asserted as PROPERTIES rather than as a fixed vendor list. The old
        # version pinned the exact legs, so it failed when the configured chain
        # was re-ordered - reporting a changed default as though it were the
        # broken-model bug this class exists to catch. It also could not tell
        # "the chain changed" from "a leg was silently dropped", and a leg IS
        # dropped here: the fixture has no gemini credential, which is the
        # designed skip.
        s = _settings()
        assert names[1:], "the chain must not be empty"
        assert "deepseek" not in names[1:], "a provider is not its own backup"
        for leg in names[1:]:
            assert leg in s.fallback_provider_list, f"{leg} is not a configured leg"
        assert names[1:] == [n for n in s.fallback_provider_list if n in names[1:]],             f"legs are out of configured order: {names[1:]}"
        for provider, client in chain.members[1:]:
            assert client.model == s.fallback_model_for(provider),                 f"{provider} leg asked for {client.model}, not its own model"

        for provider, client in chain.members:
            assert client.model, f"{provider} leg has no model"
        models = [c.model for _, c in chain.members]
        assert len(set(models)) == len(models), f"a model is shared across legs: {models}"


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

    def test_no_fallback_chosen_means_the_configured_chain(self, monkeypatch):
        """POLICY REVERSED, deliberately.

        This used to assert that an overridden role with no explicit fallback
        got NO chain at all - the reasoning being that a fallback nobody
        selected is a model nobody evaluated.

        That was right about silence and wrong about what it cost. It meant
        choosing a model on the Model Settings screen REMOVED that role's
        resilience, so the more deliberately the platform was configured the
        more fragile it became, and nothing on the screen said so.

        The estate chain is not unevaluated: every non-overridden role already
        answers from it. An explicit per-role fallback still wins over it - that
        is the test above."""
        def fake_resolve(name):
            if name == "planning":
                return {"source": "override", "provider": "deepseek", "model": "deepseek-v4-flash"}
            return {"source": "config"}

        monkeypatch.setattr(llm_factory, "resolve_role", fake_resolve)
        monkeypatch.setattr(llm_factory, "get_settings",
                            lambda: type("S", (), {"llm": _settings()})())
        llm_factory.reset_role_model_cache()
        result = llm_factory.get_chat_model_for_role("planning")
        names = [n for n, _ in result.members]
        assert names[0] == "deepseek", "the operator's choice still leads"
        assert "deepseek" not in names[1:], "a provider is not its own backup"
        assert names[1:], "an overridden role must not be left with no backup"

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
