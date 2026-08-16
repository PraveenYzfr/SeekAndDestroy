"""Optional prose on the structured endpoints.

Three chains in app.agents.chains - ``explain_forecast``,
``summarize_tradeoffs`` and ``explain_application_right_sizing`` - were
written, guarded and tested, and called from nowhere. The screens they belong
to rendered numbers with no narration beside them. This module is where they
attach.

Two rules, both of which the graph pipeline already follows:

**Narration is opt-in.** These endpoints answer with up to 500 clusters or 200
applications; narrating each one would be hundreds of model calls for a single
request. The caller asks for prose with ``explain=true`` and gets it for a
bounded number of items.

**Narration never breaks the answer.** A model failure, a quota refusal or a
number-drift rejection costs the prose and nothing else - the deterministic
result is already computed and is what the caller actually came for. This is
the same degradation the graph applies (see app.graph.nodes), stated once here
rather than repeated at each call site.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import structlog

logger = structlog.get_logger(__name__)

#: How many items one request may narrate. A caller asking to explain every
#: application in the estate is asking for 200 model calls; they get the most
#: significant handful and a count of what was left unnarrated.
MAX_NARRATED = 5


def safely(what: str, call: Callable[[], Any]) -> Optional[dict]:
    """Run a narration chain, returning None instead of raising.

    ``what`` names the call in the log, so a run of "Report narration
    unavailable" can be traced to the chain and entity that failed rather than
    inferred.
    """
    try:
        result = call()
    except Exception as exc:  # noqa: BLE001 - narration is never a hard dependency
        logger.warning("narration.failed", chain=what, error=str(exc))
        return None
    return result.model_dump() if hasattr(result, "model_dump") else result


def binding_resource(forecast) -> tuple[str, Any]:
    """The resource that runs out first, which is the one worth explaining.

    A cluster forecast covers CPU, memory and storage. Narrating all three
    triples the cost to say two things nobody asked about: the constraint is
    whichever breaches soonest, and if none breaches, whichever is closest.
    """
    resources = [("cpu", forecast.cpu), ("memory", forecast.memory), ("storage", forecast.storage)]

    breaching = [(name, r) for name, r in resources if r.breaches_threshold_within_horizon]
    pool = breaching or resources

    def sort_key(item):
        _, r = item
        # Earliest exhaustion first; among those with no date, highest
        # predicted utilization. date.max keeps the undated ones last without
        # a second pass.
        from datetime import date

        return (r.exhaustion_date or date.max, -float(r.predicted_percent))

    return sorted(pool, key=sort_key)[0]
