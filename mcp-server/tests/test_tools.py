from __future__ import annotations

import json

import pytest
from mcp import Client


@pytest.mark.asyncio
async def test_server_exposes_exactly_27_tools(mcp_server):
    # 25 from the original specification + list_data_centers +
    # rank_clusters_by_utilization (data-center browsing / least-most-used
    # cluster views added after infra-engineer feedback on the UX).
    async with Client(mcp_server) as client:
        listed = await client.list_tools()
        assert len(listed.tools) == 27


@pytest.mark.asyncio
async def test_rank_clusters_by_utilization_least_and_most(mcp_server):
    async with Client(mcp_server) as client:
        least = json.loads((await client.call_tool("rank_clusters_by_utilization", {"order": "least", "limit": 3})).content[0].text)
        most = json.loads((await client.call_tool("rank_clusters_by_utilization", {"order": "most", "limit": 3})).content[0].text)
        assert len(least["results"]) == 3
        assert len(most["results"]) == 3
        # Least-used and most-used should not be the same top-3 set.
        assert {r["cluster_code"] for r in least["results"]} != {r["cluster_code"] for r in most["results"]}
        # Ascending vs descending sort actually holds.
        least_vals = [r["overall_utilization_percent"] for r in least["results"]]
        most_vals = [r["overall_utilization_percent"] for r in most["results"]]
        assert least_vals == sorted(least_vals)
        assert most_vals == sorted(most_vals, reverse=True)


@pytest.mark.asyncio
async def test_score_hosting_candidates_respects_top_n_and_data_center(mcp_server):
    async with Client(mcp_server) as client:
        result = json.loads(
            (await client.call_tool("score_hosting_candidates", {"application_code": "APP-CRM", "top_n": 2})).content[0].text
        )
        eligible = [c for c in result["candidates"] if c["eligibility_status"] == "Eligible"]
        assert len(eligible) <= 2

        dc_result = json.loads(
            (
                await client.call_tool(
                    "score_hosting_candidates", {"application_code": "APP-CRM", "data_center": "Atlanta-DC1"}
                )
            ).content[0].text
        )
        # Every candidate considered must actually be in the requested data center.
        from app.repositories import cluster_repository

        for c in dc_result["candidates"]:
            cluster = cluster_repository.get_by_code(c["cluster_code"])
            assert cluster.DataCenter == "Atlanta-DC1"


@pytest.mark.asyncio
async def test_list_data_centers_returns_known_locations(mcp_server):
    async with Client(mcp_server) as client:
        result = json.loads((await client.call_tool("list_data_centers", {})).content[0].text)
        assert "Atlanta-DC1" in result["data_centers"]
        assert "New York-DC1" in result["data_centers"]


@pytest.mark.asyncio
async def test_server_exposes_7_resources(mcp_server):
    async with Client(mcp_server) as client:
        static_resources = await client.list_resources()
        templates = await client.list_resource_templates()
        assert len(static_resources.resources) + len(templates.resource_templates) == 7


@pytest.mark.asyncio
async def test_no_execute_sql_tool(mcp_server):
    async with Client(mcp_server) as client:
        listed = await client.list_tools()
        names = {t.name for t in listed.tools}
        assert not any("sql" in n.lower() for n in names)


@pytest.mark.asyncio
async def test_get_application_returns_real_data(mcp_server):
    async with Client(mcp_server) as client:
        result = await client.call_tool("get_application", {"application_code": "APP-PAYMENTS"})
        payload = json.loads(result.content[0].text)
        assert payload["ApplicationCode"] == "APP-PAYMENTS"
        assert payload["BusinessCriticality"] == "Critical"


@pytest.mark.asyncio
async def test_score_hosting_candidates_returns_ranked_list(mcp_server):
    async with Client(mcp_server) as client:
        result = await client.call_tool("score_hosting_candidates", {"application_code": "APP-CRM"})
        payload = json.loads(result.content[0].text)
        assert len(payload["candidates"]) > 0
        ranks = [c["rank"] for c in payload["candidates"]]
        assert ranks == sorted(ranks)


@pytest.mark.asyncio
async def test_submit_decision_requires_access_token(mcp_server):
    # access_token has no default, so the MCP schema itself rejects a call
    # missing it entirely with a Pydantic validation error (is_error=True) -
    # that's the tool refusing to even accept an anonymous invocation, at the
    # protocol layer, before any tool code runs.
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "submit_recommendation_decision",
            {"recommendation_id": 1, "decision": "Approve", "reviewer_employee_id": 1, "reason": ""},
        )
        assert result.is_error
        assert "access_token" in result.content[0].text


@pytest.mark.asyncio
async def test_submit_decision_rejects_empty_access_token(mcp_server):
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "submit_recommendation_decision",
            {"recommendation_id": 1, "decision": "Approve", "reviewer_employee_id": 1, "access_token": "", "reason": ""},
        )
        payload = json.loads(result.content[0].text)
        assert "error" in payload
        assert "anonymous" in payload["error"].lower()


@pytest.mark.asyncio
async def test_submit_decision_rejects_mismatched_reviewer_id(mcp_server, access_token, auth_employee_id):
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "submit_recommendation_decision",
            {
                "recommendation_id": 1, "decision": "Approve",
                "reviewer_employee_id": auth_employee_id + 1, "access_token": access_token, "reason": "",
            },
        )
        payload = json.loads(result.content[0].text)
        assert "error" in payload
        assert "employee" in payload["error"].lower()


@pytest.mark.asyncio
async def test_submit_decision_succeeds_with_valid_token(mcp_server, access_token, auth_employee_id):
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "submit_recommendation_decision",
            {
                "recommendation_id": 1, "decision": "RequestMoreAnalysis",
                "reviewer_employee_id": auth_employee_id, "access_token": access_token, "reason": "test",
            },
        )
        payload = json.loads(result.content[0].text)
        assert "error" not in payload
        assert payload["decision_id"] > 0


@pytest.mark.asyncio
async def test_every_tool_call_is_audited(mcp_server):
    from app.repositories.base import T, fetch_one

    async with Client(mcp_server) as client:
        await client.call_tool("get_cluster", {"cluster_code": "atl-03"})

    row = fetch_one(
        f"SELECT TOP 1 ToolName, Success FROM {T('AgentAuditLog')} WHERE ToolName = 'get_cluster' ORDER BY AuditId DESC",
        {},
    )
    assert row is not None
    assert row["Success"] is True


@pytest.mark.asyncio
async def test_resource_read_returns_json(mcp_server):
    async with Client(mcp_server) as client:
        result = await client.read_resource("cmdb://clusters/3")
        payload = json.loads(result.contents[0].text)
        assert payload["ClusterCode"] == "atl-03"
