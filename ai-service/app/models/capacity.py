"""Result shapes for the capacity engine. Every numeric field is a ``Decimal``
computed via :mod:`app.services.capacity` - never touched by the LLM.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class ClusterCapacitySnapshot(BaseModel):
    cluster_id: int
    cluster_code: str

    effective_cpu_cores: Decimal
    effective_memory_gb: Decimal
    effective_storage_gb: Decimal

    allocated_cpu_cores: Decimal
    allocated_memory_gb: Decimal
    allocated_storage_gb: Decimal

    measured_cpu_percent: Optional[Decimal]
    measured_memory_percent: Optional[Decimal]
    measured_storage_percent: Optional[Decimal]
    measurement_sample_count: int

    consumed_cpu_cores: Decimal
    consumed_memory_gb: Decimal
    consumed_storage_gb: Decimal

    available_cpu_cores: Decimal
    available_memory_gb: Decimal
    available_storage_gb: Decimal

    current_cpu_utilization_percent: Decimal
    current_memory_utilization_percent: Decimal
    current_storage_utilization_percent: Decimal


class NodeCapacitySnapshot(BaseModel):
    """The per-node equivalent of :class:`ClusterCapacitySnapshot`.

    Same conservative ``consumed = max(allocated, measured)`` rule, with two
    node-specific caveats worth knowing when reading the numbers:

    * ``allocated_*`` only counts ApplicationHosting rows *pinned to this node*
      (``NodeId`` is nullable and most hosting rows record only the cluster),
      so on an unpinned estate allocation is 0 and ``consumed`` is purely the
      measured figure.
    * ``measured_*`` comes from NodeUtilization. A node with no metrics in the
      window has ``measurement_sample_count == 0`` and is reported as
      ``has_measurements=False`` rather than silently scored as empty.
    """

    node_id: int
    cluster_id: int
    host_name: str

    effective_cpu_cores: Decimal
    effective_memory_gb: Decimal
    effective_storage_gb: Decimal

    allocated_cpu_cores: Decimal
    allocated_memory_gb: Decimal
    allocated_storage_gb: Decimal

    measured_cpu_percent: Optional[Decimal]
    measured_memory_percent: Optional[Decimal]
    measured_storage_percent: Optional[Decimal]
    measurement_sample_count: int
    has_measurements: bool

    consumed_cpu_cores: Decimal
    consumed_memory_gb: Decimal
    consumed_storage_gb: Decimal

    available_cpu_cores: Decimal
    available_memory_gb: Decimal
    available_storage_gb: Decimal

    current_cpu_utilization_percent: Decimal
    current_memory_utilization_percent: Decimal
    current_storage_utilization_percent: Decimal


class ProjectedUtilization(BaseModel):
    required_cpu_cores_effective: Decimal
    required_memory_gb_effective: Decimal
    required_storage_gb_effective: Decimal

    projected_cpu_utilization_percent: Decimal
    projected_memory_utilization_percent: Decimal
    projected_storage_utilization_percent: Decimal
    projected_headroom_percent: Decimal

    fits_cpu: bool
    fits_memory: bool
    fits_storage: bool
    fits_all: bool
