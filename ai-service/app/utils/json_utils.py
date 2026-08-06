"""JSON response conversion that keeps numbers as numbers.

FastAPI's ``jsonable_encoder`` hardcodes ``mode="json"`` when it encounters a
``pydantic.BaseModel`` (see ``fastapi/encoders.py``), which delegates to
Pydantic's own JSON-mode serialization - and Pydantic v2 serializes
``Decimal`` fields as **strings** in JSON mode (to avoid float precision
loss), with no parameter on ``jsonable_encoder`` able to override it once a
``BaseModel`` is in the object graph. Every UI numeric field (scores, costs,
utilization percentages) would arrive as a string and silently break
arithmetic (`0 + "32500.00"` is string concatenation in JavaScript, not
addition).

:func:`to_jsonable` recurses through the object graph itself, calling
``model.model_dump()`` (mode="python", the default - keeps ``Decimal`` and
``datetime`` as native objects) instead of ``mode="json"``, then converts
those native objects to JSON-safe types explicitly: ``Decimal`` -> ``float``,
``date``/``datetime`` -> ISO string.

Used in two places that must stay consistent with each other: every FastAPI
route (`app/api/routes_*.py`, in place of `fastapi.encoders.jsonable_encoder`)
and `app/graph/nodes.py` wherever a Decimal-bearing result is written into
LangGraph state - the state gets checkpointed to SQLite and later returned
from `/api/investigations`, so it has to be numerically correct too, not just
the direct API responses.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return to_jsonable(obj.model_dump())
    if isinstance(obj, dict):
        return {key: to_jsonable(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(item) for item in obj]
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj
