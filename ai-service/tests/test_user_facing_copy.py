"""What a person reads must be about their estate, never about our machinery.

Reported: a question about one host came back

    "I cannot filter by runtime language - this platform does not track it.
     That is a limit of what is recorded, not a search that came back empty,
     so rephrasing will not help."

The middle sentence explains the difference between a schema gap and an empty
result set. That distinction is ours to care about and theirs to be spared.

GROUNDED_QA_SYSTEM already forbids this for MODEL-WRITTEN prose - "never mention
retrieval, context, embeddings, documents, indexes, chunks, prompts or the model
itself". The fixed strings this platform ships had no equivalent check, so the
rule applied to the half we generate and not the half we wrote by hand.
"""
from __future__ import annotations

import re

import pytest

from app.agents import query_capability as qc
from app.graph import conversation, nodes, scope

#: Words that describe how this platform works rather than what the estate holds.
INTERNAL = (
    "retriev", "embedding", "index", "chunk", "vector", "prompt", "schema",
    "column", "table", "result set", "came back empty", "pipeline", "corpus",
    "LLM", "token", "graph", "checkpoint", "repository",
)


def _standard_replies() -> dict[str, str]:
    replies = {
        "out_of_scope": scope.OUT_OF_SCOPE_REPLY,
        "no_referent": conversation._NO_REFERENT,
        "no_options": conversation._NO_OPTIONS,
        "vague_placement": nodes.quick_reply("find me somewhere to put it"),
        "unmodelled_attribute": nodes.quick_reply("best dc for java apps"),
        "frustration": scope.frustration_reply("this is useless", "Where can I host APP-CRM?"),
    }
    override = getattr(scope, "OVERRIDE_FRAMING_REPLY", None)
    if override:
        replies["override_framing"] = override
    return {k: v for k, v in replies.items() if v}


@pytest.mark.parametrize("name", sorted(_standard_replies()))
def test_no_standard_reply_explains_our_own_machinery(name):
    text = _standard_replies()[name]
    leaked = [w for w in INTERNAL if re.search(rf"\b{re.escape(w)}", text, re.I)]
    assert not leaked, (
        f"{name} tells the reader about {leaked} - that is how this platform "
        "works, not what their estate holds"
    )


def test_the_unmodelled_attribute_reply_speaks_in_one_voice():
    """It read "I don't record X. What it does record is Y" - first person then
    third, which reads as two systems talking about each other."""
    text = nodes.quick_reply("best dc for java apps") or ""
    assert "What it does record" not in text


def test_it_still_says_rewording_will_not_help():
    """The one genuinely useful thing this reply offers: it stops somebody
    trying the same question five different ways."""
    text = nodes.quick_reply("best dc for java apps") or ""
    assert "rewording won't help" in text.lower()


class TestANodeNameIsNotARuntime:
    """Every node here is <cluster>-NODE-nn, and "node" is a runtime language.

    So "cmh-p234-NODE-01 is not an ideal choice?" - a question about one host -
    was answered "I cannot filter by runtime language". The hyphens either side
    of NODE are word boundaries, so the boundary-aware match fired on a hostname.

    Third time this class has bitten: "app" matched "apply", "report" matched
    "reporting service", now "node" matches every host we own.
    """

    @pytest.mark.parametrize("query", [
        "cmh-p234-NODE-01 (in cmh-p234) is a not an ideal choice ?",
        "is den-p097-NODE-11 a good pick?",
        "compare cmh-p225-NODE-02 and phx-p167-NODE-08",
        "why was atl-03 rejected?",
        "what happened in INC1009985?",
    ])
    def test_an_identifier_is_never_read_as_an_attribute(self, query):
        assert qc.unmodelled_attribute(query) is None, (
            "a name being asked ABOUT was read as a filter being asked FOR"
        )

    @pytest.mark.parametrize("query", [
        "give me best dc for java apps",
        "which clusters run node.js workloads?",
        "where can I host a python service?",
        "do we run any node applications?",
    ])
    def test_a_real_language_question_is_still_caught(self, query):
        """The masking must not blind the check it protects. "node" as a word in
        a sentence is still a language question."""
        found = qc.unmodelled_attribute(query)
        assert found is not None and found.name == "runtime language"
