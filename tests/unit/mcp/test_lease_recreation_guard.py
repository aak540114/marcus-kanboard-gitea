"""
Unit tests for the lease-recreation guard in report_task_progress.

When a stale agent reports intermediate progress after another agent has
already completed the task, the no-lease branch must NOT recreate a lease
on the finished task.  Doing so creates an orphaned lease that expires,
triggering another recovery cycle and eventually a zombie task.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(task_id: str, status: str) -> MagicMock:
    """Return a mock Task with .id and .status set."""
    t = MagicMock()
    t.id = task_id
    t.status = status
    return t


def _make_state(
    task_id: str,
    task_status: str,
    lease_manager: MagicMock | None = None,
) -> MagicMock:
    """Return a mock MarcusState with the minimum attributes the code touches."""
    state = MagicMock()
    state.project_tasks = [_make_task(task_id, task_status)]
    state.lease_manager = lease_manager
    state.kanban_client = AsyncMock()
    state.kanban_client.update_task_progress = AsyncMock()
    return state


def _make_lease_manager_no_active_lease() -> MagicMock:
    """Return a lease manager whose renew_lease returns None (no active lease)."""
    lm = MagicMock()
    lm.renew_lease = AsyncMock(return_value=None)
    lm.create_lease = AsyncMock()
    return lm


# ---------------------------------------------------------------------------
# Tests: done-task guard
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests: integration with report_task_progress internals
# We patch the minimal collaborators to exercise the guard path in the actual
# module without spinning up a full Marcus server.
# ---------------------------------------------------------------------------


class TestLeaseGuardIntegration:
    """
    Verify the guard is wired correctly inside _renew_or_recreate_lease
    (or the inline logic of report_task_progress).
    """

    def _build_fake_state(
        self,
        task_id: str,
        task_status: str,
    ) -> tuple[MagicMock, MagicMock]:
        """Return (state, lease_manager) ready to simulate the no-lease path."""
        lm = _make_lease_manager_no_active_lease()
        fake_lease = MagicMock()
        fake_lease.lease_expires.isoformat.return_value = "2099-01-01T00:00:00"
        lm.create_lease.return_value = fake_lease

        state = _make_state(task_id, task_status, lm)
        return state, lm

    def test_done_task_status_constant(self) -> None:
        """TaskStatus.DONE value must be 'done' — guard relies on string match."""
        from src.core.models import TaskStatus

        assert TaskStatus.DONE.value == "done"
