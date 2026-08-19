"""
Unit tests for HybridDependencyInferer._get_ai_dependencies direction handling.

The "1->2"/"2->1" branches previously produced the *identical*
dependent/dependency assignment (dependent=task1_id, dependency=task2_id)
regardless of which direction the AI actually returned, silently
discarding half of the AI's determinations.

Direction convention (pinned by the existing, pre-fix-verified tests
test_hybrid_mode_low_confidence_triggers_ai and
test_combined_confidence_boost in test_hybrid_dependency_inferer.py,
cross-checked against the pure pattern-based test's adjacency
assertions): "X->Y" means "X precedes Y", i.e. Y depends on X.
- "1->2": task1 precedes task2 -> dependent=task2_id, dependency=task1_id
- "2->1": task2 precedes task1 -> dependent=task1_id, dependency=task2_id
The two arms must be exact opposites of each other for the direction
field to carry any information at all.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.intelligence.dependency_inferer_hybrid import HybridDependencyInferer

pytestmark = pytest.mark.unit


def _make_task(task_id: str, name: str) -> Task:
    return Task(
        id=task_id,
        name=name,
        description="",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        estimated_hours=1.0,
    )


class TestAiDependencyDirection:
    @pytest.mark.asyncio
    async def test_direction_1_to_2_means_task2_depends_on_task1(self) -> None:
        """
        "1->2": task1 (tests) precedes task2 (deploy), so deploy
        depends on tests.
        """
        task_tests = _make_task("task-tests", "Run integration tests")
        task_deploy = _make_task("task-deploy", "Deploy to production")

        ai_engine = Mock()
        ai_engine._call_claude = AsyncMock(
            return_value=(
                '[{"task1_id": "task-tests", "task2_id": "task-deploy", '
                '"dependency_direction": "1->2", "confidence": 0.9, '
                '"reasoning": "must test before deploying", '
                '"dependency_type": "hard"}]'
            )
        )

        inferer = HybridDependencyInferer(ai_engine=ai_engine)

        result = await inferer._get_ai_dependencies(
            [task_tests, task_deploy], [(task_tests, task_deploy)]
        )

        assert len(result) == 1
        dep = next(iter(result.values()))
        assert dep.dependent_task_id == "task-deploy"
        assert dep.dependency_task_id == "task-tests"

    @pytest.mark.asyncio
    async def test_direction_2_to_1_means_task1_depends_on_task2(self) -> None:
        """
        "2->1": task2 (tests) precedes task1 (deploy), so deploy
        depends on tests — same real-world outcome as the "1->2" case
        above, reached with task1_id/task2_id swapped, proving the two
        branches are genuine opposites rather than duplicates.
        """
        task_deploy = _make_task("task-deploy", "Deploy to production")
        task_tests = _make_task("task-tests", "Run integration tests")

        ai_engine = Mock()
        ai_engine._call_claude = AsyncMock(
            return_value=(
                '[{"task1_id": "task-deploy", "task2_id": "task-tests", '
                '"dependency_direction": "2->1", "confidence": 0.9, '
                '"reasoning": "must test before deploying", '
                '"dependency_type": "hard"}]'
            )
        )

        inferer = HybridDependencyInferer(ai_engine=ai_engine)

        result = await inferer._get_ai_dependencies(
            [task_deploy, task_tests], [(task_deploy, task_tests)]
        )

        assert len(result) == 1
        dep = next(iter(result.values()))
        assert dep.dependent_task_id == "task-deploy"
        assert dep.dependency_task_id == "task-tests"
