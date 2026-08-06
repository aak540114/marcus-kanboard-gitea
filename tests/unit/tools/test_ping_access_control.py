"""
Unit tests for ping's access control and confirmation gates on
destructive commands.

Bug found during a full codebase deep-analysis pass: ping is listed
in DEFAULT_TOOLS (available to unregistered clients) and in the
"observer" role's tool list — which authenticate()'s own docstring
defines as "Read-only access for monitoring/analytics" — yet
ping(echo="reset") wipes state.tasks_being_assigned, state.agent_tasks,
_active_operations, and assignment_monitor's reversion-tracking dicts
with no role check and no confirmation step (unlike remove_project,
which requires an explicit confirm=True).

The access-control gate in handlers.handle_tool_call only checks the
tool NAME against the role's allow-list — there is no per-argument
gating, so nothing there can distinguish ping(echo="pong") from
ping(echo="reset"). The fix lives inside ping() itself.
"""

from unittest.mock import Mock

import pytest

from src.marcus_mcp.tools.system import ping

pytestmark = pytest.mark.unit


def _make_state(client_type: str | None, client_id: str = "client-1") -> Mock:
    """Build a mock state with (or without) an authenticated client.

    ``client_type=None`` simulates an unregistered client — no
    ``_current_client_id``/``_registered_clients`` at all, matching
    DEFAULT_TOOLS access before ``authenticate()`` is ever called.
    """
    state = Mock()
    state.provider = "test_provider"
    state.instance_id = "test_instance"
    state.tasks_being_assigned = {"task-1", "task-2"}
    state.agent_status = {}
    state.agent_tasks = {"agent-1": Mock()}
    state._shutdown_event = None
    state._active_operations = {"task_assignment_task-1"}
    state.assignment_monitor = Mock()
    state.log_event = Mock()
    # ping's base response reads state.realtime_log.name as a path when
    # present — Mock() auto-vivifies both attrs, so without this the
    # response builder crashes on Path(Mock()). Not relevant to this
    # test's scope; keep it absent like a fresh state.
    del state.realtime_log

    if client_type is not None:
        state._current_client_id = client_id
        state._registered_clients = {client_id: {"client_type": client_type}}
    else:
        # No registration at all — matches an unauthenticated client.
        del state._current_client_id
        del state._registered_clients

    return state


class TestPingCleanupAccessControl:
    """ping(echo='cleanup') must reject observer/unregistered clients."""

    @pytest.mark.asyncio
    async def test_unregistered_client_rejected(self) -> None:
        state = _make_state(client_type=None)

        result = await ping(echo="cleanup", state=state)

        assert result["cleanup"]["success"] is False
        assert "requires an authenticated" in result["cleanup"]["error"]
        # State must be untouched.
        assert state.tasks_being_assigned == {"task-1", "task-2"}
        assert state._active_operations == {"task_assignment_task-1"}

    @pytest.mark.asyncio
    async def test_observer_client_rejected(self) -> None:
        state = _make_state(client_type="observer")

        result = await ping(echo="cleanup", state=state)

        assert result["cleanup"]["success"] is False
        assert state.tasks_being_assigned == {"task-1", "task-2"}

    @pytest.mark.asyncio
    async def test_developer_client_allowed(self) -> None:
        state = _make_state(client_type="developer")

        result = await ping(echo="cleanup", state=state)

        assert result["cleanup"]["success"] is True
        assert state.tasks_being_assigned == set()

    @pytest.mark.asyncio
    async def test_agent_client_allowed(self) -> None:
        state = _make_state(client_type="agent")

        result = await ping(echo="cleanup", state=state)

        assert result["cleanup"]["success"] is True

    @pytest.mark.asyncio
    async def test_admin_client_allowed(self) -> None:
        state = _make_state(client_type="admin")

        result = await ping(echo="cleanup", state=state)

        assert result["cleanup"]["success"] is True


class TestPingResetAccessControlAndConfirm:
    """ping(echo='reset') must reject observer/unregistered clients AND
    require confirm=True even for an allowed client type."""

    @pytest.mark.asyncio
    async def test_unregistered_client_rejected_even_with_confirm(self) -> None:
        """Access control is checked before confirm — a client with no
        mutation rights cannot bypass it just by passing confirm=True."""
        state = _make_state(client_type=None)

        result = await ping(echo="reset", state=state, confirm=True)

        assert result["reset"]["success"] is False
        assert "requires an authenticated" in result["reset"]["error"]
        assert state.tasks_being_assigned == {"task-1", "task-2"}
        assert state.agent_tasks != {}

    @pytest.mark.asyncio
    async def test_observer_client_rejected_even_with_confirm(self) -> None:
        state = _make_state(client_type="observer")

        result = await ping(echo="reset", state=state, confirm=True)

        assert result["reset"]["success"] is False
        assert state.tasks_being_assigned == {"task-1", "task-2"}

    @pytest.mark.asyncio
    async def test_allowed_client_without_confirm_is_a_dry_run(self) -> None:
        """An allowed client that omits confirm gets a dry-run, no mutation."""
        state = _make_state(client_type="developer")

        result = await ping(echo="reset", state=state)

        assert result["reset"]["success"] is False
        assert "confirm=true" in result["reset"]["error"]
        assert result["reset"]["would_clear"]["tasks_cleared"] == 2
        # Nothing was actually cleared.
        assert state.tasks_being_assigned == {"task-1", "task-2"}
        assert state.agent_tasks == {"agent-1": state.agent_tasks["agent-1"]}

    @pytest.mark.asyncio
    async def test_allowed_client_with_confirm_performs_reset(self) -> None:
        state = _make_state(client_type="developer")

        result = await ping(echo="reset", state=state, confirm=True)

        assert result["reset"]["success"] is True
        assert state.tasks_being_assigned == set()
        assert state.agent_tasks == {}
        assert state._active_operations == set()

    @pytest.mark.asyncio
    async def test_agent_client_with_confirm_performs_reset(self) -> None:
        state = _make_state(client_type="agent")

        result = await ping(echo="reset", state=state, confirm=True)

        assert result["reset"]["success"] is True

    @pytest.mark.asyncio
    async def test_admin_client_with_confirm_performs_reset(self) -> None:
        state = _make_state(client_type="admin")

        result = await ping(echo="reset", state=state, confirm=True)

        assert result["reset"]["success"] is True


class TestPingReadOnlyCommandsUnaffected:
    """Non-mutating commands must remain available to every client."""

    @pytest.mark.asyncio
    async def test_unregistered_client_can_still_ping(self) -> None:
        state = _make_state(client_type=None)

        result = await ping(echo="pong", state=state)

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_observer_client_can_still_check_health(self) -> None:
        state = _make_state(client_type="observer")
        state.lease_manager = None
        state.assignment_persistence = Mock()
        state.kanban_client = Mock()

        result = await ping(echo="health", state=state)

        assert result["success"] is True
        assert "health" in result
