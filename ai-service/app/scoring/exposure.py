"""How much a cluster's change churn is amplified by what depends on it.

WHY THIS LIVES IN scoring AND NOT IN services
---------------------------------------------
It used to live in services.change_exposure, and app.scoring.subscores reached
up into services to call it - a function-local import, written that way to break
the cycle that a module-level one would have created.

That local import was the tell. `scoring` is the layer the platform's trust
boundary rests on: it is where numbers come from, and the reason a recommendation
can be believed is that nothing in it is decided anywhere else. A scoring module
that depends on a service is a scoring module whose result depends on whatever
that service later grows into.

Nothing about this function needed a service. It takes an int and returns a
Decimal - no database, no I/O, no configuration. It sat in change_exposure.py
because its one caller's DATA comes from there, next to exposure_for_clusters(),
which genuinely does query the CMDB. Proximity to the data source is not the
same as belonging to it.

So the split is: services.change_exposure fetches WHAT DEPENDS ON A CLUSTER,
and this decides WHAT THAT IS WORTH. The first needs a database; the second is
arithmetic, and arithmetic belongs with the other arithmetic.
"""

from __future__ import annotations

from decimal import Decimal

#: Dependent applications at which the churn penalty is doubled. Chosen from the
#: estate as measured - the busiest cluster in a forty-cluster sample carries 27
#: dependent applications against a median of 4 - so this sits just above the
#: observed top and a typical cluster lands well under it.
#:
#: The estate is about to grow roughly forty-fold. Revisit this then: it is an
#: absolute threshold by deliberate choice, which means it does not
#: self-calibrate and will understate exposure if the average cluster ends up
#: carrying far more than it does today.
EXPOSURE_REFERENCE_APPS = Decimal("30")

#: Ceiling on the multiplier. Without it a hub cluster could contribute an
#: unbounded penalty and dominate a score that has six other dimensions.
MAX_EXPOSURE_MULTIPLIER = Decimal("2.0")


def exposure_multiplier(dependent_applications: int | None) -> Decimal:
    """1.0 for a cluster nothing depends on, rising to MAX at the reference.

    Returns 1.0 - no effect - for None. Absent exposure data must leave the
    change score exactly as it was rather than inventing a penalty, for the same
    reason RULE-012 passes on silence: an incomplete CMDB must not quietly make
    an estate look dangerous.
    """
    if not dependent_applications or dependent_applications <= 0:
        return Decimal("1.0")
    ratio = Decimal(dependent_applications) / EXPOSURE_REFERENCE_APPS
    return min(MAX_EXPOSURE_MULTIPLIER, Decimal("1.0") + ratio)
