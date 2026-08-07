from __future__ import annotations

from decimal import Decimal

from app.config import get_settings


def get_weights() -> dict[str, Decimal]:
    """Configurable weighted-scoring weights, as Decimal. Always sums to 1.0
    (enforced by :class:`app.config.settings.ScoringSettings`).
    """
    w = get_settings().scoring.as_dict()
    return {k: Decimal(str(v)) for k, v in w.items()}


def get_node_weights() -> dict[str, Decimal]:
    """Node-level weights (capacity/cost/reliability/risk), as Decimal. Always
    sums to 1.0 (enforced by :class:`app.config.settings.ScoringSettings`).
    """
    w = get_settings().scoring.node_weights_as_dict()
    return {k: Decimal(str(v)) for k, v in w.items()}
