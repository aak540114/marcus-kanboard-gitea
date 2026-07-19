"""
Unit tests for src/marcus_mcp/tools/human_gated.py's MCP tool wrappers.

Focused on start_ticket_dev_environment, where an existing bug was found:
the tool used the CALLER-supplied `provider` argument to look up the
just-started environment, but HumanGatedWorkflow.start_dev_environment()
always registers it under the workflow's own configured provider
(self._provider) — a caller-supplied provider that doesn't match silently
returned port: None for an environment that actually started fine.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.marcus_mcp.tools.human_gated import (
    post_ticket_progress,
    register_workflow,
    signal_ready_for_review,
    signal_waiting_for_human,
    start_ticket_dev_environment,
    update_project_description,
)


@pytest.fixture(autouse=True)
def _reset_workflow_singleton():
    """Ensure each test starts with no workflow registered."""
    register_workflow(None)
    yield
    register_workflow(None)


@pytest.fixture
def mock_workflow():
    wf = MagicMock()
    wf._provider = "kanboard"
    wf.start_dev_environment = AsyncMock(return_value="http://localhost:9100")
    wf._dev_env = MagicMock()
    wf._dev_env.get_info = MagicMock(
        return_value=MagicMock(port=9100)
    )
    return wf


class TestStartTicketDevEnvironment:
    @pytest.mark.asyncio
    async def test_missing_ticket_id_returns_error(self):
        result = await start_ticket_dev_environment({"provider": "kanboard"})
        assert result["success"] is False
        assert "required" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_provider_returns_error(self):
        result = await start_ticket_dev_environment({"ticket_id": "42"})
        assert result["success"] is False
        assert "required" in result["error"]

    @pytest.mark.asyncio
    async def test_no_workflow_registered_returns_error(self):
        result = await start_ticket_dev_environment(
            {"ticket_id": "42", "provider": "kanboard"}
        )
        assert result["success"] is False
        assert "not initialised" in result["error"]

    @pytest.mark.asyncio
    async def test_dev_env_start_failure_returns_error(self, mock_workflow):
        mock_workflow.start_dev_environment = AsyncMock(return_value=None)
        register_workflow(mock_workflow)

        result = await start_ticket_dev_environment(
            {"ticket_id": "42", "provider": "kanboard"}
        )

        assert result["success"] is False
        assert "Failed to start" in result["error"]

    @pytest.mark.asyncio
    async def test_exception_is_caught_and_returned_as_failure(self, mock_workflow):
        mock_workflow.start_dev_environment = AsyncMock(
            side_effect=RuntimeError("docker unreachable")
        )
        register_workflow(mock_workflow)

        result = await start_ticket_dev_environment(
            {"ticket_id": "42", "provider": "kanboard"}
        )

        assert result["success"] is False
        assert "docker unreachable" in result["error"]

    @pytest.mark.asyncio
    async def test_success_uses_workflows_own_provider_for_lookup(self, mock_workflow):
        """The core regression case: the workflow's real provider is
        'kanboard', but the caller (mistakenly, or for another provider
        entirely) supplies a different `provider`. get_info must be
        looked up under the workflow's own provider, not the caller's,
        or the port would be silently reported as None."""
        register_workflow(mock_workflow)

        result = await start_ticket_dev_environment(
            {"ticket_id": "42", "provider": "jira"}
        )

        assert result["success"] is True
        mock_workflow._dev_env.get_info.assert_called_once_with("42", "kanboard")
        assert result["result"]["port"] == 9100
        assert result["result"]["provider"] == "kanboard"
        assert result["result"]["url"] == "http://localhost:9100"

    @pytest.mark.asyncio
    async def test_get_info_miss_still_reports_success_with_none_port(
        self, mock_workflow
    ):
        """A started environment Marcus can't find in its own bookkeeping
        still reports success (the URL is authoritative) with port: None,
        rather than failing outright."""
        mock_workflow._dev_env.get_info = MagicMock(return_value=None)
        register_workflow(mock_workflow)

        result = await start_ticket_dev_environment(
            {"ticket_id": "42", "provider": "kanboard"}
        )

        assert result["success"] is True
        assert result["result"]["port"] is None


class TestSignalToolsPropagateFailure:
    """Signal tools must report the workflow's real success/failure."""

    @pytest.mark.asyncio
    async def test_signal_ready_reports_false_when_workflow_returns_false(self):
        """A rejected/duplicate signal_ready_for_review → success: False."""
        wf = MagicMock()
        wf._provider = "kanboard"
        wf.signal_ready_for_review = AsyncMock(return_value=False)
        wf._lifecycle = MagicMock()
        wf._lifecycle.get = MagicMock(return_value=None)
        register_workflow(wf)

        result = await signal_ready_for_review(
            {"ticket_id": "42", "provider": "kanboard"}
        )

        assert result["success"] is False
        assert result["result"]["comment_posted"] is False

    @pytest.mark.asyncio
    async def test_signal_waiting_reports_false_when_not_in_progress(self):
        """set_waiting_for_human returning False → success: False, real state."""
        wf = MagicMock()
        wf._provider = "kanboard"
        wf.set_waiting_for_human = AsyncMock(return_value=False)
        rec = MagicMock()
        rec.state = MagicMock(value="in_progress")
        wf._lifecycle = MagicMock()
        wf._lifecycle.get = MagicMock(return_value=rec)
        register_workflow(wf)

        result = await signal_waiting_for_human(
            {"ticket_id": "42", "provider": "kanboard"}
        )

        assert result["success"] is False
        # Reports the REAL state, not a hardcoded "waiting_for_human".
        assert result["result"]["new_state"] == "in_progress"


