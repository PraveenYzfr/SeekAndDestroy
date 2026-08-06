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
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import routes_cmdb, routes_forecast, routes_hosting, routes_investigations, routes_recommendations, routes_rightsizing, routes_system
from app.api.errors import register_exception_handlers
from app.config import get_settings

settings = get_settings()

_log_level = getattr(logging, settings.service.log_level.upper(), logging.INFO)

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer() if settings.service.log_json else structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(_log_level),
)

logger = structlog.get_logger(__name__)

app = FastAPI(
    title="SeekAndDestroy AI Service",
    description="AI-powered infrastructure recommendation platform - deterministic capacity/scoring engines with an LLM narration layer.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
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

app.include_router(routes_system.router)
app.include_router(routes_cmdb.router)
app.include_router(routes_hosting.router)
app.include_router(routes_rightsizing.router)
app.include_router(routes_forecast.router)
app.include_router(routes_investigations.router)
app.include_router(routes_recommendations.router)
