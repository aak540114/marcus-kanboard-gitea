"""
Cross-project isolation tests for ProjectCloneWorkflow.

Unlike test_project_clone_workflow.py (which mocks every collaborator to
verify the workflow's own call sequencing), this file wires
ProjectCloneWorkflow against REAL GateSettingManager, ProjectAccessSettingManager,
ProjectDescriptionManager, and TicketLifecycleManager instances — the same
JSON/file-backed classes Marcus actually uses in production — so a bug
where the clone's data secretly aliased the baseline's (e.g. two dict
keys pointing at the same mutable object, or a missing project_id
qualifier) would show up as a real assertion failure, not something a
mock could paper over. Only the Kanban RPC client, HumanGatedWorkflow,
and ProjectSyncWorkflow are mocked (no live Kanboard/Gitea in this
environment).

Verifies the property the user explicitly asked for: after cloning
project A into project B, changing A's state must never change B's, and
vice versa.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.gate_settings import GateSettingManager
from src.core.models import Priority, Task, TaskStatus
from src.core.project_access_settings import ProjectAccessSettingManager
from src.core.project_description import ProjectDescriptionManager, SOURCE_HUMAN
from src.core.ticket_lifecycle import TicketLifecycleManager, TicketState
from src.workflows.project_clone_workflow import ProjectCloneWorkflow

BASELINE_ID = 7
NEW_ID = 99  # what mock_kanban.create_project() will return


def _make_task(task_id, name="Ticket", status=TaskStatus.READY, labels=None):
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description="desc",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=0.0,
        labels=labels or [],
    )


@pytest.fixture
def mock_kanban():
    kb = MagicMock()
    kb.create_project = AsyncMock(return_value=NEW_ID)
    kb.get_tasks_for_project = AsyncMock(
        return_value=[_make_task("50", status=TaskStatus.READY)]
    )
    kb.create_task = AsyncMock(return_value=_make_task("150", status=TaskStatus.READY))
    kb.move_task_to_column = AsyncMock(return_value=True)
    kb.set_task_tags = AsyncMock(return_value=True)
    kb.assign_task = AsyncMock(return_value=True)
    kb.get_raw_task_links = AsyncMock(return_value=[])
    kb.get_link_type_map = AsyncMock(return_value={})
    kb.create_task_link = AsyncMock(return_value=True)
    return kb


@pytest.fixture
def mock_project_sync():
    ps = MagicMock()
    ps.get_repo_for_project = MagicMock(return_value=None)
    ps.ensure_repo_from_source = AsyncMock(return_value=None)
    return ps


@pytest.fixture
def mock_human_gated():
    hg = MagicMock()
    branch_mgr = MagicMock()
    branch_mgr.create_branch = AsyncMock(return_value=True)
    hg.branch_manager_for_repo = MagicMock(return_value=branch_mgr)
    return hg


@pytest.fixture
def gate_settings(tmp_path):
    return GateSettingManager(data_dir=tmp_path / "gate")


@pytest.fixture
def project_access(tmp_path):
    return ProjectAccessSettingManager(data_dir=tmp_path / "access")


@pytest.fixture
def project_description(tmp_path):
    return ProjectDescriptionManager(data_dir=tmp_path / "desc")


@pytest.fixture
def lifecycle(tmp_path):
    return TicketLifecycleManager(state_file=str(tmp_path / "lifecycle.json"))


@pytest.fixture
def workflow(
    mock_kanban,
    lifecycle,
    mock_human_gated,
    mock_project_sync,
    gate_settings,
    project_access,
    project_description,
):
    return ProjectCloneWorkflow(
        kanban=mock_kanban,
        lifecycle=lifecycle,
        human_gated_workflow=mock_human_gated,
        project_sync=mock_project_sync,
        gate_settings=gate_settings,
        project_access=project_access,
        project_description=project_description,
    )


class TestSettingsIsolationAfterClone:
    """Gate/verify/decompose/access settings must be independent dict
    entries after clone, not aliases of the baseline's."""

    @pytest.mark.asyncio
    async def test_changing_baseline_gate_after_clone_does_not_change_clone(
        self, workflow, gate_settings
    ):
        gate_settings.set_project_gate(BASELINE_ID, "ai")
        gate_settings.set_project_verify_count(BASELINE_ID, 2)
        await workflow.clone_project(BASELINE_ID, "Cloned App")
        assert gate_settings.get_project_gate(NEW_ID) == "ai"

        gate_settings.set_project_gate(BASELINE_ID, "human")
        gate_settings.set_project_verify_count(BASELINE_ID, 0)

        assert gate_settings.get_project_gate(NEW_ID) == "ai"
        assert gate_settings.get_project_verify_count(NEW_ID) == 2

    @pytest.mark.asyncio
    async def test_changing_clone_gate_after_clone_does_not_change_baseline(
        self, workflow, gate_settings
    ):
        gate_settings.set_project_gate(BASELINE_ID, "ai")
        await workflow.clone_project(BASELINE_ID, "Cloned App")

        gate_settings.set_project_gate(NEW_ID, "human")

        assert gate_settings.get_project_gate(BASELINE_ID) == "ai"

    @pytest.mark.asyncio
    async def test_changing_baseline_decompose_after_clone_does_not_change_clone(
        self, workflow, gate_settings
    ):
        gate_settings.set_project_decompose_enabled(BASELINE_ID, False)
        await workflow.clone_project(BASELINE_ID, "Cloned App")
        assert gate_settings.get_project_decompose_enabled(NEW_ID) is False

        gate_settings.set_project_decompose_enabled(BASELINE_ID, True)

        assert gate_settings.get_project_decompose_enabled(NEW_ID) is False

    @pytest.mark.asyncio
    async def test_changing_baseline_access_after_clone_does_not_change_clone(
        self, workflow, gate_settings, project_access
    ):
        project_access.set_project_enabled(BASELINE_ID, True)
        await workflow.clone_project(BASELINE_ID, "Cloned App")
        assert project_access.is_enabled(NEW_ID) is True

        project_access.set_project_enabled(BASELINE_ID, False)

        assert project_access.is_enabled(NEW_ID) is True  # unaffected

    @pytest.mark.asyncio
    async def test_disabling_clone_after_clone_does_not_disable_baseline(
        self, workflow, gate_settings, project_access
    ):
        project_access.set_project_enabled(BASELINE_ID, True)
        await workflow.clone_project(BASELINE_ID, "Cloned App")

        project_access.set_project_enabled(NEW_ID, False)

        assert project_access.is_enabled(BASELINE_ID) is True


