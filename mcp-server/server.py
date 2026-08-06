"""SeekAndDestroy MCP server.

Exposes 25 tools (search/get read tools, deterministic analysis tools, and a
small set of controlled write tools) and 7 resources over the ai-service
service layer. There is no execute_sql tool and no tool that provisions,
decommissions or migrates infrastructure - see docs/business-rules.md
"Security and governance" for the full list of things this server refuses
to do.

Run with:
    .venv\\Scripts\\python.exe mcp-server\\server.py            (stdio transport)

The ai-service package (../ai-service/app) is added to sys.path below so this
server can import the same deterministic engines the FastAPI service uses -
one implementation of every rule/score/forecast, never two.
"""

from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_AI_SERVICE_DIR = _THIS_DIR.parent / "ai-service"
for p in (str(_THIS_DIR), str(_AI_SERVICE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from mcp.server import MCPServer  # noqa: E402

from resources import cmdb_resources  # noqa: E402
from tools import analysis_tools, dependency_incident_tools, read_tools, write_tools  # noqa: E402

server = MCPServer(
    "seek-and-destroy",
    title="SeekAndDestroy Infrastructure Recommendation Server",
    instructions=(
        "Controlled, read-mostly access to CMDB, capacity, scoring and forecasting data for "
        "SeekAndDestroy. All eligibility rules, capacity math and scores are computed "
        "deterministically server-side - callers (including LLMs) must not recompute or alter "
        "them. There is no SQL execution tool and no infrastructure-mutation tool."
    ),
)

# --- read tools -------------------------------------------------------------
server.add_tool(read_tools.search_applications)
server.add_tool(read_tools.get_application)
server.add_tool(read_tools.get_application_requirements)
server.add_tool(read_tools.get_current_application_hosting)
server.add_tool(read_tools.search_clusters)
server.add_tool(read_tools.get_cluster)
server.add_tool(read_tools.get_cluster_nodes)
server.add_tool(read_tools.get_cluster_utilization)
server.add_tool(read_tools.get_node_utilization)
server.add_tool(read_tools.get_available_cluster_capacity)
server.add_tool(read_tools.list_data_centers)

# --- dependency / incident tools --------------------------------------------
server.add_tool(dependency_incident_tools.get_application_dependencies)
server.add_tool(dependency_incident_tools.get_recent_incidents)

# --- deterministic analysis tools -------------------------------------------
server.add_tool(analysis_tools.calculate_projected_utilization)
server.add_tool(analysis_tools.find_eligible_hosting_candidates)
server.add_tool(analysis_tools.score_hosting_candidates)
server.add_tool(analysis_tools.rank_clusters_by_utilization)
server.add_tool(analysis_tools.run_cluster_right_sizing_analysis)
server.add_tool(analysis_tools.run_application_right_sizing_analysis)
server.add_tool(analysis_tools.run_consolidation_analysis)
server.add_tool(analysis_tools.run_capacity_forecast)

# --- controlled write tools --------------------------------------------------
server.add_tool(write_tools.create_capacity_request)
server.add_tool(write_tools.create_investigation)
server.add_tool(write_tools.get_investigation)
server.add_tool(write_tools.save_recommendation)
server.add_tool(write_tools.list_recommendations)
server.add_tool(write_tools.submit_recommendation_decision)

# --- resources ----------------------------------------------------------------
server.resource("cmdb://schema", name="schema", mime_type="text/markdown")(cmdb_resources.schema_resource)
server.resource("cmdb://business-rules", name="business-rules", mime_type="text/markdown")(cmdb_resources.business_rules_resource)
server.resource("cmdb://scoring-model", name="scoring-model", mime_type="text/markdown")(cmdb_resources.scoring_model_resource)
server.resource("cmdb://applications/{application_id}", name="application", mime_type="application/json")(cmdb_resources.application_resource)
server.resource("cmdb://clusters/{cluster_id}", name="cluster", mime_type="application/json")(cmdb_resources.cluster_resource)
server.resource("cmdb://investigations/{investigation_id}", name="investigation", mime_type="application/json")(cmdb_resources.investigation_resource)
server.resource("cmdb://capacity-requests/{capacity_request_id}", name="capacity-request", mime_type="application/json")(cmdb_resources.capacity_request_resource)


if __name__ == "__main__":
    server.run(transport="stdio")
