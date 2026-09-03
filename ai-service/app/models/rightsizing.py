from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

from app.models.capacity import ClusterCapacitySnapshot


#  NO MONEY IN THIS FILE, AND THE REASON IS THE ESTATE, NOT SQUEAMISHNESS.
#
#  These clusters sit in data centres the bank OWNS. The capacity is paid for
#  whether or not a workload is on it, so powering down a node does not save a
#  pound - it frees capacity that has already been bought. "Estimated monthly
#  savings" described a refund that never happens.
#
#  The figures that used to live here were monthly_cost_per_node,
#  estimated_monthly_savings and estimated_annual_savings, all derived from
#  sad.InfrastructureCluster.MonthlyCost divided by node count. Two things made
#  them worse than merely wrong:
#
#    - the number was floored at zero for expansions, so a cluster needing five
#      more nodes reported 0.00 rather than a cost, and a right-sizing answer
#      could only ever show the flattering half of its own arithmetic;
#    - they were the API's sort key, so "best right-sizing candidate" was
#      decided by them - and a cluster with nothing to do (node_delta 0,
#      floored by N-1 tolerance) outranked a real reduction.
#
#  They did NOT reach the narration model: with_evidence strips any key
#  containing "cost", "saving", "price", "chargeback", "spend", "budget" or
#  "rate_card" before the prompt is built (app/prompts/templates.py), and
#  tests/test_no_cost_in_prompts.py holds that line. What the strip could not
#  fix was RightSizingExplanation asking the model to OUTPUT
#  estimated_monthly_savings - a field whose value had just been removed from
#  its input, so it could only be left null or invented.
#
#  ui/src/pages/ClusterRightSizing.tsx had already stopped DISPLAYING them and
#  said so in a comment - "they are only hidden" - which left the computation,
#  the API response, the sort key and that output contract still carrying them.
#  Hiding a number from one screen is not removing it.
#
#  What replaces them is the thing right-sizing actually buys on owned
#  hardware: RECLAIMED CAPACITY. Signed like node_delta, so the same field
#  answers both directions - negative frees cores, positive needs them - and
#  there is no branch that can quietly floor one of them at zero.
class ClusterRightSizingResult(BaseModel):
    cluster_id: int
    cluster_code: str
    classification: str  # Overprovisioned | Underprovisioned | Healthy
    snapshot: ClusterCapacitySnapshot
    current_node_count: int
    recommended_node_count: int
    node_delta: int
    #: Capacity the node change frees (negative) or requires (positive), in the
    #: units the estate is actually planned in.
    cpu_cores_delta: Decimal
    memory_gb_delta: Decimal
    risks: list[str]
    rationale: str


class ApplicationRightSizingResult(BaseModel):
    application_id: int
    application_code: str
    cluster_code: str
    allocated_cpu_cores: Decimal
    allocated_memory_gb: Decimal
    allocated_storage_gb: Decimal
    measured_cpu_consumed: Optional[Decimal]
    measured_memory_consumed_gb: Optional[Decimal]
    measured_storage_consumed_gb: Optional[Decimal]
    recommended_cpu_cores: Decimal
    recommended_memory_gb: Decimal
    recommended_storage_gb: Decimal
    classification: str  # OverAllocated | UnderAllocated | RightSized
    #: Same reasoning as the cluster result above - owned capacity, so the
    #: recommendation is expressed in cores and gigabytes, not currency.
    cpu_cores_delta: Decimal
    memory_gb_delta: Decimal
    rationale: str


class ConsolidationCandidate(BaseModel):
    application_id: int
    application_code: str
    current_cluster_code: str
    target_cluster_code: str
    reason: str
    #: CPU the SOURCE cluster gets back if this move happens. Was
    #: estimated_monthly_savings, computed as the difference in
    #: MonthlyCost-per-core between the two clusters times the workload's CPU -
    #: a price difference between two racks the bank already owns, which buys
    #: nobody anything. What consolidation actually produces is free capacity
    #: on the cluster being emptied.
    reclaimed_cpu_cores: Decimal
    blocking_constraints: list[str]
    feasible: bool
