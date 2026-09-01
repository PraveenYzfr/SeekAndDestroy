from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.auth import get_current_employee
from app.cache.store import get_cache_store
from app.repositories.base import fetch_one
from app.retrieval.vector_store import get_vector_store
from app.security.jwt_service import AuthenticatedEmployee

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["system"])


@router.get("/api/health")
def health():
    return {"status": "ok"}


@router.get("/api/ready")
def ready():
    checks = {}
    try:
        fetch_one("SELECT 1 AS Ok")
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"

    try:
        store = get_vector_store()
        checks["vector_store"] = f"ok ({store.count()} documents)"
    except Exception as exc:
        checks["vector_store"] = f"error: {exc}"

    try:
        cache = get_cache_store()
        checks["cache"] = f"ok ({cache.count()} keys)"
    except Exception as exc:
        checks["cache"] = f"error: {exc}"

    healthy = all(v.startswith("ok") for v in checks.values())
    return JSONResponse(status_code=200 if healthy else 503, content={"status": "ready" if healthy else "not_ready", "checks": checks})


@router.post("/api/index/rebuild")
def rebuild_index(current: AuthenticatedEmployee = Depends(get_current_employee)):
    # /api/health and /api/ready stay unauthenticated on purpose (standard
    # infra liveness/readiness probes never carry credentials) - this is the
    # only mutating endpoint on this router, so it alone is protected.
    from app.retrieval.indexer import index_all

    count = index_all()
    return {"indexed_documents": count}


@router.post("/api/index/refresh")
def refresh_index_endpoint(current: AuthenticatedEmployee = Depends(get_current_employee)):
    """Index only what changed since the last run. Manually triggered.

    Same authentication as /api/index/rebuild - it mutates the index and it
    spends money at the embedding provider, so it is not a probe.

    Returns a per-source breakdown rather than a bare total, because "the job
    ran" and "the job indexed something" have to stay distinguishable. A refresh
    that legitimately finds nothing returns zeros, which is a different and
    equally useful answer from one that found nothing because a watermark was
    never written.
    """
    from app.retrieval.indexer import refresh_index

    return refresh_index()


@router.get("/api/index/status")
def index_status(current: AuthenticatedEmployee = Depends(get_current_employee)):
    """How far each source has been indexed, and how many documents are stored.

    Authenticated with the others: it reports row counts and table names, which
    is more than an unauthenticated caller needs to know about the estate.
    """
    from app.repositories import index_watermark_repository
    from app.retrieval.vector_store import get_vector_store

    try:
        indexed = get_vector_store().count()
    except Exception as exc:
        indexed = f"error: {exc}"
    return {"documents_in_index": indexed, "watermarks": index_watermark_repository.list_all()}
