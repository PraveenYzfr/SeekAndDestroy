"""Score the models this platform has been using, against its own answer key.

    python scripts/evaluate.py                      # scorecard
    python scripts/evaluate.py --investigation 87   # one investigation
    python scripts/evaluate.py --by-schema          # per chain, not just per model
    python scripts/evaluate.py --json               # machine-readable

As a gate in CI - exits non-zero when a model falls below the bar:

    python scripts/evaluate.py --min-entities 1.0 --min-numbers 0.98

Costs a table scan, not a provider bill: it grades calls that were already
made, from sad.AgentAuditLog, rather than re-running the estate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai-service"))

from app.evaluation.harness import check_thresholds, evaluate  # noqa: E402


def _pct(measured: dict | None) -> str:
    if not measured or measured["rate"] is None:
        return "       -"
    return f"{measured['rate'] * 100:6.1f}%"


def _n(measured: dict | None) -> str:
    return "" if not measured else f"n={measured['observations']}"


def _ms(value: int | None) -> str:
    return "     -" if value is None else f"{value:5d}ms"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--investigation", type=int, default=None, help="grade one investigation only")
    parser.add_argument("--limit", type=int, default=20_000, help="most recent N calls (default 20000)")
    parser.add_argument("--by-schema", action="store_true", help="break each model down by chain")
    parser.add_argument("--json", action="store_true", help="emit the raw result")
    parser.add_argument("--min-numbers", type=float, default=None, help="fail below this number-fidelity rate")
    parser.add_argument("--min-entities", type=float, default=None, help="fail below this entity-fidelity rate")
    parser.add_argument("--min-completeness", type=float, default=None, help="fail below this completeness rate")
    parser.add_argument("--min-observations", type=int, default=20,
                        help="thresholds are skipped below this sample size (default 20)")
    args = parser.parse_args()

    result = evaluate(investigation_id=args.investigation, limit=args.limit)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_report(result, by_schema=args.by_schema)

    thresholds = {
        name: value
        for name, value in (
            ("number_fidelity", args.min_numbers),
            ("entity_fidelity", args.min_entities),
            ("completeness", args.min_completeness),
        )
        if value is not None
    }
    if not thresholds:
        return 0

    problems = check_thresholds(result, thresholds, min_observations=args.min_observations)
    failed = [p for p in problems if p.startswith("FAILED")]
    for problem in problems:
        print(problem, file=sys.stderr)
    # A skipped threshold is not a pass, and is not a failure either - saying so
    # out loud is the only way a thin sample does not read as a clean result.
    return 1 if failed else 0


def _print_report(result: dict, *, by_schema: bool) -> None:
    print(f"\nread {result['calls_seen']} recorded model calls\n")
    if not result["models"]:
        print("nothing to grade - no llm:* rows in sad.AgentAuditLog yet.")
        return

    header = (f"{'model':<30}{'calls':>6}{'gen':>5}{'cache':>6}{'fail':>5}{'ungrad':>7}"
              f"{'p50':>8}{'p95':>8}{'numbers':>9}{'entities':>10}{'complete':>10}")
    print(header)
    print("-" * len(header))
    for m in result["models"]:
        props = m["properties"]
        print(
            f"{m['model']:<30}{m['calls']:>6}{m['generated']:>5}{m['cached']:>6}"
            f"{m['failures']:>5}{m['ungradeable']:>7}"
            f"{_ms(m['latency_p50_ms']):>8}{_ms(m['latency_p95_ms']):>8}"
            f"{_pct(props.get('number_fidelity')):>9}{_pct(props.get('entity_fidelity')):>10}"
            f"{_pct(props.get('completeness')):>10}"
        )
        observations = " ".join(
            f"{name.split('_')[0]} {_n(props.get(name))}"
            for name in ("number_fidelity", "entity_fidelity", "completeness")
            if props.get(name)
        )
        if observations:
            print(f"{'':<30}{observations}")

    if by_schema:
        print("\nby chain:")
        for m in result["models"]:
            if not m["by_schema"]:
                continue
            print(f"\n  {m['model']}")
            for schema, props in m["by_schema"].items():
                rates = "  ".join(
                    f"{name.split('_')[0]} {props[name]['rate'] * 100:5.1f}% (n={props[name]['observations']})"
                    for name in sorted(props)
                )
                print(f"    {schema:<28}{rates}")

    if result["flagged"]:
        print(f"\n{len(result['flagged'])} flagged (first few):")
        for f in result["flagged"][:10]:
            print(f"  audit {f['audit_id']:>6}  {f['schema']:<26} {f['property']:<17} {', '.join(f['ungrounded'])}")

    print(
        "\ncache hits are counted but never graded - the same text served again is not\n"
        "a second success. 'ungrad' is calls whose prompt was capped, where fidelity\n"
        "is not measurable and is excluded rather than guessed. Rates carry their own\n"
        "denominators: 100% over 3 mentions is not the claim 100% over 400 is.\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
