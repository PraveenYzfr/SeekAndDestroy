from __future__ import annotations

import sys
from pathlib import Path

import pytest

_MCP_SERVER_DIR = Path(__file__).resolve().parents[1]
_AI_SERVICE_DIR = _MCP_SERVER_DIR.parent / "ai-service"
for p in (str(_MCP_SERVER_DIR), str(_AI_SERVICE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True, scope="session")
def _quiet_logging():
    import logging

    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))


@pytest.fixture
def mcp_server():
    import server as srv

    return srv.server
