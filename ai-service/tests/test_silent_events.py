"""The two events that produced no log line at all.

Both are the platform working correctly, and both were invisible to the person
who needed to see them:

    A DRIFT REJECTION is the single most safety-critical event here - the LLM
    stated a figure the evidence did not support and the answer was discarded.
    guards.py imported no logger, so the evidence value went to the CALLER (in
    the 400 body) and NOTHING went to the OPERATOR. Fixed in two halves: the
    body in 28c7f75, the log line here.

    A BUDGET DENIAL is a deliberate guardrail. BudgetExceededError subclasses
    plain Exception and was caught nowhere, so a spend limit doing its job
    surfaced as a generic 500 "unexpected error".
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from app.agents.guards import NumberDriftError, assert_no_number_drift
from app.services.spend_budget import BudgetExceededError


class _Explanation(BaseModel):
    overall_score: float


class TestADriftRejectionIsVisibleToAnOperator:
    def test_it_logs_the_figures(self, capsys):
        """capsys, not caplog: structlog here keeps its default PrintLogger and
        writes to stdout, so nothing reaches a stdlib handler."""
        with pytest.raises(NumberDriftError):
            assert_no_number_drift(_Explanation(overall_score=87.1), {"overall_score": 42.0})
        out = capsys.readouterr().out
        assert "guards.number_drift_rejected" in out, "the rejection left no trace"
        assert "87.1" in out and "42.0" in out, (
            "the operator needs both figures - which the model said, and which the "
            "evidence held"
        )

    def test_the_field_and_evidence_key_are_named(self, capsys):
        """"a number drifted" is not actionable. WHICH number is."""
        with pytest.raises(NumberDriftError):
            assert_no_number_drift(_Explanation(overall_score=87.1), {"overall_score": 42.0})
        out = capsys.readouterr().out
        assert "overall_score" in out
        assert "_Explanation" in out, "the schema says which chain produced it"

    def test_a_clean_explanation_logs_no_rejection(self, capsys):
        assert_no_number_drift(_Explanation(overall_score=42.0), {"overall_score": 42.0})
        assert "number_drift_rejected" not in capsys.readouterr().out


class TestABudgetDenialIsRefusedNotBroken:
    def test_it_maps_to_429_and_not_500(self):
        """The status is the whole point. 500 says the platform failed; 429 says
        it refused, which is what actually happened and may succeed tomorrow."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.errors import register_exception_handlers

        app = FastAPI()
        register_exception_handlers(app)

        @app.get("/boom")
        def _boom():
            raise BudgetExceededError("llm_chat", 100, 100)

        response = TestClient(app, raise_server_exceptions=False).get("/boom")
        assert response.status_code == 429, "a guardrail firing is not a server error"

    def test_the_body_does_not_state_the_limit(self):
        """The ceiling is estate configuration. A caller who can read it can
        probe for it; an operator reads it from the log."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.errors import register_exception_handlers

        app = FastAPI()
        register_exception_handlers(app)

        @app.get("/boom")
        def _boom():
            raise BudgetExceededError("llm_chat", 12345, 12345)

        body = json.dumps(TestClient(app, raise_server_exceptions=False).get("/boom").json())
        assert "12345" not in body, "the limit leaked to the caller"
        assert "budget" in body.lower(), "the caller should still learn WHY it was refused"


class TestOneLoggingConfigurationForEveryProcess:
    """ai-indexer runs `python -m app.retrieval.worker` against the SAME image
    as ai-service, with a different command - so it never imported app.main and
    structlog.configure() never ran there at all.

    Setting SAD_SERVICE__LOG_JSON in its environment did not help: the worker
    read the same setting and rendered with a configuration it had never
    applied. A correct value with no effect is the hardest kind of wrong to see,
    and the result was TWO log shapes from one platform - the odd one coming
    from the container doing the long unattended work.
    """

    def test_both_entrypoints_configure_logging(self):
        import inspect

        from app import main
        from app.retrieval import worker

        assert "configure_logging" in inspect.getsource(main)
        assert "configure_logging()" in inspect.getsource(worker), (
            "the worker must configure logging itself - it never imports app.main"
        )

    def test_it_is_idempotent(self):
        """An entrypoint that imports another must not end up with whichever
        configuration ran last."""
        from app.observability import logging as platform_logging

        platform_logging.configure_logging()
        platform_logging.configure_logging()  # must not raise or reconfigure

    def test_json_is_the_container_default(self):
        """The compose anchor sets SAD_SERVICE__LOG_JSON true for both services,
        so a shipper receives one shape rather than two."""
        import yaml

        with open(r"D:\Praveen\Projects\SeekandDestroy\docker\docker-compose.yml", encoding="utf-8") as fh:
            compose = yaml.safe_load(fh)
        for service in ("ai-service", "ai-indexer"):
            assert "true" in compose["services"][service]["environment"]["SAD_SERVICE__LOG_JSON"]

    def test_every_service_has_bounded_logs(self):
        """json-file has NO size limit by default. On a VM that also holds SQL
        Server, Qdrant and the Prometheus TSDB, one chatty container filling the
        disk takes the database down with it."""
        import yaml

        with open(r"D:\Praveen\Projects\SeekandDestroy\docker\docker-compose.yml", encoding="utf-8") as fh:
            compose = yaml.safe_load(fh)
        unbounded = [n for n, v in compose["services"].items() if "logging" not in v]
        assert not unbounded, f"unbounded container logs: {unbounded}"
