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


@pytest.fixture(scope="session")
def auth_employee_id() -> int:
    """The real, active Employee row every auth-related test authenticates
    as (E1001 in the deterministic seed data)."""
    return 1


@pytest.fixture(scope="session")
def access_token(auth_employee_id) -> str:
    """A valid local-mode dev token for write-tool tests (SAD_AUTH__MODE=local
    is the test-suite default) - see app.security.jwt_service."""
    from app.security.jwt_service import create_local_token

    return create_local_token(
        employee_id=auth_employee_id, employee_number="E1001", display_name="Aditi Sharma",
        email="aditi.sharma@seekanddestroy.example",
    )
