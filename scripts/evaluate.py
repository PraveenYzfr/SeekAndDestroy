"""Print a scorecard for the models this platform has been using.

    python scripts/evaluate.py                # everything in the audit log
    python scripts/evaluate.py --investigation 87
    python scripts/evaluate.py --json         # machine-readable

Costs nothing but a table scan: it grades calls that were already made, from
sad.AgentAuditLog, rather than re-running the estate against a provider.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai-service"))

from app.evaluation.harness import evaluate  # noqa: E402


def _pct(value: float | None) -> str:
    return "     -" if value is None else f"{value * 100:5.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--investigation", type=int, default=None, help="grade one investigation only")
    parser.add_argument("--limit", type=int, default=2000, help="most recent N calls (default 2000)")
    parser.add_argument("--json", action="store_true", help="emit the raw result")
    args = parser.parse_args()

    result = evaluate(investigation_id=args.investigation, limit=args.limit)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"\ngraded {result['calls_graded']} recorded model calls\n")
    if not result["models"]:
        print("nothing to grade - no llm:* rows in sad.AgentAuditLog yet.")
        return 0

    header = (f"{'model':<34}{'calls':>7}{'fail':>6}{'cached':>8}{'ungrad':>8}"
              f"{'numbers':>9}{'entities':>10}{'complete':>10}")
    print(header)
    print("-" * len(header))
    for m in result["models"]:
        print(
            f"{m['model']:<34}{m['calls']:>7}{m['failures']:>6}{m['cache_hits']:>8}{m['ungradeable']:>8}"
            f"{_pct(m['number_fidelity']):>9}{_pct(m['entity_fidelity']):>10}{_pct(m['completeness']):>10}"
        )

    if result["flagged"]:
        print(f"\n{len(result['flagged'])} flagged (first few):")
        for f in result["flagged"][:10]:
            print(f"  audit {f['audit_id']:>6}  {f['schema']:<26} {f['property']:<17} {', '.join(f['ungrounded'])}")
        print(
            "\nnumbers are a rate, not a verdict: rounding and counts are treated as grounded,\n"
            "so what is flagged here is a figure that rounds to nothing it was given."
        )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
