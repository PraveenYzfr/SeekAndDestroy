"""SeekAndDestroy AI service entrypoint.

Run with:
    uvicorn app.main:app --host 127.0.0.1 --port 8088 --reload
(from the ai-service/ directory, with .venv active).
"""

from __future__ import annotations

import logging
import time
import uuid

import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from app.api import auth, routes_admin, routes_cmdb, routes_forecast, routes_hosting, routes_insights, routes_investigations, routes_recommendations, routes_rightsizing, routes_system
from app.api.auth import get_current_employee
from app.api.errors import register_exception_handlers
from app.config import get_settings
from app.observability.logging import configure_logging

settings = get_settings()

# Shared with the indexer, which runs a different command against this same
# image and therefore never imports this module. See app/observability/logging.py.
configure_logging()

logger = structlog.get_logger(__name__)

app = FastAPI(
    title="SeekAndDestroy AI Service",
    description="AI-powered infrastructure recommendation platform - deterministic capacity/scoring engines with an LLM narration layer.",
    version="1.0.0",
)

_cors = get_settings().cors
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors.origin_list,
    # A wildcard origin and credentials are mutually exclusive by spec, and
    # browsers enforce it - claiming both meant the header was ignored while
    # the config read as "anybody, authenticated". Say which one you mean.
    allow_credentials=not _cors.is_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-Id", str(uuid.uuid4()))
    request.state.correlation_id = correlation_id
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Correlation-Id"] = correlation_id
    logger.info(
        "http.request", method=request.method, path=request.url.path,
        status_code=response.status_code, duration_ms=round(duration_ms, 2),
    )
    structlog.contextvars.clear_contextvars()
    return response


register_exception_handlers(app)

# Standard HTTP request-rate/latency/status-code metrics, auto-instrumented.
# GET /metrics is unauthenticated, matching /api/health and /api/ready - a
# Prometheus scraper is another standard infra probe, not a platform client.
# Platform-specific counters (LLM/embedding calls, cache hit rate, spend-
# budget denials, investigations created) live in app.observability.metrics
# and are wired in at their own call sites, not here.
Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

# /api/health, /api/ready (unauthenticated - standard infra probes) and
# /api/index/rebuild (individually protected - see routes_system.py) live here.
app.include_router(routes_system.router)
# /api/auth/dev-token must itself be unauthenticated (that's how a token is
# obtained in the first place); it validates the employee_number it's given
# against a real, active Employee row instead.
app.include_router(auth.router)

# Every other route in the platform requires a valid Bearer token - applied at
# the router level so no individual route can be added later and forgotten.
_auth_dep = [Depends(get_current_employee)]
app.include_router(routes_cmdb.router, dependencies=_auth_dep)
app.include_router(routes_hosting.router, dependencies=_auth_dep)
app.include_router(routes_rightsizing.router, dependencies=_auth_dep)
app.include_router(routes_forecast.router, dependencies=_auth_dep)
app.include_router(routes_investigations.router, dependencies=_auth_dep)
app.include_router(routes_insights.router, dependencies=_auth_dep)
app.include_router(routes_recommendations.router, dependencies=_auth_dep)
# Admin routes carry their own require_admin dependency per route rather than
# relying on _auth_dep here - _auth_dep proves who you are, not what you may
# change, and a route added to this router later must not inherit merely
# being authenticated as sufficient.
app.include_router(routes_admin.router)
