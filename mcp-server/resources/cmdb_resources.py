"""cmdb:// resource content. Static resources (schema/business-rules/scoring-model)
are read from docs/ so this server and docs/ never drift; per-entity resources
are computed live from the repository layer.
"""

from __future__ import annotations

from pathlib import Path

from app.repositories import application_repository, capacity_request_repository, cluster_repository, investigation_repository, recommendation_repository

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"


def _read_doc(filename: str, fallback: str) -> str:
    path = DOCS_DIR / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return fallback


def schema_resource() -> str:
    return _read_doc(
        "architecture.md",
        "docs/architecture.md has not been generated yet. Run the schema through "
        "database/schema.sql for the authoritative table definitions.",
    )


def business_rules_resource() -> str:
    return _read_doc(
        "business-rules.md",
        "docs/business-rules.md has not been generated yet. See app/rules/eligibility.py "
        "for the authoritative RULE-001..010 implementations.",
    )


def scoring_model_resource() -> str:
    return _read_doc(
        "scoring-model.md",
        "docs/scoring-model.md has not been generated yet. See app/scoring/engine.py and "
        "app/scoring/subscores.py for the authoritative scoring formulas.",
    )


def application_resource(application_id: str) -> str:
    app = application_repository.get_by_id(int(application_id))
    if app is None:
        return f"No application with id {application_id}."
    return app.model_dump_json(indent=2)


def cluster_resource(cluster_id: str) -> str:
    cluster = cluster_repository.get_by_id(int(cluster_id))
    if cluster is None:
        return f"No cluster with id {cluster_id}."
    return cluster.model_dump_json(indent=2)


def investigation_resource(investigation_id: str) -> str:
    inv = investigation_repository.get_by_id(int(investigation_id))
    if inv is None:
        return f"No investigation with id {investigation_id}."
    recs = recommendation_repository.list_for_investigation(inv.InvestigationId)
    import json

    return json.dumps(
        {"investigation": inv.model_dump(mode="json"), "recommendations": [r.model_dump(mode="json") for r in recs]},
        indent=2,
    )


def capacity_request_resource(capacity_request_id: str) -> str:
    req = capacity_request_repository.get_by_id(int(capacity_request_id))
    if req is None:
        return f"No capacity request with id {capacity_request_id}."
    return req.model_dump_json(indent=2)
