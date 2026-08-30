"""
Unit tests for the "🏗️ Architectural Notes" instruction layer in
build_tiered_instructions (src/marcus_mcp/tools/task.py, Layer 1.25).

Background: Marcus's board is the only channel between an agent and a
human. A library pick, a chosen pattern, or a tradeoff an agent makes
while implementing never reaches anyone unless the agent itself says so.
This layer tells every agent, on every task, to post a "🏗️ Note: ..."
comment via the existing post_ticket_progress tool (already scoped to the
ticket) whenever it makes such a choice. The exact "🏗️ Note:" prefix
matters — src/core/decision_notes.py scans for it verbatim to build the
Decisions Log shown on the /project-description page.
"""

from datetime import datetime, timezone
from typing import Optional

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.task import build_tiered_instructions

pytestmark = pytest.mark.unit

_NOTES_SENTINEL = "ARCHITECTURAL NOTES"
_PREFIX_SENTINEL = "🏗️ Note:"
_TOOL_SENTINEL = "post_ticket_progress"


def _task(
    name: str = "Implement auth module",
    labels: Optional[list] = None,
    acceptance_criteria: Optional[list] = None,
) -> Task:
    now = datetime.now(timezone.utc)
    task = Task(
        id="task-test",
        name=name,
        description="Test task description",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=2.0,
        labels=labels or [],
        dependencies=[],
    )
    if acceptance_criteria is not None:
        task.acceptance_criteria = acceptance_criteria
    return task


class TestArchitecturalNotesGuidanceAlwaysFires:
    """Unlike the label-gated layers, this one is unconditional — every
    task gets it, regardless of type, labels, or acceptance criteria."""

    def test_appears_for_plain_implementation_task(self):
        result = build_tiered_instructions(
            base_instructions="Build the feature.",
            task=_task(),
            context_data=None,
            dependency_awareness=None,
            predictions=None,
        )
        assert _NOTES_SENTINEL in result
        assert _PREFIX_SENTINEL in result
        assert _TOOL_SENTINEL in result

    def test_appears_for_design_task(self):
        result = build_tiered_instructions(
            base_instructions="Design the registry.",
            task=_task(name="Design the widget registry", labels=["design"]),
            context_data=None,
            dependency_awareness=None,
            predictions=None,
        )
        assert _NOTES_SENTINEL in result

    def test_appears_regardless_of_acceptance_criteria(self):
        result = build_tiered_instructions(
            base_instructions="Build it.",
            task=_task(acceptance_criteria=["Button is visible"]),
            context_data=None,
            dependency_awareness=None,
            predictions=None,
        )
        assert _NOTES_SENTINEL in result

    def test_appears_with_no_context_data_and_no_predictions(self):
        """Must not depend on context_data/predictions being populated —
        those gate other layers (3, 4, 5) but not this one."""
        result = build_tiered_instructions(
            base_instructions="Build it.",
            task=_task(),
            context_data=None,
            dependency_awareness=None,
            predictions=None,
        )
        assert _NOTES_SENTINEL in result


class TestArchitecturalNotesGuidanceContent:
    def test_mentions_library_pattern_tradeoff_examples(self):
        result = build_tiered_instructions(
            base_instructions="Build it.",
            task=_task(),
            context_data=None,
            dependency_awareness=None,
            predictions=None,
        )
        assert "library" in result.lower()
        assert "pattern" in result.lower()
        assert "tradeoff" in result.lower()

    def test_tells_agent_to_call_post_ticket_progress_with_the_exact_prefix(self):
        """The exact "🏗️ Note:" string must appear as the literal prefix
        the agent is told to use, since decision_notes.py's regex looks
        for it verbatim."""
        result = build_tiered_instructions(
            base_instructions="Build it.",
            task=_task(),
            context_data=None,
            dependency_awareness=None,
            predictions=None,
        )
        assert "'🏗️ Note:'" in result or '"🏗️ Note:"' in result
