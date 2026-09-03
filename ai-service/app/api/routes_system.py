from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException
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
    """Deep readiness: database, vector store, cache.

    UNAUTHENTICATED, AND SHIELDED FROM THE INTERNET ONLY BY ACCIDENT.

    routes_system is mounted at main.py:88 WITHOUT _auth_dep - which is defined
    eight lines later and applied to every other router. So anything that can
    reach this process can call this endpoint.

    It is NOT publicly reachable today, and the reason is worth stating because
    nobody designed it: ui/nginx.conf proxies `location /api/` to
    http://api-gateway:5090/api/, and the .NET gateway has no /api/ready route.
    So the public URL 404s at the GATEWAY, not here. MEASURED, not inferred -
    curl against the public host returns 404 with an empty body.

    That shield is undesigned and untested. It disappears the moment the gateway
    gains a passthrough route or someone points nginx at ai-service directly,
    and it would disappear with no change to this file. tests/test_error_
    disclosure.py asserts the gateway does not route it, so the shield at least
    fails loudly.

    Hence: this endpoint says WHETHER, never WHY OR HOW MUCH - defence in depth
    rather than reliance on a proxy that happens not to forward the path. It
    previously returned:

        database:     f"error: {exc}"          full driver/pyodbc text, which
                                               can carry the server name and the
                                               failing statement
        vector_store: f"ok ({count} documents)" corpus size
        cache:        f"ok ({count} keys)"      cache population

    to any unauthenticated caller INSIDE the network. A probe needs the status
    code; it has never needed the prose. The diagnosis goes to the LOG, where an
    operator reads it and a stranger does not - the same split applied to the
    drift guard in app/api/errors.py.

    The counts are not lost: /api/index/status serves them behind
    authentication, which is where they belonged already.
    """
    checks: dict[str, str] = {}

    def _probe(name: str, fn) -> None:
        try:
            fn()
            checks[name] = "ok"
        except Exception as exc:  # noqa: BLE001
            # Full text to the operator, one word to the caller.
            logger.warning(
                "readiness.check_failed", check=name, error=str(exc)[:500]
            )
            checks[name] = "error"

    _probe("database", lambda: fetch_one("SELECT 1 AS Ok"))
    _probe("vector_store", lambda: get_vector_store().count())
    _probe("cache", lambda: get_cache_store().count())

    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ready" if healthy else "not_ready", "checks": checks},
    )


def _enqueue(mode: str, employee_number: str) -> dict:
    """Record the run, then publish it. In that order.

    The run row is written first so a failure to reach Redis leaves a visible
    Queued run with an explanation, rather than a request that vanished. The
    reverse order can produce a queued job with no record of who asked for it.
    """
    from app.repositories import index_run_repository
    from app.retrieval import index_queue

    run_id = index_run_repository.create(mode, employee_number)
    try:
        index_queue.enqueue(run_id)
    except index_queue.QueueUnavailable as exc:
        index_run_repository.finish(
            run_id, status="Failed", documents=0, batches=0,
            error=f"could not reach the queue: {exc}",
        )
        raise HTTPException(
            status_code=503,
            detail=f"indexing queue unavailable; run {run_id} was recorded as failed",
        ) from exc
    return {"run_id": run_id, "mode": mode, "status": "Queued"}


@router.post("/api/index/rebuild", status_code=202)
def rebuild_index(current: AuthenticatedEmployee = Depends(get_current_employee)):
    """Queue a full rebuild: clear the collection and index everything.

    202, not 200. This returns as soon as the job is queued - a full index takes
    minutes and used to hold the HTTP connection for all of them, which meant a
    proxy timeout looked identical to a failed index. Poll
    /api/index/runs/{run_id} for progress.

    /api/health and /api/ready stay unauthenticated (probes carry no
    credentials); everything that mutates the index or spends money at the
    embedding provider is authenticated and attributed.
    """
    return _enqueue("rebuild", current.employee_number)


@router.post("/api/index/refresh", status_code=202)
def refresh_index_endpoint(current: AuthenticatedEmployee = Depends(get_current_employee)):
    """Queue a differential index: only what changed since each watermark.

    The everyday operation. A refresh that finds nothing is normal and cheap;
    a rebuild is for schema changes and repair.
    """
    return _enqueue("refresh", current.employee_number)


@router.get("/api/index/runs")
def list_index_runs(limit: int = 20, current: AuthenticatedEmployee = Depends(get_current_employee)):
    from app.repositories import index_run_repository

    return {"runs": index_run_repository.list_recent(limit=min(limit, 100))}


@router.get("/api/index/runs/{run_id}")
def get_index_run(run_id: int, current: AuthenticatedEmployee = Depends(get_current_employee)):
    """One run: status, progress, and why it stopped.

    This is what a caller polls instead of holding a connection open. A Running
    run reports DocumentsIndexed and CurrentSource as it goes, so a long index is
    observable while it is still running rather than only once it is over.
    """
    from app.repositories import index_run_repository

    row = index_run_repository.get(run_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"no index run {run_id}")
    return row


@router.get("/api/index/status")
def index_status(current: AuthenticatedEmployee = Depends(get_current_employee)):
    """Index state: documents stored, watermarks, queue depth, active run.

    Queue depth is here for one specific question: a run that stays Queued means
    either the worker is busy or the worker is not running, and those need
    different responses. Depth plus the active run separates them.
    """
    from app.repositories import index_run_repository, index_watermark_repository
    from app.retrieval import index_queue
    from app.retrieval.vector_store import get_vector_store

    try:
        indexed = get_vector_store().count()
    except Exception as exc:
        indexed = f"error: {exc}"
    try:
        queued = index_queue.depth()
    except index_queue.QueueUnavailable as exc:
        queued = f"error: {exc}"

    return {
        "documents_in_index": indexed,
        "queue_depth": queued,
        "active_run": index_run_repository.active(),
        "watermarks": index_watermark_repository.list_all(),
    }
