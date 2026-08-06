"""The seed generator must be byte-for-byte reproducible (IMPLEMENTATION_PLAN
§7 rule 4). See scripts/generate_seed.py's SEED/ANCHOR_DATE constants."""

from __future__ import annotations

import sys
from pathlib import Path


def test_seed_generation_is_deterministic(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import importlib

    import generate_seed

    importlib.reload(generate_seed)
    first_path = tmp_path / "seed_first.sql"
    generate_seed.OUTPUT_PATH = first_path
    generate_seed.main()

    importlib.reload(generate_seed)
    second_path = tmp_path / "seed_second.sql"
    generate_seed.OUTPUT_PATH = second_path
    generate_seed.main()

    assert first_path.read_text(encoding="utf-8") == second_path.read_text(encoding="utf-8")


def test_seed_scenario_counts_match_specification():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import generate_seed as gs

    assert len(gs.CLUSTERS) == 256
    assert 2000 <= len(gs.NODES) <= 5000
    assert len(gs.APPLICATIONS) == 40
    assert len(gs.EMPLOYEES) == 20
    assert len(gs.SUPPORT_GROUPS) == 15

    s = gs.SCENARIOS
    assert len(s["overprovisioned_clusters"]) == 3
    assert len(s["nearing_cpu_clusters"]) == 2
    assert len(s["nearing_memory_clusters"]) == 2
    assert len(s["high_cost_low_utilization_clusters"]) == 2
    assert len(s["suitable_for_new_workloads_clusters"]) == 3
    assert len(s["insufficient_resiliency_clusters"]) == 2
    assert len(s["compliance_mismatch_clusters"]) == 2
    assert len(s["forecast_exhaustion_clusters"]) == 3
    assert len(s["poor_fit_applications"]) == 5
    assert len(s["consolidation_applications"]) == 4
    assert len(s["expansion_applications"]) == 3
    assert len(s["strong_alternative_applications"]) == 4
