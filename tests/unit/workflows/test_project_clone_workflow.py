"""
Unit tests for src/workflows/project_clone_workflow.py

ProjectCloneWorkflow orchestrates the "clone this project" feature: create
a new Kanboard project and replicate a baseline project's tickets, links,
description, settings, and git repo into it. The kanban client, project
sync workflow, and settings managers are mocked; the lifecycle manager is
a REAL TicketLifecycleManager (backed by a temp file) so lifecycle-state
seeding is exercised against the actual state machine, not a mock stand-in
for it.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.core.ticket_lifecycle import TicketLifecycleManager, TicketState
from src.workflows.project_clone_workflow import ProjectCloneWorkflow


def _make_task(
    task_id="1",
    name="Ticket",
    description="desc",
    status=TaskStatus.TODO,
    priority=Priority.MEDIUM,
    labels=None,
    assigned_to=None,
):
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description=description,
        status=status,
        priority=priority,
        assigned_to=assigned_to,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=0.0,
        labels=labels or [],
    )


@pytest.fixture
def mock_kanban():
    kb = MagicMock()
    kb.create_project = AsyncMock(return_value=99)
    kb.get_tasks_for_project = AsyncMock(return_value=[])
    kb.create_task = AsyncMock()
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
def mock_gate_settings():
    gs = MagicMock()
    gs.get_project_gate = MagicMock(return_value=None)
    gs.get_project_verify_count = MagicMock(return_value=None)
    gs.get_project_decompose_enabled = MagicMock(return_value=None)
    return gs


@pytest.fixture
def mock_project_access():
    pa = MagicMock()
    pa.get_project_enabled = MagicMock(return_value=None)
    return pa


@pytest.fixture
def mock_project_description():
    pd = MagicMock()
    pd.get_description = MagicMock(return_value=None)
    pd.get_source = MagicMock(return_value="template")
    return pd


@pytest.fixture
def lifecycle(tmp_path):
    return TicketLifecycleManager(state_file=str(tmp_path / "lifecycle.json"))


@pytest.fixture
def mock_human_gated():
    hg = MagicMock()
    branch_mgr = MagicMock()
    branch_mgr.create_branch = AsyncMock(return_value=True)
    hg.branch_manager_for_repo = MagicMock(return_value=branch_mgr)
    return hg


@pytest.fixture
def workflow(
    mock_kanban,
    lifecycle,
    mock_human_gated,
    mock_project_sync,
    mock_gate_settings,
    mock_project_access,
    mock_project_description,
):
    return ProjectCloneWorkflow(
        kanban=mock_kanban,
        lifecycle=lifecycle,
        human_gated_workflow=mock_human_gated,
        project_sync=mock_project_sync,
        gate_settings=mock_gate_settings,
        project_access=mock_project_access,
        project_description=mock_project_description,
    )


# ---------------------------------------------------------------------------
# clone_project: top-level orchestration
# ---------------------------------------------------------------------------


class TestCloneProjectBasics:
    @pytest.mark.asyncio
    async def test_creates_new_project_with_given_name(self, workflow, mock_kanban):
        result = await workflow.clone_project(1, "Cloned App")
        mock_kanban.create_project.assert_awaited_once_with("Cloned App")
        assert result.new_project_id == 99

    @pytest.mark.asyncio
    async def test_returns_empty_ticket_map_when_baseline_has_no_tickets(self, workflow):
        result = await workflow.clone_project(1, "Cloned App")
        assert result.ticket_id_map == {}
        assert isinstance(result.warnings, list)


# ---------------------------------------------------------------------------
# Ticket cloning: id mapping, columns, labels, priority, assignee
# ---------------------------------------------------------------------------


class TestCloneTickets:
    @pytest.mark.asyncio
    async def test_id_mapping_correctness(self, workflow, mock_kanban):
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5"), _make_task(task_id="7")]
        )
        mock_kanban.create_task = AsyncMock(
            side_effect=[
                _make_task(task_id="105"),
                _make_task(task_id="107"),
            ]
        )
        result = await workflow.clone_project(1, "Cloned App")
        assert result.ticket_id_map == {"5": "105", "7": "107"}

    @pytest.mark.asyncio
    async def test_creates_tickets_under_new_project_id(self, workflow, mock_kanban):
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5", name="Fix bug", description="details")]
        )
        mock_kanban.create_task = AsyncMock(return_value=_make_task(task_id="105"))
        await workflow.clone_project(1, "Cloned App")
        call = mock_kanban.create_task.call_args.args[0]
        assert call["project_id"] == 99
        assert call["name"] == "Fix bug"
        assert call["description"] == "details"

    @pytest.mark.asyncio
    async def test_column_preservation(self, workflow, mock_kanban):
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5", status=TaskStatus.BLOCKED)]
        )
        mock_kanban.create_task = AsyncMock(return_value=_make_task(task_id="105"))
        await workflow.clone_project(1, "Cloned App")
        mock_kanban.move_task_to_column.assert_awaited_once_with("105", "Blocked")

    @pytest.mark.asyncio
    async def test_label_recreation(self, workflow, mock_kanban):
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5", labels=["urgent", "backend"])]
        )
        mock_kanban.create_task = AsyncMock(return_value=_make_task(task_id="105"))
        await workflow.clone_project(1, "Cloned App")
        mock_kanban.set_task_tags.assert_awaited_once_with(
            "105", project_id=99, tags=["urgent", "backend"]
        )

    @pytest.mark.asyncio
    async def test_no_tag_call_when_no_labels(self, workflow, mock_kanban):
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5", labels=[])]
        )
        mock_kanban.create_task = AsyncMock(return_value=_make_task(task_id="105"))
        await workflow.clone_project(1, "Cloned App")
        mock_kanban.set_task_tags.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_assignee_preserved(self, workflow, mock_kanban):
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5", assigned_to="42")]
        )
        mock_kanban.create_task = AsyncMock(return_value=_make_task(task_id="105"))
        await workflow.clone_project(1, "Cloned App")
        mock_kanban.assign_task.assert_awaited_once_with("105", "42")

    @pytest.mark.asyncio
    async def test_baseline_fetch_failure_does_not_propagate(
        self, workflow, mock_kanban
    ):
        """Regression: clone_project()'s own docstring promises it raises
        ONLY if the new project itself can't be created — every step
        after that is best-effort. get_tasks_for_project failing (a
        transient Kanboard RPC error, unlike per-ticket create_task
        failures a few lines below, which were already individually
        caught) must not propagate uncaught: by the time this runs,
        create_project/_clone_repo/_clone_description/_clone_settings
        have already run and potentially created real state (the new
        Kanboard project, possibly an already-mirrored git repo) — an
        uncaught exception here would abort clone_project() entirely and
        leave the caller with no way to discover any of that."""
        mock_kanban.get_tasks_for_project = AsyncMock(
            side_effect=RuntimeError("Kanboard RPC timed out")
        )

        result = await workflow.clone_project(1, "Cloned App")

        assert result.new_project_id == 99
        assert result.ticket_id_map == {}
        assert any(
            "Kanboard RPC timed out" in w or "list baseline" in w.lower()
            for w in result.warnings
        )
        # The failure did not merely get swallowed silently — the caller
        # (server.py's _run_clone) still gets a usable, non-raised result
        # with the real new_project_id it needs to discover the
        # already-created project. (An uncaught exception here would
        # have made this whole `await` raise instead of returning.)

    @pytest.mark.asyncio
    async def test_no_assign_call_when_unassigned(self, workflow, mock_kanban):
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5", assigned_to=None)]
        )
        mock_kanban.create_task = AsyncMock(return_value=_make_task(task_id="105"))
        await workflow.clone_project(1, "Cloned App")
        mock_kanban.assign_task.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_ticket_failure_does_not_abort_remaining_tickets(
        self, workflow, mock_kanban
    ):
        """Best-effort: a single ticket's create_task failure is recorded
        as a warning, and the rest of the clone still proceeds."""
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5"), _make_task(task_id="7")]
        )
        mock_kanban.create_task = AsyncMock(
            side_effect=[RuntimeError("Kanboard down"), _make_task(task_id="107")]
        )
        result = await workflow.clone_project(1, "Cloned App")
        assert result.ticket_id_map == {"7": "107"}
        assert any("5" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Link recreation
# ---------------------------------------------------------------------------


class TestCloneLinks:
    @pytest.mark.asyncio
    async def test_recreates_link_between_two_cloned_tickets(self, workflow, mock_kanban):
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5"), _make_task(task_id="9")]
        )
        mock_kanban.create_task = AsyncMock(
            side_effect=[_make_task(task_id="105"), _make_task(task_id="109")]
        )

        async def raw_links(task_id):
            if task_id == "5":
                return [{"task_id": 9, "label": "blocks", "link_id": 2}]
            return [{"task_id": 5, "label": "is blocked by", "link_id": 2}]

        mock_kanban.get_raw_task_links = AsyncMock(side_effect=raw_links)

        await workflow.clone_project(1, "Cloned App")

        mock_kanban.create_task_link.assert_awaited_once_with("105", "109", 2)

    @pytest.mark.asyncio
    async def test_link_created_only_once_for_bidirectional_pair(
        self, workflow, mock_kanban
    ):
        """Kanboard reports the link from BOTH endpoints — must not create
        it twice (create_task_link already auto-creates the opposite
        direction on the other task)."""
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5"), _make_task(task_id="9")]
        )
        mock_kanban.create_task = AsyncMock(
            side_effect=[_make_task(task_id="105"), _make_task(task_id="109")]
        )

        async def raw_links(task_id):
            if task_id == "5":
                return [{"task_id": 9, "label": "blocks", "link_id": 2}]
            return [{"task_id": 5, "label": "is blocked by", "link_id": 2}]

        mock_kanban.get_raw_task_links = AsyncMock(side_effect=raw_links)

        await workflow.clone_project(1, "Cloned App")

        assert mock_kanban.create_task_link.await_count == 1

    @pytest.mark.asyncio
    async def test_skips_link_to_a_ticket_that_was_not_cloned(self, workflow, mock_kanban):
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5")]
        )
        mock_kanban.create_task = AsyncMock(return_value=_make_task(task_id="105"))
        mock_kanban.get_raw_task_links = AsyncMock(
            return_value=[{"task_id": 999, "label": "relates to"}]
        )

        await workflow.clone_project(1, "Cloned App")

        mock_kanban.create_task_link.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_link_type_resolution_uses_getAllLinks_when_no_raw_link_id(
        self, workflow, mock_kanban
    ):
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5"), _make_task(task_id="9")]
        )
        mock_kanban.create_task = AsyncMock(
            side_effect=[_make_task(task_id="105"), _make_task(task_id="109")]
        )
        mock_kanban.get_link_type_map = AsyncMock(return_value={"blocks": 7})

        async def raw_links(task_id):
            if task_id == "5":
                return [{"task_id": 9, "label": "blocks"}]  # no link_id field
            return []

        mock_kanban.get_raw_task_links = AsyncMock(side_effect=raw_links)

        await workflow.clone_project(1, "Cloned App")

        mock_kanban.create_task_link.assert_awaited_once_with("105", "109", 7)

    @pytest.mark.asyncio
    async def test_link_type_falls_back_to_default_when_unresolvable(
        self, workflow, mock_kanban
    ):
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5"), _make_task(task_id="9")]
        )
        mock_kanban.create_task = AsyncMock(
            side_effect=[_make_task(task_id="105"), _make_task(task_id="109")]
        )
        mock_kanban.get_link_type_map = AsyncMock(return_value={})

        async def raw_links(task_id):
            if task_id == "5":
                return [{"task_id": 9, "label": "blocks"}]
            return []

        mock_kanban.get_raw_task_links = AsyncMock(side_effect=raw_links)

        await workflow.clone_project(1, "Cloned App")

        mock_kanban.create_task_link.assert_awaited_once_with("105", "109", 6)

    @pytest.mark.asyncio
    async def test_get_link_type_map_failure_does_not_abort_link_cloning(
        self, workflow, mock_kanban
    ):
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5"), _make_task(task_id="9")]
        )
        mock_kanban.create_task = AsyncMock(
            side_effect=[_make_task(task_id="105"), _make_task(task_id="109")]
        )
        mock_kanban.get_link_type_map = AsyncMock(side_effect=RuntimeError("Method not found"))

        async def raw_links(task_id):
            if task_id == "5":
                return [{"task_id": 9, "label": "blocks", "link_id": 2}]
            return []

        mock_kanban.get_raw_task_links = AsyncMock(side_effect=raw_links)

        result = await workflow.clone_project(1, "Cloned App")

        mock_kanban.create_task_link.assert_awaited_once_with("105", "109", 2)
        assert any("link-type map" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Lifecycle-state seeding
# ---------------------------------------------------------------------------


class TestSeedLifecycle:
    @pytest.mark.asyncio
    async def test_untouched_baseline_ticket_gets_no_lifecycle_record(
        self, workflow, mock_kanban, lifecycle
    ):
        """A baseline ticket AI never touched (no lifecycle record) means
        the clone also starts untouched — no record created."""
        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5")]
        )
        mock_kanban.create_task = AsyncMock(return_value=_make_task(task_id="105"))

        await workflow.clone_project(1, "Cloned App")

        assert lifecycle.get("105", "kanboard") is None

    @pytest.mark.asyncio
    async def test_ready_baseline_ticket_seeds_ready_clone(
        self, workflow, mock_kanban, lifecycle
    ):
        lifecycle.get_or_create("5", "kanboard")
        lifecycle.transition("5", "kanboard", TicketState.READY)

        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5")]
        )
        mock_kanban.create_task = AsyncMock(return_value=_make_task(task_id="105"))

        await workflow.clone_project(1, "Cloned App")

        new_record = lifecycle.get("105", "kanboard")
        assert new_record is not None
        assert new_record.state == TicketState.READY

    @pytest.mark.asyncio
    async def test_in_progress_baseline_ticket_seeds_in_progress_clone_via_transition(
        self, workflow, mock_kanban, lifecycle
    ):
        """Uses transition() (AI-initiated), never human_transition() —
        confirmed by the resulting state being reachable only through the
        legal AI path, and ai_agent_id staying unset (unclaimed)."""
        lifecycle.get_or_create("5", "kanboard")
        lifecycle.transition("5", "kanboard", TicketState.READY)
        lifecycle.transition("5", "kanboard", TicketState.IN_PROGRESS, agent_id="agent-1")

        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5")]
        )
        mock_kanban.create_task = AsyncMock(return_value=_make_task(task_id="105"))

        await workflow.clone_project(1, "Cloned App")

        new_record = lifecycle.get("105", "kanboard")
        assert new_record.state == TicketState.IN_PROGRESS
        assert new_record.ai_agent_id is None  # unclaimed — not agent-1's

    @pytest.mark.asyncio
    async def test_waiting_for_human_baseline_ticket_seeds_via_transition_not_human_transition(
        self, workflow, mock_kanban, lifecycle
    ):
        """WAITING_FOR_HUMAN is forbidden as a human_transition() target —
        this must succeed, proving transition() (not human_transition())
        is used."""
        lifecycle.get_or_create("5", "kanboard")
        lifecycle.transition("5", "kanboard", TicketState.READY)
        lifecycle.transition("5", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("5", "kanboard", TicketState.WAITING_FOR_HUMAN)

        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5")]
        )
        mock_kanban.create_task = AsyncMock(return_value=_make_task(task_id="105"))

        await workflow.clone_project(1, "Cloned App")

        new_record = lifecycle.get("105", "kanboard")
        assert new_record.state == TicketState.WAITING_FOR_HUMAN

    @pytest.mark.asyncio
    async def test_blocked_baseline_ticket_seeds_blocked_clone(
        self, workflow, mock_kanban, lifecycle
    ):
        lifecycle.get_or_create("5", "kanboard")
        lifecycle.transition("5", "kanboard", TicketState.READY)
        lifecycle.transition("5", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("5", "kanboard", TicketState.BLOCKED)

        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5")]
        )
        mock_kanban.create_task = AsyncMock(return_value=_make_task(task_id="105"))

        await workflow.clone_project(1, "Cloned App")

        assert lifecycle.get("105", "kanboard").state == TicketState.BLOCKED

    @pytest.mark.asyncio
    async def test_done_baseline_ticket_seeds_done_clone(
        self, workflow, mock_kanban, lifecycle
    ):
        lifecycle.get_or_create("5", "kanboard")
        lifecycle.transition("5", "kanboard", TicketState.READY)
        lifecycle.transition("5", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("5", "kanboard", TicketState.DONE)

        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5")]
        )
        mock_kanban.create_task = AsyncMock(return_value=_make_task(task_id="105"))

        await workflow.clone_project(1, "Cloned App")

        assert lifecycle.get("105", "kanboard").state == TicketState.DONE


# ---------------------------------------------------------------------------
# Branch seeding (in-progress clones only)
# ---------------------------------------------------------------------------


class TestBranchSeeding:
    @pytest.mark.asyncio
    async def test_in_progress_clone_seeds_branch_from_baseline_branch(
        self, workflow, mock_kanban, lifecycle, mock_human_gated, mock_project_sync
    ):
        lifecycle.get_or_create("5", "kanboard", branch_name="ticket/kanboard/5")
        lifecycle.transition("5", "kanboard", TicketState.READY)
        lifecycle.transition("5", "kanboard", TicketState.IN_PROGRESS)

        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5")]
        )
        mock_kanban.create_task = AsyncMock(return_value=_make_task(task_id="105"))
        mock_project_sync.get_repo_for_project = MagicMock(
            return_value={"local_repo_path": "./data/repos/cloned-app"}
        )

        await workflow.clone_project(1, "Cloned App")

        mock_human_gated.branch_manager_for_repo.assert_called_with(
            "./data/repos/cloned-app"
        )
        branch_mgr = mock_human_gated.branch_manager_for_repo.return_value
        branch_mgr.create_branch.assert_awaited_once_with(
            "ticket/kanboard/105", from_branch="ticket/kanboard/5"
        )

    @pytest.mark.asyncio
    async def test_no_branch_seed_when_new_project_has_no_repo(
        self, workflow, mock_kanban, lifecycle, mock_human_gated, mock_project_sync
    ):
        lifecycle.get_or_create("5", "kanboard")
        lifecycle.transition("5", "kanboard", TicketState.READY)
        lifecycle.transition("5", "kanboard", TicketState.IN_PROGRESS)

        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5")]
        )
        mock_kanban.create_task = AsyncMock(return_value=_make_task(task_id="105"))
        mock_project_sync.get_repo_for_project = MagicMock(return_value=None)

        result = await workflow.clone_project(1, "Cloned App")

        mock_human_gated.branch_manager_for_repo.assert_not_called()
        assert any("105" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_ready_clone_does_not_seed_a_branch(
        self, workflow, mock_kanban, lifecycle, mock_human_gated, mock_project_sync
    ):
        """Only IN_PROGRESS clones need a branch — READY/BLOCKED/WFH/DONE
        clones don't have one yet on the baseline either (or don't need
        active work resumed)."""
        lifecycle.get_or_create("5", "kanboard")
        lifecycle.transition("5", "kanboard", TicketState.READY)

        mock_kanban.get_tasks_for_project = AsyncMock(
            return_value=[_make_task(task_id="5")]
        )
        mock_kanban.create_task = AsyncMock(return_value=_make_task(task_id="105"))
        mock_project_sync.get_repo_for_project = MagicMock(
            return_value={"local_repo_path": "./data/repos/cloned-app"}
        )

        await workflow.clone_project(1, "Cloned App")

        mock_human_gated.branch_manager_for_repo.assert_not_called()


# ---------------------------------------------------------------------------
# Project description provenance
# ---------------------------------------------------------------------------


class TestCloneDescription:
    @pytest.mark.asyncio
    async def test_copies_description_text_and_source(
        self, workflow, mock_project_description
    ):
        mock_project_description.get_description = MagicMock(return_value="# My Project\n")
        mock_project_description.get_source = MagicMock(return_value="human")

        await workflow.clone_project(1, "Cloned App")

        mock_project_description.update_description.assert_called_once_with(
            99, "# My Project\n", source="human"
        )

    @pytest.mark.asyncio
    async def test_no_description_means_no_copy(self, workflow, mock_project_description):
        mock_project_description.get_description = MagicMock(return_value=None)

        await workflow.clone_project(1, "Cloned App")

        mock_project_description.update_description.assert_not_called()

    @pytest.mark.asyncio
    async def test_description_copy_failure_does_not_abort_clone(
        self, workflow, mock_project_description, mock_kanban
    ):
        mock_project_description.get_description = MagicMock(return_value="text")
        mock_project_description.update_description = MagicMock(
            side_effect=OSError("disk full")
        )

        result = await workflow.clone_project(1, "Cloned App")

        mock_kanban.create_project.assert_awaited_once()
        assert any("description" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Settings copy
# ---------------------------------------------------------------------------


class TestCloneSettings:
    @pytest.mark.asyncio
    async def test_copies_explicit_gate_settings(
        self, workflow, mock_gate_settings, mock_project_access
    ):
        mock_gate_settings.get_project_gate = MagicMock(return_value="ai")
        mock_gate_settings.get_project_verify_count = MagicMock(return_value=3)
        mock_gate_settings.get_project_decompose_enabled = MagicMock(return_value=False)
        mock_project_access.get_project_enabled = MagicMock(return_value=True)

        await workflow.clone_project(1, "Cloned App")

        mock_gate_settings.set_project_gate.assert_called_once_with(99, "ai")
        mock_gate_settings.set_project_verify_count.assert_called_once_with(99, 3)
        mock_gate_settings.set_project_decompose_enabled.assert_called_once_with(99, False)
        mock_project_access.set_project_enabled.assert_called_once_with(99, True)

    @pytest.mark.asyncio
    async def test_unconfigured_baseline_settings_are_not_copied(
        self, workflow, mock_gate_settings, mock_project_access
    ):
        await workflow.clone_project(1, "Cloned App")

        mock_gate_settings.set_project_gate.assert_not_called()
        mock_gate_settings.set_project_verify_count.assert_not_called()
        mock_gate_settings.set_project_decompose_enabled.assert_not_called()
        mock_project_access.set_project_enabled.assert_not_called()


# ---------------------------------------------------------------------------
# Repo cloning
# ---------------------------------------------------------------------------


class TestCloneRepo:
    @pytest.mark.asyncio
    async def test_clones_repo_when_baseline_has_one(
        self, workflow, mock_project_sync
    ):
        mock_project_sync.get_repo_for_project = MagicMock(
            return_value={"gitea_repo_url": "http://gitea/root/baseline.git"}
        )

        await workflow.clone_project(1, "Cloned App")

        mock_project_sync.ensure_repo_from_source.assert_awaited_once_with(
            99, "Cloned App", "http://gitea/root/baseline.git"
        )

    @pytest.mark.asyncio
    async def test_no_repo_call_when_baseline_has_no_mapping(
        self, workflow, mock_project_sync
    ):
        mock_project_sync.get_repo_for_project = MagicMock(return_value=None)

        result = await workflow.clone_project(1, "Cloned App")

        mock_project_sync.ensure_repo_from_source.assert_not_awaited()
        assert any("no git repo" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_repo_clone_failure_does_not_abort_clone(
        self, workflow, mock_project_sync, mock_kanban
    ):
        mock_project_sync.get_repo_for_project = MagicMock(
            return_value={"gitea_repo_url": "http://gitea/root/baseline.git"}
        )
        mock_project_sync.ensure_repo_from_source = AsyncMock(
            side_effect=RuntimeError("mirror push failed")
        )

        result = await workflow.clone_project(1, "Cloned App")

        mock_kanban.create_project.assert_awaited_once()
        assert any("git repository" in w for w in result.warnings)
