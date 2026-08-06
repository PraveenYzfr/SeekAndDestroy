"""Programmatic MCP client for SeekAndDestroy, plus a LangChain tool adapter.

Two connection modes:
- in-process (pass the MCPServer instance directly) - used by tests and by
  ``interactive_client.py`` when it imports the server module directly.
- stdio subprocess - used when the server runs as a separate process
  (``python mcp-server/server.py``).
"""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp import ClientSession

_THIS_DIR = Path(__file__).resolve().parent
_MCP_SERVER_DIR = _THIS_DIR.parent / "mcp-server"


def in_process_client() -> Client:
    """Connect to the server in-process (fastest; used for demos/tests)."""
    sys.path.insert(0, str(_MCP_SERVER_DIR))
    sys.path.insert(0, str(_THIS_DIR.parent / "ai-service"))
    import server as srv  # type: ignore

    return Client(srv.server)


@asynccontextmanager
async def stdio_session():
    """Connect to the server as a separate subprocess over stdio."""
    params = StdioServerParameters(
        command=sys.executable, args=[str(_MCP_SERVER_DIR / "server.py")], cwd=str(_MCP_SERVER_DIR),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call_tool(client: Client, name: str, arguments: dict) -> Any:
    result = await client.call_tool(name, arguments)
    text = result.content[0].text if result.content else "{}"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def read_resource(client: Client, uri: str) -> str:
    result = await client.read_resource(uri)
    return result.contents[0].text if result.contents else ""


# =============================================================================
# LangChain adapter
# =============================================================================

_JSON_TYPE_MAP = {"string": str, "number": float, "integer": int, "boolean": bool, "object": dict, "array": list}


def _pydantic_model_from_json_schema(tool_name: str, schema: dict):
    from pydantic import create_model

    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields = {}
    for prop_name, prop_schema in properties.items():
        py_type = _JSON_TYPE_MAP.get(prop_schema.get("type"), Any)
        default = ... if prop_name in required else prop_schema.get("default", None)
        fields[prop_name] = (py_type, default)
    model_name = "".join(part.capitalize() for part in tool_name.split("_")) + "Args"
    return create_model(model_name, **fields)


async def load_mcp_tools_as_langchain(client: Client) -> list:
    """Build LangChain ``StructuredTool`` objects for every tool the MCP
    server exposes. Each tool's coroutine calls back into the live MCP
    session, so LangChain agents built on top of this list go through the
    exact same audited, rule-governed surface as the interactive client.
    """
    from langchain_core.tools import StructuredTool

    listed = await client.list_tools()
    tools = []
    for t in listed.tools:
        args_model = _pydantic_model_from_json_schema(t.name, t.inputSchema or {})

        def make_coroutine(tool_name: str):
            async def _run(**kwargs):
                return await call_tool(client, tool_name, kwargs)

            return _run

        tools.append(
            StructuredTool(
                name=t.name, description=t.description or t.name, args_schema=args_model,
                coroutine=make_coroutine(t.name), func=None,
            )
        )
    return tools
