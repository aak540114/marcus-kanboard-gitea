"""
Unit tests for the lease-recreation guard in report_task_progress.

When a stale agent reports intermediate progress after another agent has
already completed the task, the no-lease branch must NOT recreate a lease
on the finished task.  Doing so creates an orphaned lease that expires,
triggering another recovery cycle and eventually a zombie task.

Regression coverage: the guard used to compare ``task_obj.status`` (a
``TaskStatus`` enum member) against the plain strings ``{"done",
"completed"}``. An Enum member is never ``==`` to a bare string with the
same text, so the comparison was always False and the guard never fired —
every stale progress report on an already-DONE task silently recreated a
lease. These tests call the real ``report_task_progress`` function (not a
reimplementation of the guard logic) so a regression here is caught by
actually exercising the code, not by re-deriving the expected answer.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from src.core.models import Priority, TaskAssignment, TaskStatus
from src.marcus_mcp.tools.task import report_task_progress

pytestmark = pytest.mark.unit


def _make_task(task_id: str, status: TaskStatus) -> MagicMock:
    """Return a mock Task with .id and .status set."""
    t = MagicMock()
    t.id = task_id
    t.status = status
    return t


def _make_assignment(task_id: str, agent_id: str = "agent-001") -> TaskAssignment:
    """Build a minimal TaskAssignment for state.agent_tasks."""
    return TaskAssignment(
        task_id=task_id,
        task_name="Test Task",
        description="desc",
        instructions="do it",
        estimated_hours=0.1,
        priority=Priority.HIGH,
        dependencies=[],
        assigned_to=agent_id,
        assigned_at=datetime.now(timezone.utc),
        due_date=None,
    )


def _make_state_no_active_lease(
    task_id: str,
    task_status: TaskStatus,
    agent_id: str = "agent-001",
) -> Mock:
    """Build a state where renew_lease reports "no active lease"."""
    state = Mock()
    state.initialize_kanban = AsyncMock()
    state.kanban_client = Mock()
    state.kanban_client.get_all_tasks = AsyncMock(return_value=[])
    state.kanban_client.update_task = AsyncMock()
    state.kanban_client.update_task_progress = AsyncMock()
    state.kanban_client._load_workspace_state = Mock(return_value=None)
    state.agent_tasks = {agent_id: _make_assignment(task_id, agent_id)}
    state.project_tasks = [_make_task(task_id, task_status)]

    lease_manager = Mock()
    lease_manager.active_leases = {}
    lease_manager.renew_lease = AsyncMock(return_value=None)
    lease_manager.create_lease = AsyncMock()
    state.lease_manager = lease_manager

    state.agent_status = {}
    state.assignment_persistence = Mock()
    state.assignment_persistence.remove_assignment = AsyncMock()
    state.memory = None
    state.provider = "sqlite"
    state.code_analyzer = None
    state.subtask_manager = None
    return state


class TestLeaseGuardIntegration:
    """Verify the guard is wired correctly inside report_task_progress."""

    @pytest.mark.asyncio
    async def test_no_lease_recreated_when_task_already_done(self) -> None:
        """Stale progress report on a DONE task must not recreate a lease."""
        state = _make_state_no_active_lease("task-789", TaskStatus.DONE)

        await report_task_progress(
            agent_id="agent-001",
            task_id="task-789",
            status="in_progress",
            progress=50,
            message="stale update after another agent finished",
            state=state,
        )

        state.lease_manager.create_lease.assert_not_called()

    @pytest.mark.asyncio
    async def test_lease_recreated_when_task_still_in_progress(self) -> None:
        """Sanity check: the guard must not block legitimate recreation."""
        state = _make_state_no_active_lease("task-789", TaskStatus.IN_PROGRESS)
        fake_lease = MagicMock()
        fake_lease.lease_expires.isoformat.return_value = "2099-01-01T00:00:00"
        state.lease_manager.create_lease.return_value = fake_lease

        await report_task_progress(
            agent_id="agent-001",
            task_id="task-789",
            status="in_progress",
            progress=50,
            message="still working",
            state=state,
        )

        state.lease_manager.create_lease.assert_called_once()

    def test_done_task_status_constant(self) -> None:
        """TaskStatus.DONE value must be 'done' — guard relies on string match."""
        assert TaskStatus.DONE.value == "done"
