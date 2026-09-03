"""Per-role model selection: resolution, persistence, and the admin gate.

These never call a provider. list_models is patched where a listing is needed,
because the point under test is the routing, and a test that asked DeepSeek what
it serves would fail whenever DeepSeek was busy.
"""

from __future__ import annotations

import pytest

from app.agents import provider_models, roles
from app.agents.llm_factory import reset_role_model_cache, resolve_all_roles, resolve_role
from app.repositories import llm_role_repository as repo


@pytest.fixture(autouse=True)
def clean_overrides():
    """Clear every ASSIGNABLE name, not every role.

    These differ now: a role's fallback is stored under its own key
    ("extraction.fallback"), and this fixture iterating roles.ROLES cleaned the
    primaries and left the fallbacks behind. A leaked override survives the
    process - it is a database row - so the damage lands on the NEXT run, as a
    test failing on state a previous run wrote. That is the worst shape a test
    failure can take, because the run that caused it passed.
    """
    def _wipe():
        for name in roles.ASSIGNABLE_ROLE_NAMES:
            repo.clear(name)
        reset_role_model_cache()

    _wipe()
    yield
    _wipe()


class TestRoleDefinitions:
    def test_every_chain_function_is_routed_exactly_once(self):
        """A chain absent from the map would silently keep using the process
        default while the screen claimed to control it."""
        seen: list[str] = []
        for role in roles.ROLES:
            seen.extend(role.chains)
        assert len(seen) == len(set(seen)), "a chain function is claimed by two roles"

    def test_the_map_covers_every_chain_in_chains_py(self):
        """The roles exist to describe real call sites. A chain added later
        without a role is the failure this catches."""
        import inspect

        from app.agents import chains

        public = {
            name
            for name, obj in inspect.getmembers(chains, inspect.isfunction)
            if not name.startswith("_")
            and obj.__module__ == chains.__name__
            and "llm" in inspect.signature(obj).parameters
        }
        assert public <= set(roles.CHAIN_TO_ROLE), f"unrouted chains: {public - set(roles.CHAIN_TO_ROLE)}"

    def test_there_is_no_evaluation_role(self):
        """app.evaluation.graders is deterministic on purpose - an LLM-as-judge
        would introduce the failure it is measuring. Adding an evaluation role
        here would imply a model choice that does not exist."""
        assert "evaluation" not in roles.ROLE_NAMES
        assert "eval" not in roles.ROLE_NAMES


class TestResolution:
    def test_a_role_with_no_override_is_not_an_override(self):
        """Asserts the INTENT, not the literal layer.

        This read `source == "config"` and broke when the tier slots were
        populated with groq models - narration now resolves at "tier", which is
        correct and has nothing to do with what this test is checking. Pinning
        the exact layer made it fail on a change that never touched overrides.
        """
        resolved = resolve_role("narration")
        assert resolved["source"] != "override"
        assert resolved["provider"] and resolved["model"]

    def test_an_override_wins_and_says_so(self):
        repo.set_override("narration", "mock", "seek-and-destroy-mock", "E1001")
        resolved = resolve_role("narration")
        assert resolved["source"] == "override"
        assert (resolved["provider"], resolved["model"]) == ("mock", "seek-and-destroy-mock")
        assert resolved["updated_by"] == "E1001"

    def test_overriding_one_role_leaves_the_others_alone(self):
        """The whole point of roles: change narration to compare it without
        moving extraction underneath the comparison."""
        repo.set_override("narration", "mock", "seek-and-destroy-mock", "E1001")
        by_role = {r["role"]: r for r in resolve_all_roles()}
        assert by_role["narration"]["source"] == "override"
        # Asserts no OTHER role became an OVERRIDE - not that they are all
        # "config". They are not: the judge resolves to judge_default so it is
        # never the author of what it grades. Pinning the literal string made
        # this test fail on a new resolution layer that had not touched
        # narration at all, which is the opposite of what it exists to check.
        assert all(
            by_role[r.name]["source"] != "override"
            for r in roles.ROLES if r.name != "narration"
        )

    def test_reset_returns_the_role_to_config(self):
        repo.set_override("reporting", "mock", "seek-and-destroy-mock", "E1001")
        assert resolve_role("reporting")["source"] == "override"
        assert repo.clear("reporting") is True
        # Back to whatever the configuration says - tier or base config. The
        # point is that it is no longer an override.
        assert resolve_role("reporting")["source"] != "override"

    def test_reset_reports_whether_anything_was_removed(self):
        """"reset" and "was already the default" are different answers, and the
        screen should not claim to have changed something it did not."""
        assert repo.clear("extraction") is False
        repo.set_override("extraction", "mock", "seek-and-destroy-mock", "E1001")
        assert repo.clear("extraction") is True

    def test_a_second_write_updates_rather_than_duplicating(self):
        repo.set_override("extraction", "mock", "seek-and-destroy-mock", "E1001")
        repo.set_override("extraction", "mock", "seek-and-destroy-mock", "E1002")
        rows = [r for r in repo.list_all() if r["RoleName"] == "extraction"]
        assert len(rows) == 1
        assert rows[0]["UpdatedBy"] == "E1002"

    def test_resolution_survives_the_overrides_table_being_unreachable(self, monkeypatch):
        """The admin screen is a convenience. A database problem must not take
        narration offline - the configured default is a correct answer."""

        def boom():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(repo, "as_map", boom)
        resolved = resolve_role("narration")
        # It answered at all - that is the property. Which layer answered is not
        # what this test is about.
        assert resolved["source"] != "override"
        assert resolved["provider"] and resolved["model"]


