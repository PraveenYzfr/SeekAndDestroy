from __future__ import annotations

import structlog
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.cache.store import get_cache_store
from app.repositories.base import fetch_one
from app.retrieval.vector_store import get_vector_store

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
def rebuild_index():
    from app.retrieval.indexer import index_all

    count = index_all()
    return {"indexed_documents": count}
