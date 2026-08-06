"""RFC 7807 ProblemDetails error handling."""

from __future__ import annotations

import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.repositories.base import RowLimitExceeded

logger = structlog.get_logger(__name__)


class ProblemDetailsError(Exception):
    def __init__(self, status: int, title: str, detail: str, type_: str = "about:blank", errors: dict | None = None):
        self.status = status
        self.title = title
        self.detail = detail
        self.type_ = type_
        self.errors = errors
        super().__init__(detail)


def _problem_response(request: Request, status: int, title: str, detail: str, type_: str = "about:blank", errors=None) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    body = {
        "type": type_, "title": title, "status": status, "detail": detail,
        "instance": str(request.url.path), "correlationId": correlation_id,
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(status_code=status, content=body, media_type="application/problem+json")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemDetailsError)
    async def handle_problem(request: Request, exc: ProblemDetailsError):
        return _problem_response(request, exc.status, exc.title, exc.detail, exc.type_, exc.errors)

    @app.exception_handler(ValidationError)
    async def handle_validation(request: Request, exc: ValidationError):
        return _problem_response(
            request, 422, "Validation failed", "The request failed schema validation.",
            errors=exc.errors(),
        )

    @app.exception_handler(RowLimitExceeded)
    async def handle_row_limit(request: Request, exc: RowLimitExceeded):
        return _problem_response(request, 400, "Query too broad", str(exc))

    @app.exception_handler(ValueError)
    async def handle_value_error(request: Request, exc: ValueError):
        return _problem_response(request, 400, "Invalid request", str(exc))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        logger.error("api.unhandled_exception", error=str(exc), path=str(request.url.path))
        return _problem_response(request, 500, "Internal server error", "An unexpected error occurred.")
