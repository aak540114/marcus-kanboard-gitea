"""
Unit tests for predict_cascade_effects in src/marcus_mcp/tools/predictions.py.

Regression coverage for a diamond-dependency double-counting bug: the
inner ``find_dependents`` recursive walk had no visited-set guard, so a
task reachable via two different dependency paths (a "diamond" shape) was
counted twice in ``affected_tasks`` and in ``total_delay_impact``. The
same missing guard also risked infinite recursion if the dependency
graph ever contained a real cycle.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.predictions import predict_cascade_effects

pytestmark = pytest.mark.unit


def _make_task(task_id: str, dependencies: list[str]) -> Task:
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        estimated_hours=4.0,
        dependencies=dependencies,
    )


def _make_state_with_tasks(tasks: list[Task], target_task: Task) -> Mock:
    kanban_provider = Mock()
    kanban_provider.get_task = AsyncMock(return_value=target_task)
    kanban_provider.get_tasks = AsyncMock(return_value=tasks)

    project_context = Mock()
    project_context.kanban_provider = kanban_provider
    project_context.memory = Mock()
    project_context.memory.predict_completion_time = AsyncMock(
        return_value={"predicted_completion": None}
    )

    state = Mock()
    state.get_current_project_context = Mock(return_value=project_context)
    state.get_project_context = Mock(return_value=project_context)
    state.current_project = Mock()
    state.current_project.id = "proj-1"
    return state


class TestPredictCascadeEffectsDiamond:
    """A shared descendant reachable via two paths must be counted once."""

    @pytest.mark.asyncio
    async def test_diamond_dependency_not_double_counted(self) -> None:
        # task-1 is delayed; task-2 and task-3 both depend on task-1;
        # task-4 depends on both task-2 and task-3.
        task_1 = _make_task("task-1", [])
        task_2 = _make_task("task-2", ["task-1"])
        task_3 = _make_task("task-3", ["task-1"])
        task_4 = _make_task("task-4", ["task-2", "task-3"])
        all_tasks = [task_1, task_2, task_3, task_4]

        state = _make_state_with_tasks(all_tasks, task_1)

        result = await predict_cascade_effects(
            task_id="task-1", delay_days=1, state=state
        )

        assert result["success"] is True
        returned_ids = [t["id"] for t in result["affected_tasks"]]
        assert sorted(returned_ids) == sorted(["task-2", "task-3", "task-4"])
        assert returned_ids.count("task-4") == 1
        assert result["total_delay_impact"] == 1 * 3

    @pytest.mark.asyncio
    async def test_does_not_infinite_recurse_on_cyclic_dependencies(self) -> None:
        """A cyclic dependency graph must not cause infinite recursion."""
        task_1 = _make_task("task-1", ["task-2"])
        task_2 = _make_task("task-2", ["task-1"])
        all_tasks = [task_1, task_2]

        state = _make_state_with_tasks(all_tasks, task_1)

        result = await predict_cascade_effects(
            task_id="task-1", delay_days=1, state=state
        )

        assert result["success"] is True
        returned_ids = [t["id"] for t in result["affected_tasks"]]
        assert returned_ids.count("task-2") == 1
