"""
Unit tests for EnricherMode._detect_workflow_pattern.

Regression coverage: the in-progress ratio was looked up with the key
"IN_PROGRESS" against a dict keyed by ``TaskStatus.value`` (lowercase,
e.g. "in_progress"). The lookup always missed, so in_progress_ratio was
always 0.0 and the "parallel" workflow pattern could never be detected.
"""

from datetime import datetime, timezone

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.modes.enricher.enricher_mode import EnricherMode

pytestmark = pytest.mark.unit


def _make_task(task_id: str, status: TaskStatus) -> Task:
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        estimated_hours=1.0,
    )


class TestDetectWorkflowPattern:
    def test_high_in_progress_ratio_detected_as_parallel(self) -> None:
        """8/10 tasks IN_PROGRESS must be detected as 'parallel'."""
        mode = EnricherMode()
        tasks = [
            _make_task(f"t{i}", TaskStatus.IN_PROGRESS) for i in range(8)
        ] + [_make_task("t8", TaskStatus.TODO), _make_task("t9", TaskStatus.DONE)]

        pattern = mode._detect_workflow_pattern(tasks)

        assert pattern == "parallel"

    def test_low_in_progress_ratio_detected_as_sequential(self) -> None:
        """1/10 tasks IN_PROGRESS must be detected as 'sequential'."""
        mode = EnricherMode()
        tasks = [_make_task("t0", TaskStatus.IN_PROGRESS)] + [
            _make_task(f"t{i}", TaskStatus.TODO) for i in range(1, 10)
        ]

        pattern = mode._detect_workflow_pattern(tasks)

        assert pattern == "sequential"
