"""
Unit tests for TaskEnricher._suggest_dependencies.

Regression coverage: the configured "typical_dependencies" for a task
type (self.task_patterns[task_type]["typical_dependencies"]) was looked
up but the result was never assigned or added to the returned
suggestions list — it was computed and immediately discarded. Task
types like "backend" (typical_dependencies=["design"]) that aren't
covered by any of the hardcoded if-branches below it got zero
suggestions even though a pattern was configured for them.
"""

from datetime import datetime, timezone

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.modes.enricher.task_enricher import BoardContext, TaskEnricher

pytestmark = pytest.mark.unit


def _make_task(name: str, description: str = "") -> Task:
    return Task(
        id="task-1",
        name=name,
        description=description,
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        estimated_hours=1.0,
    )


class TestSuggestDependenciesUsesTypicalDependencies:
    @pytest.mark.asyncio
    async def test_backend_task_suggests_configured_typical_dependency(self) -> None:
        """
        "backend" tasks have typical_dependencies=["design"] configured
        but aren't covered by any of the hardcoded if-branches, so the
        configured pattern must be the only source of a suggestion.
        """
        enricher = TaskEnricher()
        task = _make_task("Build user service", "Implements the user model")
        board_context = BoardContext(
            project_type="web",
            detected_phases=[],
            detected_components=[],
            common_labels=[],
            workflow_pattern="sequential",
        )

        suggestions = await enricher._suggest_dependencies(
            task, "backend", board_context
        )

        assert "design" in suggestions
