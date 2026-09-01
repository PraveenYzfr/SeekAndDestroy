"""What to offer when the shortlist is not good enough.

This is a search, not a report. When the results are usable the engineer picks
one and leaves; when they are not, the useful next move is a choice - see more,
or ask for less - not an explanation of every rule that failed.

So this module answers two questions:

    what is actually blocking these candidates?
    what smaller request would succeed, and by how much?

Both from arithmetic over candidates that have already been evaluated. Nothing
here re-runs the eligibility engine: re-evaluating 256 clusters against three
hypothetical requirements would be several hundred database round trips on an
interactive path, to produce numbers already implied by the snapshots in hand.

WHY THE SUGGESTIONS ARE COMPUTED AND NOT PHRASED
------------------------------------------------
"Try reducing CPU" is advice anybody could give without looking. "24 cores would
make 9 more clusters eligible" is a fact about this estate at this moment, and
it is the difference between a suggestion an engineer acts on and one they
ignore. Every option below carries the count it would unlock, and options that
unlock nothing are not offered.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

#: Only these are worth proposing to shrink. A rejection on environment or data
#: classification is not negotiable by making the request smaller, and offering
#: it would send the engineer down a path that cannot work.
_SHRINKABLE = ("cpu_cores", "memory_gb", "storage_gb")

#: The rules a smaller request can clear. Leaving RULE-003 out of this was a
#: real error: it rejected 51 of 60 candidates in the first run and was reported
#: as non-negotiable, which would have hidden the most useful offer available.
_CAPACITY_RULES = ("RULE-003", "RULE-009")

#: Fractions of the original request to test. Coarse on purpose: an engineer
#: choosing between 32, 24 and 16 cores is making a capacity decision, and
#: offering 31 would imply a precision the thresholds do not have.
_STEPS = (Decimal("0.75"), Decimal("0.5"), Decimal("0.25"))


@dataclass(frozen=True)
class BlockingReason:
    rule_id: str
    name: str
    #: How many candidates this rule rejected. Ordering by this is what makes
    #: "most of them failed on capacity" visible without reading each one.
    count: int
    #: True when shrinking the request could plausibly clear it. Capacity is
    #: two rules, not one - RULE-003 is "is there enough free right now" and
    #: RULE-009 is "would the projection still fit under the threshold". Both
    #: yield to a smaller request; everything else is a compatibility fact that
    #: no amount of shrinking changes.
    negotiable: bool


@dataclass(frozen=True)
class SizeOption:
    """A smaller request, and what it would actually buy."""

    dimension: str
    label: str
    value: float
    would_make_eligible: int


def blocking_reasons(candidates: list[dict]) -> list[BlockingReason]:
    """Why the rejected candidates were rejected, most common first."""
    tally: dict[str, dict] = {}
    for candidate in candidates:
        if candidate.get("eligibility_status") == "Eligible":
            continue
        for rule in candidate.get("rule_results") or []:
            if rule.get("passed"):
                continue
            key = rule.get("rule_id") or "?"
            entry = tally.setdefault(key, {"name": rule.get("name") or key, "count": 0})
            entry["count"] += 1
    return sorted(
        (
            BlockingReason(rule_id=k, name=v["name"], count=v["count"], negotiable=(k in _CAPACITY_RULES))
            for k, v in tally.items()
        ),
        key=lambda r: -r.count,
    )


def _only_blocked_on_capacity(candidate: dict) -> bool:
    """Whether capacity headroom is this candidate's ONLY failure.

    The distinction matters. A cluster failing both headroom and platform
    compatibility does not become eligible by asking for fewer cores, and
    counting it toward "9 more would be eligible" would make the offer a lie
    the engineer discovers only after taking it.
    """
    failed = [r for r in (candidate.get("rule_results") or []) if not r.get("passed")]
    return bool(failed) and all(r.get("rule_id") in _CAPACITY_RULES for r in failed)


def _plain(value: Decimal) -> str:
    """A Decimal as a person writes it. normalize() produces 2E+1 for 20."""
    quantized = value.quantize(Decimal("1")) if value == value.to_integral_value() else value
    return format(quantized, "f")


def _free(snapshot: dict | None, key: str) -> Decimal | None:
    if not snapshot:
        return None
    value = snapshot.get(key)
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def size_options(candidates: list[dict], requirement: dict) -> list[SizeOption]:
    """Smaller requests worth offering, each with what it would unlock.

    A candidate is counted for a given size only when capacity headroom is its
    sole failure and its free capacity in that dimension covers the smaller
    request. Options that unlock nothing are dropped rather than shown at zero -
    an offer that buys nothing is noise at exactly the moment the engineer is
    already stuck.
    """
    blocked = [c for c in candidates if _only_blocked_on_capacity(c)]
    if not blocked:
        return []

    dimensions = {
        "cpu_cores": ("available_cpu_cores", "cores"),
        "memory_gb": ("available_memory_gb", "GB memory"),
        "storage_gb": ("available_storage_gb", "GB storage"),
    }

    options: list[SizeOption] = []
    for dimension in _SHRINKABLE:
        requested = requirement.get(dimension)
        if requested in (None, ""):
            continue
        try:
            requested_dec = Decimal(str(requested))
        except Exception:  # noqa: BLE001
            continue
        if requested_dec <= 0:
            continue

        snapshot_key, unit = dimensions[dimension]
        for step in _STEPS:
            smaller = (requested_dec * step).quantize(Decimal("1"))
            if smaller <= 0 or smaller >= requested_dec:
                continue
            unlocked = sum(
                1
                for c in blocked
                if (free := _free(c.get("snapshot"), snapshot_key)) is not None and free >= smaller
            )
            if unlocked:
                options.append(
                    SizeOption(
                        dimension=dimension,
                        # _plain, not normalize(): Decimal("20").normalize() is
                        # 2E+1, and an offer reading "15 cores instead of 2E+1"
                        # is not an offer anyone acts on.
                        label=f"{_plain(smaller)} {unit} instead of {_plain(requested_dec)}",
                        value=float(smaller),
                        would_make_eligible=unlocked,
                    )
                )
                # One option per dimension: the largest request that helps. A
                # list of three shrinking sizes for the same dimension asks the
                # engineer to compare our arithmetic instead of making a
                # capacity decision.
                break
    return options


def next_steps(
    candidates: list[dict], requirement: dict | None, *, shown: int, offset: int = 0
) -> dict:
    """The choice to offer when the shortlist is thin.

    Returned on every investigation but only meaningful when it is. With a full
    shortlist of eligible options `sufficient` is True and the UI shows nothing:
    the engineer picks one and leaves, which is the common case and should stay
    the quiet one.

    ``offset`` is how far into the ranked list the caller already is, so "show
    me the next three" is a slice of the same match rather than a new search.
    The next three may be on the same clusters or different ones - that is a
    property of the ranking, not a mode to choose, and asking the engineer which
    they wanted would be asking them to do the ranking's job.
    """
    eligible = [c for c in candidates if c.get("eligibility_status") == "Eligible"]
    reasons = blocking_reasons(candidates)
    options = size_options(candidates, requirement or {})
    remaining = max(0, len(eligible) - (offset + shown))
    sufficient = shown > 0 and len(eligible) >= offset + shown

    # The moves available, as things to press rather than prose to read. Only
    # offered when they lead somewhere: "show the next 3" with nothing left
    # behind it wastes the one interaction the engineer has when stuck.
    choices: list[dict] = []
    if remaining:
        choices.append({
            "action": "show_more",
            "label": f"Show the next {min(shown or 3, remaining)}",
            "detail": f"{remaining} more eligible candidate(s) in this ranking",
            "next_offset": offset + shown,
        })
    for option in options:
        choices.append({
            "action": "refine_requirement",
            "label": option.label,
            "detail": f"would make {option.would_make_eligible} more cluster(s) eligible",
            "dimension": option.dimension,
            "value": option.value,
        })
    hard_blocks = [r for r in reasons if not r.negotiable][:2]
    if not eligible and hard_blocks:
        # Nothing to shrink toward. Say which constraint is doing it, once,
        # rather than listing every rule every candidate failed - that is the
        # detailed summary this replaced.
        choices.append({
            "action": "change_constraints",
            "label": "Change the search constraints",
            "detail": "blocked by "
            + ", ".join(f"{r.name.lower()} ({r.count})" for r in hard_blocks),
        })

    return {
        "eligible_total": len(eligible),
        "shown": shown,
        "offset": offset,
        "more_available": remaining,
        "sufficient": sufficient,
        "choices": choices,
        "blocking_reasons": [
            {"rule_id": r.rule_id, "name": r.name, "count": r.count, "negotiable": r.negotiable}
            for r in reasons[:3]
        ],
        "size_options": [
            {
                "dimension": o.dimension,
                "label": o.label,
                "value": o.value,
                "would_make_eligible": o.would_make_eligible,
            }
            for o in options
        ],
    }
