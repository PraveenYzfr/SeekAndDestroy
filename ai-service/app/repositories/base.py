"""Database access primitives shared by every repository.

Every query in this codebase goes through :func:`fetch_all`, :func:`fetch_one`,
:func:`execute` or :func:`execute_insert` in this module. All of them take a
``sqlalchemy.text()`` clause with bound parameters - there is no string
interpolation of user-controlled values anywhere, and there is deliberately no
"execute arbitrary SQL" helper (see docs/business-rules.md security section).
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Iterator, Mapping, Sequence

import structlog
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from app.config import get_settings

logger = structlog.get_logger(__name__)


class RowLimitExceeded(RuntimeError):
    """Raised when a query would return more rows than the configured cap."""


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    # Pooling=False in the specified connection string -> NullPool, one
    # connection per unit of work, closed immediately after.
    return create_engine(settings.db.sqlalchemy_url, poolclass=NullPool, future=True)


def reset_engine_cache() -> None:
    get_engine.cache_clear()


@contextmanager
def connection() -> Iterator[Any]:
    engine = get_engine()
    conn = engine.connect()
    try:
        yield conn
    finally:
        conn.close()


def _row_to_dict(row) -> dict[str, Any]:
    return dict(row._mapping)


def fetch_all(
    sql: str,
    params: Mapping[str, Any] | None = None,
    *,
    max_rows: int | None = None,
) -> list[dict[str, Any]]:
    settings = get_settings()
    cap = max_rows if max_rows is not None else settings.service.max_rows
    started = time.perf_counter()
    with connection() as conn:
        result = conn.execute(text(sql), params or {})
        rows = [_row_to_dict(r) for r in result.fetchmany(cap + 1)]
    duration_ms = (time.perf_counter() - started) * 1000
    logger.debug("sql.fetch_all", duration_ms=round(duration_ms, 2), row_count=len(rows), sql=sql[:200])
    if len(rows) > cap:
        raise RowLimitExceeded(f"query returned more than {cap} rows; narrow the filter")
    return rows


def fetch_one(sql: str, params: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    started = time.perf_counter()
    with connection() as conn:
        result = conn.execute(text(sql), params or {})
        row = result.fetchone()
    duration_ms = (time.perf_counter() - started) * 1000
    logger.debug("sql.fetch_one", duration_ms=round(duration_ms, 2), found=row is not None, sql=sql[:200])
    return _row_to_dict(row) if row is not None else None


def execute(sql: str, params: Mapping[str, Any] | None = None) -> int:
    """Run a data-modifying statement. Returns the affected row count."""
    started = time.perf_counter()
    with connection() as conn:
        with conn.begin():
            result = conn.execute(text(sql), params or {})
        rowcount = result.rowcount
    duration_ms = (time.perf_counter() - started) * 1000
    logger.debug("sql.execute", duration_ms=round(duration_ms, 2), rowcount=rowcount, sql=sql[:200])
    return rowcount


def execute_many(sql: str, param_list: Sequence[Mapping[str, Any]]) -> int:
    if not param_list:
        return 0
    started = time.perf_counter()
    with connection() as conn:
        with conn.begin():
            result = conn.execute(text(sql), list(param_list))
        rowcount = result.rowcount
    duration_ms = (time.perf_counter() - started) * 1000
    logger.debug(
        "sql.execute_many", duration_ms=round(duration_ms, 2), rowcount=rowcount,
        batch_size=len(param_list), sql=sql[:200],
    )
    return rowcount


def execute_insert(table: str, pk_column: str, values: Mapping[str, Any]) -> int:
    """Parameterized single-row INSERT returning the new identity value.

    ``table`` and ``pk_column`` are never derived from user input - every call
    site passes a literal string, so there is no injection surface despite the
    f-string below.
    """
    columns = list(values.keys())
    col_list = ", ".join(columns)
    param_list = ", ".join(f":{c}" for c in columns)
    sql = (
        f"INSERT INTO {table} ({col_list}) "
        f"OUTPUT INSERTED.{pk_column} AS NewId "
        f"VALUES ({param_list})"
    )
    started = time.perf_counter()
    with connection() as conn:
        with conn.begin():
            result = conn.execute(text(sql), dict(values))
            new_id = result.scalar_one()
    duration_ms = (time.perf_counter() - started) * 1000
    logger.debug("sql.execute_insert", duration_ms=round(duration_ms, 2), table=table, new_id=new_id)
    return int(new_id)


def schema() -> str:
    return get_settings().db.schema_name


def T(table_name: str) -> str:
    """Schema-qualified table reference, e.g. ``T("CmdbApplication")`` -> ``sad.CmdbApplication``.

    ``table_name`` is always a literal string at call sites (never user input);
    the schema name itself is validated in :class:`app.config.DatabaseSettings`.
    """
    return f"{schema()}.{table_name}"
