"""RFC 7807 ProblemDetails error handling.

AN EXCEPTION MESSAGE IS INTERNAL UNTIL IT SAYS OTHERWISE
--------------------------------------------------------
This module used to render `str(exc)` into the response body for every
ValueError. That is a disclosure primitive, and it was live:

    guards.py     NumberDriftError("LLM output field 'overall_score' = 87.1 does
                  not match evidence 'overall_score' = 98.33 ...")
    errors.py     handle_value_error -> 400, detail = str(exc), verbatim

NumberDriftError subclasses ValueError, so the guard's own diagnostic string
became the 400 body - field name, model value, evidence key, and the
ENGINE-COMPUTED FIGURE. Anyone who could trip the guard was handed an internal
score.

And the inversion made it worse than the leak alone: guards.py imports no
logger and this handler did not log, so the evidence value went to the CALLER
and NOTHING went to the OPERATOR. The one event this platform exists to catch
was legible only to the person who triggered it.

The grep that settled the design: most ValueErrors raised in this codebase were
never written for a caller - settings validation, provider construction,
forecast internals, "no servers resolve under cluster CI 4711". A handful WERE:
"rating must be -1, 0 or 1", "unknown role". So neither blanket rendering nor
blanket suppression is right.

    Rule: a message is INTERNAL by default. An exception opts in with
          `public_detail = True` when its text was written for the caller.

Every handler here now also LOGS what it hid, so suppressing detail costs the
operator nothing.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.agents.guards import NumberDriftError
from app.repositories.base import RowLimitExceeded

logger = structlog.get_logger(__name__)


def _is_public(exc: Exception) -> bool:
    """Whether this exception's own message was written for the caller.

    Absence means private. A new exception type is safe by default and has to
    say otherwise, which is the direction that matters - the previous default
    published everything and nobody had to decide.
    """
    return bool(getattr(exc, "public_detail", False))


class ProblemDetailsError(Exception):
    #: Raised deliberately by route handlers with a message composed FOR the
    #: caller, so its detail is public by construction.
    public_detail = True

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

    @app.exception_handler(NumberDriftError)
    async def handle_number_drift(request: Request, exc: NumberDriftError):
        """The platform's most safety-critical rejection, and its most sensitive
        message.

        Registered ABOVE the ValueError handler because FastAPI dispatches on the
        most specific registered type; without this, NumberDriftError falls into
        handle_value_error and its diagnostic string becomes the body.

        The caller learns the answer was refused and why that is the correct
        outcome. They do not learn the figure. The operator gets the whole thing.
        """
        logger.warning(
            "api.number_drift_rejected",
            detail=str(exc)[:500],
            path=str(request.url.path),
        )
        return _problem_response(
            request, 422, "Answer rejected",
            "The generated explanation stated a figure that does not match the "
            "evidence it was given, so it was discarded rather than shown. This "
            "is the platform refusing to present an unverified number. Retry, or "
            "narrow the request.",
        )

    @app.exception_handler(RowLimitExceeded)
    async def handle_row_limit(request: Request, exc: RowLimitExceeded):
        # Public: the message names a limit and asks for a narrower filter. It
        # describes the REQUEST, not the data behind it. Logged anyway - a
        # sudden rise means a caller is scanning.
        logger.info("api.row_limit_exceeded", detail=str(exc)[:300], path=str(request.url.path))
        return _problem_response(request, 400, "Query too broad", str(exc))

    @app.exception_handler(ValueError)
    async def handle_value_error(request: Request, exc: ValueError):
        """Public only if the exception opted in.

        Most ValueErrors here are internal - settings validation, provider
        construction, forecast bounds, cluster CI ids. A few were written for the
        caller and say so. The suppressed text is always logged, so an operator
        loses nothing by the caller losing it.
        """
        if _is_public(exc):
            return _problem_response(request, 400, "Invalid request", str(exc))
        logger.warning(
            "api.invalid_request_suppressed",
            error_type=type(exc).__name__,
            detail=str(exc)[:500],
            path=str(request.url.path),
        )
        return _problem_response(
            request, 400, "Invalid request",
            "The request could not be processed. If this persists, quote the "
            "correlationId below.",
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        logger.error("api.unhandled_exception", error=str(exc), path=str(request.url.path))
        return _problem_response(request, 500, "Internal server error", "An unexpected error occurred.")
