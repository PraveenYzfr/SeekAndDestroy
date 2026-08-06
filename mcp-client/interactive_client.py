"""Interactive command-line MCP client for SeekAndDestroy.

Routing from natural language to a tool call is deterministic (regex/keyword
matching on application codes and intent verbs) - per the platform's trust
boundary, the LLM never chooses which infrastructure or which tool to call;
it only narrates results the deterministic engines already produced. This
mirrors exactly how app/graph/router.py routes the LangGraph workflow.

Run with:
    .venv\\Scripts\\python.exe mcp-client\\interactive_client.py
    .venv\\Scripts\\python.exe mcp-client\\interactive_client.py --query "Find the best clusters for hosting APP-PAYMENTS."
    .venv\\Scripts\\python.exe mcp-client\\interactive_client.py --demo
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai-service"))

from client import call_tool, in_process_client  # noqa: E402


def _demo_access_token(employee_id: int = 1) -> str:
    """A local-mode dev token identifying this demo CLI as a real, seeded
    Employee row (E1001) - write tools now require one. See
    app.security.jwt_service; only works when SAD_AUTH__MODE=local."""
    from app.security.jwt_service import create_local_token

    return create_local_token(
        employee_id=employee_id, employee_number="E1001", display_name="Aditi Sharma",
        email="aditi.sharma@seekanddestroy.example",
    )

DEMO_QUERIES = [
    "Find the best clusters for hosting APP-PAYMENTS.",
    "Where can I place a workload requiring 16 CPU cores, 64 GB RAM and 2 TB storage?",
    "Show clusters with at least 30% projected headroom.",
    "Which clusters require right-sizing?",
    "Find high-cost underutilized clusters.",
    "Which applications can be safely consolidated?",
    "Forecast capacity for CL-PROD-03 for the next 90 days.",
    "Why was CL-PROD-05 rejected?",
    "Compare CL-PROD-02 and CL-PROD-04 for APP-CRM.",
    "Generate a hosting recommendation report.",
]

_APP_CODE_RE = re.compile(r"\bAPP-[A-Z0-9]+\b")
_CLUSTER_CODE_RE = re.compile(r"\bCL-[A-Z0-9-]+\b")


def _print_header(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _print_candidates(label: str, candidates: list[dict]) -> None:
    if not candidates:
        print(f"  ({label}: none)")
        return
    print(f"  {label}:")
    for c in candidates:
        score = c.get("overall_score")
        cost = c.get("estimated_monthly_cost")
        print(f"    rank={c.get('rank')} {c['cluster_code']:14s} status={c['eligibility_status']:9s} score={score} cost={cost}")


async def handle_hosting(client, app_code: str, tools_invoked: list[str]) -> None:
    tools_invoked.append("get_application_requirements")
    req = await call_tool(client, "get_application_requirements", {"application_code": app_code})
    _print_header("Interpreted requirements")
    print(f"  {req}")

    tools_invoked.append("score_hosting_candidates")
    result = await call_tool(client, "score_hosting_candidates", {"application_code": app_code})
    candidates = result.get("candidates", [])
    eligible = [c for c in candidates if c["eligibility_status"] == "Eligible"]
    rejected = [c for c in candidates if c["eligibility_status"] != "Eligible"]

    _print_header("Candidates")
    _print_candidates("Eligible (ranked)", eligible)
    _print_candidates("Rejected", rejected)

    if eligible:
        top = eligible[0]
        _print_header(f"Top candidate detail: {top['cluster_code']}")
        print(f"  Projected utilization: {top.get('projected')}")
        print(f"  Sub-scores: {top.get('subscores')}")
        _explain(top, app_code)

    _print_header("Human action required")
    print("  Review the ranked candidates above and submit a decision via "
          "submit_recommendation_decision (Approve / Reject / RequestMoreAnalysis).")


def _explain(candidate: dict, app_code: str | None) -> None:
    from app.agents.llm_factory import get_chat_model
    from app.agents import chains
    from app.models.scoring import CandidateScore

    llm = get_chat_model()
    cs = CandidateScore.model_validate(candidate)
    explanation = chains.explain_candidate(llm, cs, app_code)
    print(f"  AI explanation: {explanation.summary}")
    if explanation.key_strengths:
        print(f"  Strengths: {explanation.key_strengths}")
    if explanation.key_risks:
        print(f"  Risks: {explanation.key_risks}")


async def handle_raw_capacity(client, cpu: float, mem: float, storage: float, tools_invoked: list[str]) -> None:
    tools_invoked.append("score_hosting_candidates")
    result = await call_tool(
        client, "score_hosting_candidates",
        {
            "cpu_cores": cpu, "memory_gb": mem, "storage_gb": storage, "platform": "Kubernetes",
            "environment": "Production", "availability_tier": "Tier-2", "data_classification": "Internal",
        },
    )
    candidates = result.get("candidates", [])
    _print_header("Candidates for raw capacity requirement")
    _print_candidates("Eligible (ranked)", [c for c in candidates if c["eligibility_status"] == "Eligible"])
    _print_candidates("Rejected", [c for c in candidates if c["eligibility_status"] != "Eligible"])


async def handle_headroom(client, min_headroom: float, tools_invoked: list[str]) -> None:
    tools_invoked.append("search_clusters")
    clusters = await call_tool(client, "search_clusters", {"limit": 50})
    _print_header(f"Clusters with >= {min_headroom}% projected headroom (self-projection)")
    for c in clusters:
        tools_invoked.append("calculate_projected_utilization")
        proj = await call_tool(
            client, "calculate_projected_utilization",
            {"cluster_code": c["ClusterCode"], "cpu_cores": 0, "memory_gb": 0, "storage_gb": 0},
        )
        headroom = proj.get("projected", {}).get("projected_headroom_percent")
        if headroom is not None and headroom >= min_headroom:
            print(f"  {c['ClusterCode']:14s} headroom={headroom}%")


async def handle_rightsizing(client, tools_invoked: list[str]) -> None:
    tools_invoked.append("run_cluster_right_sizing_analysis")
    result = await call_tool(client, "run_cluster_right_sizing_analysis", {})
    _print_header("Clusters requiring right-sizing")
    for r in result.get("results", []):
        if r["classification"] != "Healthy":
            print(f"  {r['cluster_code']:14s} {r['classification']:16s} nodes {r['current_node_count']}->{r['recommended_node_count']} savings=${r['estimated_monthly_savings']}/mo")


async def handle_high_cost_low_util(client, tools_invoked: list[str]) -> None:
    tools_invoked.append("run_cluster_right_sizing_analysis")
    result = await call_tool(client, "run_cluster_right_sizing_analysis", {})
    _print_header("High-cost, underutilized clusters")
    rows = [r for r in result.get("results", []) if r["classification"] == "Overprovisioned"]
    rows.sort(key=lambda r: -float(r["monthly_cost_per_node"]))
    for r in rows:
        print(f"  {r['cluster_code']:14s} cost/node=${r['monthly_cost_per_node']} cpu%={r['snapshot']['current_cpu_utilization_percent']}")


async def handle_consolidation(client, tools_invoked: list[str]) -> None:
    tools_invoked.append("run_consolidation_analysis")
    result = await call_tool(client, "run_consolidation_analysis", {})
    _print_header("Consolidation candidates")
    for r in result.get("results", []):
        if r["feasible"]:
            print(f"  {r['application_code']:16s} {r['current_cluster_code']} -> {r['target_cluster_code']} savings=${r['estimated_monthly_savings']}/mo")


async def handle_forecast(client, cluster_code: str, horizon: int, tools_invoked: list[str]) -> None:
    tools_invoked.append("run_capacity_forecast")
    result = await call_tool(client, "run_capacity_forecast", {"cluster_code": cluster_code, "horizon_days": horizon})
    _print_header(f"Capacity forecast for {cluster_code} ({horizon} days)")
    for resource in ("cpu", "memory", "storage"):
        r = result[resource]
        print(f"  {resource:8s} {r['current_percent']}% -> {r['predicted_percent']}% "
              f"(exhaustion: {r['exhaustion_date']}, breach in horizon: {r['breaches_threshold_within_horizon']})")
        print(f"           action: {r['recommended_action']}")


async def handle_why_rejected(client, cluster_code: str, tools_invoked: list[str]) -> None:
    # At 256 clusters, find_eligible_hosting_candidates pre-filters by
    # environment before evaluating anything (cheap SQL narrowing instead of
    # evaluating all 256 candidates per request - see placement.py). A fixed
    # "Production" probe would silently miss a non-Production target cluster,
    # so this looks the cluster up first and probes with ITS OWN environment -
    # a demanding Tier-1/Restricted workload still exercises every other rule.
    tools_invoked.append("get_cluster")
    cluster = await call_tool(client, "get_cluster", {"cluster_code": cluster_code})
    if not cluster or "error" in cluster:
        print(f"  {cluster_code} not found.")
        return

    tools_invoked.append("find_eligible_hosting_candidates")
    result = await call_tool(
        client, "find_eligible_hosting_candidates",
        {
            "cpu_cores": 8, "memory_gb": 32, "storage_gb": 500, "platform": "Kubernetes",
            "environment": cluster["Environment"], "availability_tier": "Tier-1", "data_classification": "Restricted",
        },
    )
    _print_header(f"Why was {cluster_code} rejected? (Tier-1/Restricted probe workload in {cluster['Environment']})")
    for c in result.get("rejected", []):
        if c["cluster_code"] == cluster_code:
            for rule in c["rule_results"]:
                if not rule["passed"]:
                    print(f"  FAIL {rule['rule_id']}: {rule['reason']}")
            return
    for c in result.get("eligible", []):
        if c["cluster_code"] == cluster_code:
            print(f"  {cluster_code} was actually eligible for this probe workload.")
            return
    print(f"  {cluster_code} did not appear in the candidate set for this probe (unexpected).")


async def handle_compare(client, cluster_codes: list[str], app_code: str, tools_invoked: list[str]) -> None:
    tools_invoked.append("score_hosting_candidates")
    result = await call_tool(client, "score_hosting_candidates", {"application_code": app_code})
    by_code = {c["cluster_code"]: c for c in result.get("candidates", [])}
    _print_header(f"Comparing {', '.join(cluster_codes)} for {app_code}")
    for code in cluster_codes:
        c = by_code.get(code)
        if not c:
            print(f"  {code}: not evaluated")
            continue
        print(f"  {code}: status={c['eligibility_status']} score={c.get('overall_score')} cost={c.get('estimated_monthly_cost')}")
        if c["eligibility_status"] != "Eligible":
            for rule in c["rule_results"]:
                if not rule["passed"]:
                    print(f"      FAIL {rule['rule_id']}: {rule['reason']}")


async def handle_report(client, tools_invoked: list[str]) -> None:
    tools_invoked.append("create_investigation")
    inv = await call_tool(client, "create_investigation", {
        "query": "Generate a hosting recommendation report.", "investigation_type": "Question",
        "created_by_employee_id": 1, "access_token": _demo_access_token(),
    })
    _print_header("Investigation created")
    print(f"  {inv}")
    print("  Run app/graph/graph.py::run_investigation for the full LangGraph-orchestrated report.")


async def route(client, query: str) -> None:
    tools_invoked: list[str] = []
    lower = query.lower()
    app_codes = _APP_CODE_RE.findall(query.upper())
    cluster_codes = _CLUSTER_CODE_RE.findall(query.upper())

    if "why was" in lower and cluster_codes:
        await handle_why_rejected(client, cluster_codes[0], tools_invoked)
    elif "compare" in lower and len(cluster_codes) >= 2 and app_codes:
        await handle_compare(client, cluster_codes[:2], app_codes[0], tools_invoked)
    elif "forecast" in lower and cluster_codes:
        horizon = 90
        m = re.search(r"(\d+)\s*day", lower)
        if m:
            horizon = int(m.group(1))
        await handle_forecast(client, cluster_codes[0], horizon, tools_invoked)
    elif "right-siz" in lower or "right siz" in lower:
        await handle_rightsizing(client, tools_invoked)
    elif "high-cost" in lower or "high cost" in lower or "underutilized" in lower:
        await handle_high_cost_low_util(client, tools_invoked)
    elif "consolidat" in lower:
        await handle_consolidation(client, tools_invoked)
    elif "headroom" in lower:
        m = re.search(r"(\d+)\s*%", lower)
        min_headroom = float(m.group(1)) if m else 30.0
        await handle_headroom(client, min_headroom, tools_invoked)
    elif "report" in lower:
        await handle_report(client, tools_invoked)
    elif app_codes:
        await handle_hosting(client, app_codes[0], tools_invoked)
    else:
        m = re.search(r"(\d+)\s*cpu", lower)
        cpu = float(m.group(1)) if m else 8.0
        m = re.search(r"(\d+)\s*gb\s*ram", lower) or re.search(r"(\d+)\s*gb", lower)
        mem = float(m.group(1)) if m else 32.0
        m = re.search(r"(\d+)\s*tb", lower)
        storage = float(m.group(1)) * 1000 if m else 500.0
        await handle_raw_capacity(client, cpu, mem, storage, tools_invoked)

    _print_header("Tools invoked")
    for t in tools_invoked:
        print(f"  - {t}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="SeekAndDestroy interactive MCP client")
    parser.add_argument("--query", type=str, default=None)
    parser.add_argument("--demo", action="store_true", help="run all 10 specification demo queries")
    args = parser.parse_args()

    client_cm = in_process_client()
    async with client_cm as client:
        if args.demo:
            for q in DEMO_QUERIES:
                _print_header(f"QUERY: {q}")
                await route(client, q)
            return

        if args.query:
            await route(client, args.query)
            return

        print("SeekAndDestroy interactive client. Type a request, or 'quit'.")
        while True:
            try:
                query = input("\n> ").strip()
            except EOFError:
                break
            if query.lower() in ("quit", "exit"):
                break
            if not query:
                continue
            await route(client, query)


if __name__ == "__main__":
    asyncio.run(main())
