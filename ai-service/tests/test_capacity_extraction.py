"""Scenario B (free-text, no named app) capacity extraction: regex under the
offline mock LLM (which has no real NLU), real LangChain structured
extraction once a real provider is configured. See app.graph.nodes.

THESE TESTS WERE PATCHING A FUNCTION THE CODE NO LONGER CALLS.

They monkeypatched nodes.get_chat_model. Role-based model selection replaced
every call site with get_chat_model_for_role(role), and nodes.py kept importing
the old name without using it. monkeypatch.setattr only fails on an attribute
that does not EXIST, and this one still did - so the patch applied cleanly,
bound nothing, and the tests silently resolved the real configured provider.

With SAD_LLM__PROVIDER=deepseek that meant the offline test made a LIVE, BILLED
API call, and its result decided the assertion:

    deepseek answers    -> extracted is not None -> method "llm"  -> TEST FAILS
    deepseek errors     -> caught, returns None  -> method "regex" -> TEST PASSES

So it passed only when the API was broken, which is why it looked intermittent
across runs on the same code and the same machine.

Patch get_chat_model_for_role, and note it takes a role argument - a zero-arg
lambda would raise TypeError rather than silently doing nothing, which is the
better failure but still a failure.
"""

from __future__ import annotations

from app.agents.mock_llm import MockChatModel
from app.graph import nodes
from app.models.agent_contracts import CapacityRequirement
from app.models.enums import InvestigationType


def test_capacity_extraction_uses_regex_under_mock_llm(monkeypatch):
    monkeypatch.setattr(nodes, "get_chat_model_for_role", lambda role: MockChatModel())
    state = {
        "user_query": "I need 8 CPU, 32 GB RAM and 500 GB storage for a production Kubernetes workload.",
        "investigation_type": InvestigationType.CAPACITY,
    }
    result = nodes.load_application_requirements(state)
    assert result["capacity_requirements"]["extraction_method"] == "regex"
    assert result["capacity_requirements"]["cpu_cores"] == 8.0
    assert result["capacity_requirements"]["memory_gb"] == 32.0
    assert result["capacity_requirements"]["storage_gb"] == 500.0


class _FakeRealChatModel:
    """Stands in for a real (non-mock) BaseChatModel so the LLM extraction
    branch can be exercised without a live provider."""


def test_capacity_extraction_uses_llm_chain_when_real_provider_configured(monkeypatch):
    monkeypatch.setattr(nodes, "get_chat_model_for_role", lambda role: _FakeRealChatModel())
    extracted = CapacityRequirement(
        environment="Production", cpu_cores=16.0, memory_gb=64.0, storage_gb=1000.0,
        platform="VMware", availability_tier="Tier-1", data_classification="Confidential",
        preferred_location="Atlanta-DC1", expected_growth_percent=15.0,
    )
    monkeypatch.setattr(nodes, "extract_capacity_requirement", lambda llm, query: extracted)
    state = {
        "user_query": "We need a new Confidential, Tier-1 environment in Atlanta for a growing workload.",
        "investigation_type": InvestigationType.CAPACITY,
    }
    result = nodes.load_application_requirements(state)
    assert result["capacity_requirements"]["extraction_method"] == "llm"
    assert result["capacity_requirements"]["cpu_cores"] == 16.0
    assert result["requirement"]["platform"] == "VMware"
    assert result["requirement"]["preferred_location"] == "Atlanta-DC1"


def test_capacity_extraction_falls_back_to_regex_on_llm_failure(monkeypatch):
    monkeypatch.setattr(nodes, "get_chat_model_for_role", lambda role: _FakeRealChatModel())

    def _boom(llm, query):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(nodes, "extract_capacity_requirement", _boom)
    state = {
        "user_query": "I need 4 CPU, 16 GB RAM and 200 GB storage.",
        "investigation_type": InvestigationType.CAPACITY,
    }
    result = nodes.load_application_requirements(state)
    assert result["capacity_requirements"]["extraction_method"] == "regex"
    assert result["capacity_requirements"]["cpu_cores"] == 4.0