class TestProviderListing:
    # Patches provider_models.http_get, which the adapters import at call time.
    # It was _get; the underscore said "private to this module" and it stopped
    # being that when the per-provider listing logic moved into
    # agents.providers and started calling it across a module boundary.
    def test_non_chat_models_are_filtered_out(self):
        """Provider listings mix in embedding and speech models that would fail
        or return nonsense as a narration model, and the name alone does not
        tell an operator which is which."""
        assert provider_models._is_chat_model("deepseek-v4-flash")
        assert not provider_models._is_chat_model("gemini-embedding-001")
        assert not provider_models._is_chat_model("whisper-large-v3")
        assert not provider_models._is_chat_model("text-moderation-latest")

    def test_an_unreachable_provider_reports_its_reason(self, monkeypatch):
        """Never a remembered list. A stale dropdown fails at run time, far from
        the screen that caused it."""
        monkeypatch.setattr(provider_models, "http_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("connection refused")))
        provider_models.reset_cache()
        result = provider_models.list_models("deepseek", refresh=True)
        assert result["available"] is False
        assert result["models"] == []
        assert "connection refused" in result["error"]

    def test_a_failed_listing_is_not_cached(self, monkeypatch):
        """Caching a failure would keep a provider dark for ten minutes after a
        blip, or after the operator fixed the key."""
        provider_models.reset_cache()
        monkeypatch.setattr(provider_models, "http_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        provider_models.list_models("deepseek", refresh=True)
        assert "deepseek" not in provider_models._cache

    def test_an_unverifiable_model_is_allowed_rather_than_blocked(self, monkeypatch):
        """Refusing every write while a provider is briefly unreachable would
        make the screen unusable. The API says the save was unverified instead."""
        monkeypatch.setattr(
            provider_models, "list_models",
            lambda p, **k: {"provider": p, "available": False, "models": [], "error": "down"},
        )
        assert provider_models.is_known_model("deepseek", "anything-at-all") is True


class TestAdminGate:
    def test_e1001_is_an_administrator(self):
        """Migration 006 grants it. If this fails the screen is unreachable for
        everyone, which looks like a broken page rather than a missing grant."""
        from app.repositories import employee_repository

        employee = employee_repository.get_by_number("E1001")
        assert employee is not None and employee.IsAdmin is True

    def test_an_ordinary_employee_is_not(self):
        from app.repositories import employee_repository

        others = [e for e in employee_repository.list_active(limit=50) if e.EmployeeNumber != "E1001"]
        assert others, "seed data has only one employee - cannot verify the default"
        assert all(e.IsAdmin is False for e in others)


class TestTheJudgeIsIndependentByDefault:
    """The judge graded answers written by its own model, so every verdict it
    produced was self-judged, excluded from every headline score, and worth
    nothing. It ran on every answer and emitted zero usable output.

    Not a corner case - it was the default. Every role resolves to
    SAD_LLM__PROVIDER unless something says otherwise.
    """

    def test_the_judge_does_not_share_a_model_with_the_authors(self):
        judge = resolve_role("judge")
        authors = [
            r for r in resolve_all_roles()
            if r["role"] != "judge"
            and (r["provider"], r["model"]) == (judge["provider"], judge["model"])
        ]
        assert authors == [], (
            "the judge shares a model with " + ", ".join(a["role"] for a in authors)
            + " - every verdict on those roles' answers is self-judged and discarded"
        )

    def test_the_judge_default_says_which_layer_decided(self):
        assert resolve_role("judge")["source"] == "judge_default"

    def test_the_admin_screen_still_outranks_it(self):
        """An operator who deliberately points the judge somewhere has made a
        choice, and a default must not quietly undo it."""
        repo.set_override("judge", "mock", "seek-and-destroy-mock", "E1001")
        resolved = resolve_role("judge")
        assert resolved["source"] == "override"
        assert resolved["provider"] == "mock"

    def test_the_judges_fallback_is_independent_too(self):
        """A fallback landing back on the primary model would restore exactly
        the self-judging this prevents - and only when the judge's own provider
        is down, which is the moment nobody checks where verdicts came from."""
        assert resolve_role("judge.fallback")["source"] == "judge_default"

    def test_no_other_role_is_affected(self):
        for name in ("narration", "reporting", "extraction", "grounded_qa", "summarization"):
            assert resolve_role(name)["source"] != "judge_default"

    def test_a_provider_with_no_credential_falls_through_rather_than_breaking(
        self, monkeypatch
    ):
        """Grading must never be able to break a delivered answer. A judge
        pointed at a provider with no key would raise inside the grading path,
        so an unusable default degrades to the config answer with a warning."""
        from app.config.settings import get_settings

        settings = get_settings().llm
        monkeypatch.setattr(settings, "judge_provider", "anthropic", raising=False)
        monkeypatch.setattr(settings, "judge_model", "claude-x", raising=False)
        monkeypatch.setattr(type(settings), "key_for", lambda self, p: "", raising=False)
        assert resolve_role("judge")["source"] != "judge_default"
