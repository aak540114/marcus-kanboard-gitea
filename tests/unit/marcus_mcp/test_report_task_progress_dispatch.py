"""
Unit tests for the stdio/legacy MCP transport's report_task_progress dispatch.

Regression: handlers.py's handle_tool_call for report_task_progress only
forwarded agent_id/task_id/status/progress/message/start_command/
readiness_probe — the verifications (#523 Slice B) and evidence (#677)
parameters were silently dropped and not even declared in the tool's
inputSchema, even though the FastMCP/HTTP transport (server.py) already
supported both. An agent connecting over stdio (agent_server.py, this
deployment's primary agent path) had no way to submit either extra
correctness check.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.marcus_mcp.handlers import get_tool_definitions, handle_tool_call


class _BareState:
    """A state stand-in with none of MarcusServer's attributes.

    A MagicMock() would satisfy every hasattr() check handle_tool_call
    makes along the way (client_id lookup, audit logging, ...), sending
    it down code paths this test isn't exercising and that choke on
    trying to JSON-serialize a MagicMock. This object deliberately has
    nothing, so those optional branches are skipped exactly like they
    are for a state object that doesn't define them.
    """


@pytest.mark.asyncio
@pytest.mark.unit
async def test_verifications_and_evidence_are_forwarded():
    """verifications/evidence arguments reach the implementation call."""
    fake_result = {"success": True}
    with patch(
        "src.marcus_mcp.handlers.report_task_progress",
        new_callable=AsyncMock,
        return_value=fake_result,
    ) as mock_impl, patch(
        "src.marcus_mcp.handlers.get_client_tools", return_value=["*"]
    ):
        await handle_tool_call(
            "report_task_progress",
            {
                "agent_id": "agent-1",
                "task_id": "task-1",
                "status": "completed",
                "verifications": [{"command": "npm test", "outcome": "unit"}],
                "evidence": {"exit_code": 0, "stdout": "ok"},
            },
            _BareState(),
        )

    assert mock_impl.await_args.kwargs["verifications"] == [
        {"command": "npm test", "outcome": "unit"}
    ]
    assert mock_impl.await_args.kwargs["evidence"] == {
        "exit_code": 0,
        "stdout": "ok",
    }


@pytest.mark.asyncio
@pytest.mark.unit
async def test_verifications_and_evidence_default_to_none():
    """Omitting both must forward None, not raise or drop the call."""
    fake_result = {"success": True}
    with patch(
        "src.marcus_mcp.handlers.report_task_progress",
        new_callable=AsyncMock,
        return_value=fake_result,
    ) as mock_impl, patch(
        "src.marcus_mcp.handlers.get_client_tools", return_value=["*"]
    ):
        await handle_tool_call(
            "report_task_progress",
            {"agent_id": "agent-1", "task_id": "task-1", "status": "in_progress"},
            _BareState(),
        )

    assert mock_impl.await_args.kwargs["verifications"] is None
    assert mock_impl.await_args.kwargs["evidence"] is None


@pytest.mark.unit
def test_tool_schema_declares_verifications_and_evidence():
    """The stdio tool schema must advertise both fields — a client
    introspecting via list_tools() previously had no way to discover
    they existed at all."""
    tools = {t.name: t for t in get_tool_definitions()}
    props = tools["report_task_progress"].inputSchema["properties"]
    assert "verifications" in props
    assert "evidence" in props