class TestPostProgressPercentage:
    """post_ticket_progress must coerce/clamp percentage safely."""

    @pytest.mark.asyncio
    async def test_non_numeric_percentage_returns_clean_error(self):
        wf = MagicMock()
        wf._provider = "kanboard"
        wf.report_progress = AsyncMock(return_value=True)
        register_workflow(wf)

        result = await post_ticket_progress(
            {"ticket_id": "42", "provider": "kanboard", "percentage": "about half"}
        )

        assert result["success"] is False
        assert "percentage" in result["error"]
        wf.report_progress.assert_not_called()

    @pytest.mark.asyncio
    async def test_out_of_range_percentage_is_clamped(self):
        wf = MagicMock()
        wf._provider = "kanboard"
        wf.report_progress = AsyncMock(return_value=True)
        register_workflow(wf)

        result = await post_ticket_progress(
            {"ticket_id": "42", "provider": "kanboard", "percentage": 250}
        )

        assert result["success"] is True
        assert result["result"]["percentage"] == 100
        wf.report_progress.assert_awaited_once_with("42", 100, "Work in progress.")


class TestUpdateProjectDescription:
    """update_project_description propagates the workflow's updated flag."""

    @pytest.mark.asyncio
    async def test_missing_description_returns_error(self):
        wf = MagicMock()
        register_workflow(wf)
        result = await update_project_description(
            {"ticket_id": "42", "provider": "kanboard", "description": "  "}
        )
        assert result["success"] is False
        assert "description" in result["error"]

    @pytest.mark.asyncio
    async def test_successful_update_reports_true(self):
        wf = MagicMock()
        wf.apply_agent_project_description = AsyncMock(
            return_value={"updated": True, "project_id": 3}
        )
        register_workflow(wf)
        result = await update_project_description(
            {"ticket_id": "42", "provider": "kanboard", "description": "# Docs\n"}
        )
        assert result["success"] is True
        assert result["result"]["project_id"] == 3

    @pytest.mark.asyncio
    async def test_human_lock_reports_false_with_reason(self):
        wf = MagicMock()
        wf.apply_agent_project_description = AsyncMock(
            return_value={
                "updated": False,
                "project_id": 3,
                "reason": "a human has edited this description; not overwriting",
            }
        )
        register_workflow(wf)
        result = await update_project_description(
            {"ticket_id": "42", "provider": "kanboard", "description": "# Docs\n"}
        )
        assert result["success"] is False
        assert "human" in result["result"]["reason"]
