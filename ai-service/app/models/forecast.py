from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class ResourceForecast(BaseModel):
    resource: str  # cpu | memory | storage
    horizon_days: int
    current_percent: Decimal
    predicted_percent: Decimal
    confidence_low_percent: Decimal
    confidence_high_percent: Decimal
    slope_percent_per_day: Decimal
    r_squared: Decimal
    exhaustion_date: Optional[date]
    breaches_threshold_within_horizon: bool
    recommended_action: str
    sample_count: int


class ClusterForecast(BaseModel):
    cluster_id: int
    cluster_code: str
    horizon_days: int
    cpu: ResourceForecast
    memory: ResourceForecast
    storage: ResourceForecast
