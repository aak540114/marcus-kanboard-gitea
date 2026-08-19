"""
Unit tests for add_project's provider validation.

Regression coverage: add_project's own validation accepted
["planka", "linear", "github"], but its own tool schema
(src/marcus_mcp/handlers.py) declares "provider" as an enum of
["kanboard", "sqlite"], and ProjectContextManager routes every project
it activates through KanbanFactory.create(provider, ...)
(src/integrations/kanban_factory.py), which only recognizes "kanboard"
and "sqlite" — raising ValueError for anything else. A caller following
add_project's own documented schema (provider="kanboard") was rejected
by the validation; a caller passing provider="planka" (accepted by the
old validation) would create a project that later fails the moment
anyone switches to it.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from src.marcus_mcp.tools.project_management import add_project

pytestmark = pytest.mark.unit


def _make_server() -> Mock:
    server = Mock()
    server.project_registry = Mock()
    server.project_registry.add_project = AsyncMock(return_value="project-1")
    server.project_manager = Mock()
    server.project_manager.switch_project = AsyncMock()
    server.project_manager.get_kanban_client = AsyncMock(return_value=Mock())
    return server


class TestAddProjectProviderValidation:
    @pytest.mark.asyncio
    async def test_accepts_kanboard_provider(self) -> None:
        """provider="kanboard" (the schema's own documented value) must succeed."""
        server = _make_server()

        result = await add_project(
            server, {"name": "My Project", "provider": "kanboard"}
        )

        assert result["success"] is not False
        server.project_registry.add_project.assert_called_once()

    @pytest.mark.asyncio
    async def test_accepts_sqlite_provider(self) -> None:
        server = _make_server()

        result = await add_project(
            server, {"name": "My Project", "provider": "sqlite"}
        )

        assert result["success"] is not False
        server.project_registry.add_project.assert_called_once()

    @pytest.mark.asyncio
    async def test_rejects_planka_provider_not_supported_by_kanban_factory(
        self,
    ) -> None:
        """
        provider="planka" is not implemented by KanbanFactory, so it
        must be rejected here rather than accepted and left to fail
        later when someone switches to the project.
        """
        server = _make_server()

        result = await add_project(
            server, {"name": "My Project", "provider": "planka"}
        )

        assert result["success"] is False
        server.project_registry.add_project.assert_not_called()
