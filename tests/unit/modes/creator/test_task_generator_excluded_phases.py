"""
Unit tests for TaskGenerator.generate_from_template's excluded_phases handling.

Regression coverage: ``excluded_phases`` was compared against
``phase.name.lower()`` without itself being lowercased. Template phase
names are capitalized (e.g. "Testing"), so a caller passing the natural,
displayed casing (``excluded_phases=["Testing"]``) silently failed to
exclude anything.
"""

import pytest

from src.modes.creator.task_generator import TaskGenerator
from src.modes.creator.template_library import (
    PhaseTemplate,
    ProjectTemplate,
    TaskTemplate,
)

pytestmark = pytest.mark.unit


def _make_template() -> ProjectTemplate:
    setup_phase = PhaseTemplate(
        name="Setup",
        description="Initial setup",
        order=1,
        tasks=[
            TaskTemplate(
                name="Initialize repo",
                description="Set up the repository",
                phase="Setup",
                estimated_hours=1,
            )
        ],
    )
    testing_phase = PhaseTemplate(
        name="Testing",
        description="Test the app",
        order=2,
        tasks=[
            TaskTemplate(
                name="Write tests",
                description="Add test coverage",
                phase="Testing",
                estimated_hours=2,
            )
        ],
    )
    return ProjectTemplate(
        name="Sample",
        description="A sample template",
        category="web",
        phases=[setup_phase, testing_phase],
    )


class TestExcludedPhasesCaseInsensitive:
    @pytest.mark.asyncio
    async def test_excludes_phase_using_displayed_casing(self) -> None:
        """excluded_phases=["Testing"] must exclude the "Testing" phase."""
        generator = TaskGenerator()
        template = _make_template()

        tasks = await generator.generate_from_template(
            template, {"excluded_phases": ["Testing"]}
        )

        task_names = [t.name for t in tasks]
        assert "Write tests" not in task_names
        assert "Initialize repo" in task_names

    @pytest.mark.asyncio
    async def test_excludes_phase_using_lowercase(self) -> None:
        """Lowercase input must still work (no regression)."""
        generator = TaskGenerator()
        template = _make_template()

        tasks = await generator.generate_from_template(
            template, {"excluded_phases": ["testing"]}
        )

        task_names = [t.name for t in tasks]
        assert "Write tests" not in task_names
