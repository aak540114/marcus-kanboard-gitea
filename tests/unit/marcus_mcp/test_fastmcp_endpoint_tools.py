"""
Unit tests for the FastMCP endpoint tool registration.

Regression: the Kanboard human-gated tools (get_work_context, …) were in the
endpoint allowlist (tool_groups.py) but never given a FastMCP @app.tool()
wrapper in _register_endpoint_tools, so they were invisible over the HTTP
transport a coding agent connects to.
"""

import pytest
from mcp.server.fastmcp import FastMCP

from src.marcus_mcp.server import MarcusServer

HUMAN_GATED = [
    "get_work_context",
    "get_project_description",
    "update_project_description",
    "generate_acceptance_criteria",
    "post_ticket_progress",
    "signal_ready_for_review",
    "signal_waiting_for_human",
    "signal_blocked",
    "get_ticket_lifecycle_state",
    "get_pending_tickets",
    "start_ticket_dev_environment",
    "get_ticket_dev_environment_url",
]


@pytest.mark.asyncio
async def test_agent_endpoint_registers_human_gated_tools():
    """The 'agent' FastMCP endpoint exposes every human-gated tool."""
    srv = MarcusServer.__new__(MarcusServer)  # skip heavy __init__
    app = FastMCP("test-agent")
    srv._register_endpoint_tools(app, "agent")

    names = {t.name for t in await app.list_tools()}
    for tool in HUMAN_GATED:
        assert tool in names, f"{tool} not registered on the agent endpoint"
    # And the classic surface is still present.
    assert "request_next_task" in names
    assert "get_task_context" in names
