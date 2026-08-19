"""
Unit tests for get_task_count's active-project restoration.

Regression coverage: get_task_count temporarily switches the server's
active project to `project_id` to query its task count, then switches
back to the original project. The switch-back only ran on the
non-exception path — if kanban_client.get_available_tasks() raised
(e.g. a transient network error), the function returned 0 (a plausible,
success-shaped value) from its except-block, but the server's active
project was left pointed at `project_id` forever, silently corrupting
every subsequent unrelated tool call until someone noticed and manually
switched back.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from src.marcus_mcp.tools.project_management import get_task_count

pytestmark = pytest.mark.unit


def _make_server(original_project_id: str) -> Mock:
    server = Mock()
    original_project = Mock()
    original_project.id = original_project_id
    server.project_registry = Mock()
    server.project_registry.get_active_project = AsyncMock(
        return_value=original_project
    )
    server.project_manager = Mock()
    server.project_manager.switch_project = AsyncMock()
    return server


class TestGetTaskCountRestoresActiveProject:
    @pytest.mark.asyncio
    async def test_restores_original_project_when_get_tasks_raises(self) -> None:
        server = _make_server("original-project")
        kanban_client = Mock()
        kanban_client.get_available_tasks = AsyncMock(
            side_effect=ConnectionError("kanban unreachable")
        )
        server.project_manager.get_kanban_client = AsyncMock(
            return_value=kanban_client
        )

        result = await get_task_count(server, "target-project")

        assert result == 0
        # Switch calls: 1) to target-project, 2) back to original-project.
        assert server.project_manager.switch_project.call_count == 2
        server.project_manager.switch_project.assert_any_call("target-project")
        server.project_manager.switch_project.assert_any_call("original-project")

    @pytest.mark.asyncio
    async def test_restores_original_project_on_success(self) -> None:
        server = _make_server("original-project")
        kanban_client = Mock()
        kanban_client.get_available_tasks = AsyncMock(return_value=[1, 2, 3])
        server.project_manager.get_kanban_client = AsyncMock(
            return_value=kanban_client
        )

        result = await get_task_count(server, "target-project")

        assert result == 3
        server.project_manager.switch_project.assert_any_call("original-project")
