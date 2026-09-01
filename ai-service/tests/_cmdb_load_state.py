"""Tell an empty CMDB apart from a half-loaded one, and fail on the second.

WHY THIS EXISTS
---------------
The resiliency suite went from 22 passing to 14 skipped during a seed reload and
reported success. A suite that skips is green, and a green suite that asserted
nothing is a false negative with a friendly face - the same defect as a golden
set that cannot fail or a rule that fires on everything, wearing different
clothes.

Skipping on an EMPTY database is right: nothing is loaded, there is nothing to
assert, and failing would make the suite unrunnable on a fresh checkout.

Skipping on a HALF-LOADED database is wrong, and worse than the empty case. A run
against a fraction of the estate passes, means nothing, and looks identical to a
run against all of it.

WHY NOT A MINIMUM ROW COUNT
---------------------------
The obvious guard - fail if edges are below some floor - conflates two different
things. Before tonight's load the graph held 4,290 edges and that was a complete,
correct estate; tonight's holds around 85,000. A floor set for tonight would have
failed against last week's legitimate database, and a floor set for last week
would pass against a load that is 5% done.

Size cannot distinguish "partially loaded" from "smaller". Structure can:

  A load in progress writes ConfigurationItem rows and CiRelationship rows at
  different moments, so mid-flight there are edges whose endpoints do not exist
  yet. A complete CMDB of ANY size has none.

That check is independent of how big the estate is, which is the property the row
count does not have.
"""

from __future__ import annotations

import pytest

from app.repositories.base import T, fetch_all

EMPTY = "empty"
PARTIAL = "partial"
LOADED = "loaded"


def cmdb_load_state() -> tuple[str, str]:
    """Return (state, human-readable detail)."""
    try:
        cis = fetch_all(f"SELECT COUNT(*) AS C FROM {T('ConfigurationItem')}", max_rows=1)[0]["C"]
        edges = fetch_all(f"SELECT COUNT(*) AS C FROM {T('CiRelationship')}", max_rows=1)[0]["C"]
    except Exception as exc:  # noqa: BLE001
        return EMPTY, f"CMDB tables unavailable: {exc}"

    if cis == 0 and edges == 0:
        return EMPTY, "no CI graph loaded"
    if cis == 0 or edges == 0:
        return PARTIAL, f"{cis} CIs and {edges} edges - one without the other is a load in flight"

    dangling = fetch_all(
        f"SELECT COUNT(*) AS C FROM {T('CiRelationship')} r "
        f"WHERE NOT EXISTS (SELECT 1 FROM {T('ConfigurationItem')} p WHERE p.CiId = r.ParentCiId) "
        f"   OR NOT EXISTS (SELECT 1 FROM {T('ConfigurationItem')} c WHERE c.CiId = r.ChildCiId)",
        max_rows=1,
    )[0]["C"]
    if dangling:
        return PARTIAL, f"{dangling} of {edges} edges reference a CI that does not exist"
    return LOADED, f"{cis} CIs, {edges} edges"


def require_loaded_graph() -> None:
    """Skip on an empty CMDB. FAIL on a partial one.

    The asymmetry is the whole point. Call this from any fixture that would
    otherwise skip when the graph is missing.
    """
    state, detail = cmdb_load_state()
    if state == EMPTY:
        pytest.skip(detail)
    if state == PARTIAL:
        pytest.fail(
            f"CMDB is mid-load ({detail}). Refusing to report a pass against a partial "
            f"estate - a run in this window asserts almost nothing and looks identical "
            f"to a complete one. Wait for the load to finish and re-run."
        )
