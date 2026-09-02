"""A catalogue entry is not permission to call it.

Measured on the live Gemini key:

    GET  /v1beta/models/gemini-2.5-pro                 200, and
         supportedGenerationMethods includes generateContent
    POST /v1beta/models/gemini-2.5-pro:generateContent  404

So the capability flag describes what the MODEL supports, not what the KEY may
invoke. The judge role and its fallback were both chosen from that list and
produced zero verdicts across four evaluation runs, every error reading "all LLM
providers failed".
"""

from __future__ import annotations

from app.agents import providers


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _HttpError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.response = _FakeResponse(status_code)


class _Adapter:
    """Answers per model, so one probe run can mix verdicts."""

    name = "fake"

    def __init__(self, behaviour: dict):
        self.behaviour = behaviour
        self.probed: list[str] = []

    def build(self, settings, model, label):
        self.probed.append(model)
        outcome = self.behaviour[model]

        class _Llm:
            def invoke(self, messages):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        return _Llm()


def setup_function():
    providers.reset_callability_cache()


def test_only_callable_models_survive():
    adapter = _Adapter({
        "good": "OK",
        "missing": _HttpError(404),
        "forbidden": _HttpError(403),
    })
    models, filtered = providers.callable_models(adapter, object(), ["good", "missing", "forbidden"])
    assert models == ["good"]
    assert filtered == 2


def test_catalogue_order_is_preserved():
    """The caller already sorted it; a second opinion here would silently
    reorder somebody's dropdown."""
    adapter = _Adapter({"b": "OK", "a": "OK", "c": "OK"})
    models, _ = providers.callable_models(adapter, object(), ["b", "a", "c"])
    assert models == ["b", "a", "c"]


def test_a_hard_failure_is_cached_and_not_re_probed():
    """404 is stable. Re-probing it every time would spend a real API call to
    re-learn something that cannot change."""
    adapter = _Adapter({"good": "OK", "missing": _HttpError(404)})
    providers.callable_models(adapter, object(), ["good", "missing"])
    first = list(adapter.probed)
    providers.callable_models(adapter, object(), ["good", "missing"])
    assert adapter.probed == first, "cached verdicts must not be re-probed"


def test_a_transient_failure_is_NOT_cached():
    """A 429 or a timeout is not evidence a model is unusable. Caching it beside
    the 404s would remove a working model from the dropdown for a day because of
    one bad minute."""
    adapter = _Adapter({"flaky": _HttpError(429)})
    models, _ = providers.callable_models(adapter, object(), ["flaky"])
    assert models == []
    # Second pass probes again rather than trusting the failure.
    providers.callable_models(adapter, object(), ["flaky"])
    assert adapter.probed == ["flaky", "flaky"]


def test_an_empty_completion_counts_as_callable():
    """EmptyCompletionError means the endpoint SERVED the model and the model
    spent its budget reasoning. That is a usable model with a prompt problem,
    not a missing one."""
    from app.agents.http_chat_model import EmptyCompletionError

    adapter = _Adapter({"reasoner": EmptyCompletionError("finish_reason=length")})
    models, filtered = providers.callable_models(adapter, object(), ["reasoner"])
    assert models == ["reasoner"]
    assert filtered == 0


def test_a_withdrawn_model_is_not_offered_from_cache():
    """A cached yes must not outlive the catalogue. Intersecting with the live
    list means a model pulled by the provider stops being offered."""
    adapter = _Adapter({"good": "OK", "later_withdrawn": "OK"})
    providers.callable_models(adapter, object(), ["good", "later_withdrawn"])
    models, _ = providers.callable_models(adapter, object(), ["good"])
    assert models == ["good"]


def test_refresh_re_probes_everything():
    """The case where an operator has just fixed a key or changed a plan and
    wants the truth now, not tomorrow."""
    adapter = _Adapter({"m": "OK"})
    providers.callable_models(adapter, object(), ["m"])
    providers.callable_models(adapter, object(), ["m"], refresh=True)
    assert adapter.probed == ["m", "m"]


def test_no_models_means_no_calls():
    adapter = _Adapter({})
    assert providers.callable_models(adapter, object(), []) == ([], 0)
    assert adapter.probed == []