class TestDescriptionIsolationAfterClone:
    @pytest.mark.asyncio
    async def test_editing_baseline_description_after_clone_does_not_change_clone(
        self, workflow, project_description
    ):
        project_description.update_description(
            BASELINE_ID, "# Baseline\nOriginal text.", source=SOURCE_HUMAN
        )
        await workflow.clone_project(BASELINE_ID, "Cloned App")
        assert project_description.get_description(NEW_ID) == (
            "# Baseline\nOriginal text."
        )

        project_description.update_description(
            BASELINE_ID, "# Baseline\nEdited after clone.", source=SOURCE_HUMAN
        )

        assert project_description.get_description(NEW_ID) == (
            "# Baseline\nOriginal text."
        )

    @pytest.mark.asyncio
    async def test_editing_clone_description_after_clone_does_not_change_baseline(
        self, workflow, project_description
    ):
        project_description.update_description(
            BASELINE_ID, "# Baseline\nOriginal.", source=SOURCE_HUMAN
        )
        await workflow.clone_project(BASELINE_ID, "Cloned App")

        project_description.update_description(
            NEW_ID, "# Clone\nDiverged text.", source=SOURCE_HUMAN
        )

        assert project_description.get_description(BASELINE_ID) == (
            "# Baseline\nOriginal."
        )


class TestLifecycleIsolationAfterClone:
    """A cloned ticket's lifecycle record must be a fully independent
    entry, keyed by its OWN (new) ticket id — mutating one ticket's
    state after clone must never move the other."""

    @pytest.mark.asyncio
    async def test_moving_baseline_ticket_after_clone_does_not_move_clone(
        self, workflow, lifecycle, mock_kanban
    ):
        lifecycle.get_or_create("50", "kanboard")
        lifecycle.transition("50", "kanboard", TicketState.READY)
        lifecycle.transition("50", "kanboard", TicketState.IN_PROGRESS)
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task("50", status=TaskStatus.IN_PROGRESS)]
        )
        mock_kanban.create_task = AsyncMock(
            return_value=_make_task("150", status=TaskStatus.IN_PROGRESS)
        )

        result = await workflow.clone_project(BASELINE_ID, "Cloned App")
        new_ticket_id = result.ticket_id_map["50"]
        assert lifecycle.get(new_ticket_id, "kanboard").state == TicketState.IN_PROGRESS

        # Baseline ticket keeps moving after the clone was made.
        lifecycle.transition("50", "kanboard", TicketState.DONE)

        assert lifecycle.get(new_ticket_id, "kanboard").state == TicketState.IN_PROGRESS
        assert lifecycle.get("50", "kanboard").state == TicketState.DONE

    @pytest.mark.asyncio
    async def test_moving_cloned_ticket_after_clone_does_not_move_baseline(
        self, workflow, lifecycle, mock_kanban
    ):
        lifecycle.get_or_create("50", "kanboard")
        lifecycle.transition("50", "kanboard", TicketState.READY)
        lifecycle.transition("50", "kanboard", TicketState.IN_PROGRESS)
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task("50", status=TaskStatus.IN_PROGRESS)]
        )
        mock_kanban.create_task = AsyncMock(
            return_value=_make_task("150", status=TaskStatus.IN_PROGRESS)
        )

        result = await workflow.clone_project(BASELINE_ID, "Cloned App")
        new_ticket_id = result.ticket_id_map["50"]

        lifecycle.transition(new_ticket_id, "kanboard", TicketState.DONE)

        assert lifecycle.get("50", "kanboard").state == TicketState.IN_PROGRESS
        assert lifecycle.get(new_ticket_id, "kanboard").state == TicketState.DONE

    @pytest.mark.asyncio
    async def test_cloned_ticket_history_does_not_include_baseline_history(
        self, workflow, lifecycle, mock_kanban
    ):
        """The clone's lifecycle record must have its OWN history log —
        not a shared/copied reference to the baseline's history list —
        so appending to one never mutates the other."""
        lifecycle.get_or_create("50", "kanboard")
        lifecycle.transition("50", "kanboard", TicketState.READY)
        baseline_history_len_before = len(lifecycle.get("50", "kanboard").history)

        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task("50", status=TaskStatus.READY)]
        )
        mock_kanban.create_task = AsyncMock(
            return_value=_make_task("150", status=TaskStatus.READY)
        )
        result = await workflow.clone_project(BASELINE_ID, "Cloned App")
        new_ticket_id = result.ticket_id_map["50"]

        # Drive the clone's record through several more transitions.
        lifecycle.transition(new_ticket_id, "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition(new_ticket_id, "kanboard", TicketState.DONE)

        assert (
            len(lifecycle.get("50", "kanboard").history)
            == baseline_history_len_before
        )
