"""
Unit tests for ProjectMonitor's stalled-task detection.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.monitoring.project_monitor import ProjectMonitor


def _make_monitor(stall_threshold_hours=24):
    """Build a ProjectMonitor without running its heavy __init__.

    ProjectMonitor.__init__ constructs a KanbanClient, AIAnalysisEngine, etc.
    _check_stalled_tasks only touches self.settings, self.risks, and
    self._get_all_tasks(), so bypass __init__ and set only those.
    """
    monitor = ProjectMonitor.__new__(ProjectMonitor)
    monitor.settings = type(
        "FakeSettings", (), {"get": lambda self, key, default=None: {
            "stall_threshold_hours": stall_threshold_hours
        }.get(key, default)}
    )()
    monitor.risks = []
    return monitor


class TestCheckStalledTasks:
    """Test suite for ProjectMonitor._check_stalled_tasks."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_stalled_task_reports_actual_elapsed_hours(self):
        """Regression: the risk description previously always reported the
        stall *threshold* (a constant), never how long the task had
        actually been stalled — so a task stalled for 10 days looked
        identical to one stalled for 25 hours in every generated risk."""
        now = datetime.now(timezone.utc)
        stalled_task = Task(
            id="1",
            name="Long stalled task",
            status=TaskStatus.IN_PROGRESS,
            assigned_to="agent-1",
            priority=Priority.MEDIUM,
            description="",
            created_at=now - timedelta(days=10),
            updated_at=now - timedelta(days=10),
            due_date=None,
            estimated_hours=0.0,
        )
        monitor = _make_monitor(stall_threshold_hours=24)
        monitor._get_all_tasks = AsyncMock(return_value=[stalled_task])

        await monitor._check_stalled_tasks()

        assert len(monitor.risks) == 1
        description = monitor.risks[0].description
        # ~240 hours stalled, not the 24-hour threshold.
        assert "24 hours" not in description
        assert "240" in description or "239" in description

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_task_within_threshold_is_not_flagged(self):
        """A task updated recently is not stalled and produces no risk."""
        now = datetime.now(timezone.utc)
        fresh_task = Task(
            id="2",
            name="Fresh task",
            status=TaskStatus.IN_PROGRESS,
            assigned_to="agent-1",
            priority=Priority.MEDIUM,
            description="",
            created_at=now,
            updated_at=now,
            due_date=None,
            estimated_hours=0.0,
        )
        monitor = _make_monitor(stall_threshold_hours=24)
        monitor._get_all_tasks = AsyncMock(return_value=[fresh_task])

        await monitor._check_stalled_tasks()

        assert monitor.risks == []
