"""
Unit tests for the FastMCP endpoint tool registration.

Regression: the Kanboard human-gated tools (get_work_context, …) were in the
endpoint allowlist (tool_groups.py) but never given a FastMCP @app.tool()
wrapper in _register_endpoint_tools, so they were invisible over the HTTP
transport a coding agent connects to.
"""

from unittest.mock import AsyncMock, patch

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


@pytest.mark.asyncio
async def test_add_feature_forwards_integration_point():
    """Regression: the FastMCP add_feature wrapper used to accept a
    `context` parameter it silently discarded, and hardcoded
    integration_point="current" — not one of the implementation's valid
    values (auto_detect, after_current, parallel, new_phase) — regardless
    of what the caller asked for. It must forward the caller's own
    integration_point (defaulting to "auto_detect", matching the stdio
    transport's default in handlers.py)."""
    srv = MarcusServer.__new__(MarcusServer)
    app = FastMCP("test-human")
    srv._register_endpoint_tools(app, "human")

    with patch(
        "src.marcus_mcp.tools.nlp.add_feature", new_callable=AsyncMock
    ) as mock_impl:
        mock_impl.return_value = {"success": True}

        await app.call_tool(
            "add_feature",
            {"description": "Add dark mode", "integration_point": "parallel"},
        )
        assert mock_impl.await_args.kwargs["integration_point"] == "parallel"
        assert mock_impl.await_args.kwargs["feature_description"] == (
            "Add dark mode"
        )

        await app.call_tool("add_feature", {"description": "Add export"})
        assert mock_impl.await_args.kwargs["integration_point"] == "auto_detect"


@pytest.mark.asyncio
async def test_get_usage_report_forwards_days():
    """Regression: the FastMCP get_usage_report wrapper declared
    start_date/end_date/group_by parameters but never read any of them —
    it always called the implementation with a hardcoded days=7, silently
    ignoring whatever range the caller actually asked for. The wrapper's
    signature must only declare what the implementation (audit_tools.
    get_usage_report, which takes a plain `days: int`) actually supports,
    and must forward the caller's value."""
    srv = MarcusServer.__new__(MarcusServer)
    app = FastMCP("test-human")
    srv._register_endpoint_tools(app, "human")

    with patch(
        "src.marcus_mcp.tools.audit_tools.get_usage_report",
        new_callable=AsyncMock,
    ) as mock_impl:
        mock_impl.return_value = {"success": True}

        await app.call_tool("get_usage_report", {"days": 30})
        assert mock_impl.await_args.kwargs["days"] == 30

        await app.call_tool("get_usage_report", {})
        assert mock_impl.await_args.kwargs["days"] == 7
