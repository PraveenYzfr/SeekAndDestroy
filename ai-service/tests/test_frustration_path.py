"""A complaint is not an investigation.

THE TRANSCRIPT THIS COMES FROM, verbatim:

    user       you are an idiot !!
    platform   I answer infrastructure questions only ...        <- correct
    user       Its waste talking to you
    platform   Investigation Report for: Its waste talking to you
               "The evidence provided contains no information related to the
                statement ... no analysis, recommendation, or sizing can be
                performed."
               Next steps: Obtain relevant data or clarify the question.
               Investigation #135

Both messages are equally off-topic. Only the first was refused, because the
out-of-scope gate ran ONLY when the turn was not a follow-up - so once a
conversation had one turn in it, the gate was off for everything after.
"""
from __future__ import annotations

from app.graph import conversation, scope


class TestTheGateIsNotSwitchedOffByTheSecondMessage:
    def test_a_referential_statement_is_not_a_question(self):
        assert conversation.is_question("Its waste talking to you") is False

    def test_a_referential_question_still_is_one(self):
        """The follow-ups the conversation exists to support. These carry no
        estate vocabulary of their own and MUST still reach the graph."""
        for q in ["why was that rejected?", "what about those other options?",
                  "which one was cheapest?", "explain that again"]:
            assert conversation.is_question(q) is True, q

    def test_the_abuse_still_classifies_as_a_follow_up(self):
        """Not fixed by reclassifying it - it genuinely is referential. The fix
        is that being a follow-up no longer exempts it from the gate."""
        assert conversation.looks_like_follow_up("Its waste talking to you") == conversation.ABOUT_PREVIOUS

    def test_the_gate_would_refuse_it(self):
        assert scope.out_of_scope_reply("Its waste talking to you") is not None


class TestFrustrationIsAnswered:
    def test_it_fires_on_the_real_message(self):
        assert scope.frustration_reply("Its waste talking to you") is not None

    def test_it_names_the_previous_question(self):
        """"Which answer" should not be the first thing they have to explain."""
        reply = scope.frustration_reply("this is useless", "Where can I host APP-CRM?")
        assert "APP-CRM" in reply

    def test_it_asks_what_was_wrong_and_what_to_do(self):
        reply = scope.frustration_reply("waste of time")
        assert "wrong with it" in reply.lower()
        assert "re-run" in reply.lower()

    def test_it_does_not_patronise_an_ordinary_statement(self):
        """The failure mode worth avoiding. Each of these is a normal thing for
        a working engineer to type, and none of them is a complaint."""
        for q in ["this is the wrong cluster",
                  "that capacity figure looks off",
                  "the shortlist is missing prod",
                  "which clusters are underutilized?"]:
            assert scope.frustration_reply(q) is None, q

    def test_it_apologises_once_and_asks_twice(self):
        """Not an apology loop. One acknowledgement, then the questions that can
        actually move it forward."""
        reply = scope.frustration_reply("useless")
        assert reply.lower().count("sorry") == 0
        assert reply.count("?") >= 2

    def test_a_plain_off_topic_message_gets_the_plain_refusal(self):
        """Frustration handling must not swallow the ordinary out-of-scope case
        - "who is the best actor in India?" is not a complaint."""
        assert scope.frustration_reply("who is the best actor in India?") is None
        assert scope.out_of_scope_reply("who is the best actor in India?") is not None
