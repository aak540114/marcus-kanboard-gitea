"""
Unit tests for HumanGatedWorkflow.

Covers the human-gated AI workflow rules:
  - AI starts when a ticket IS assigned to a human AND status ≠ todo.
  - When a human assigns themselves, AI starts work if the column is already
    past todo; if the column is still todo, AI waits for the next status change.
  - When status changes to ready/in_progress AND a human is assigned, AI starts.
  - When a ticket is unassigned, the AI claim is released and AI stops.
  - Humans cannot push a card to waiting_for_human (AI-only state).
  - The claim gate prevents two Marcus instances from double-starting.
  - get_work_context includes the already_claimed_by field.
  - One ticket per AI agent: agent refuses a second ticket while first is active.
  - When ticket → waiting_for_human / blocked / done, agent picks next ticket.
  - Next ticket is selected in dependency order (READY first, lower ID first).

All external dependencies (kanban, branch manager, dev env, AC generator)
are mocked; no file I/O or network calls occur.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.events import Events
from src.core.ticket_lifecycle import (
    TicketLifecycleManager,
    TicketState,
)
from src.workflows.human_gated_workflow import HumanGatedWorkflow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(data: dict) -> Any:
    """Build a minimal event object with a .data attribute."""
    ev = MagicMock()
    ev.data = data
    return ev


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_file(tmp_path):
    """Temporary lifecycle state file."""
    return str(tmp_path / "lifecycle.json")


@pytest.fixture
def lifecycle(state_file):
    """Fresh lifecycle manager backed by a temp file."""
    return TicketLifecycleManager(state_file=state_file)


@pytest.fixture
def mock_kanban():
    """Mock KanbanInterface."""
    kb = MagicMock()
    kb.move_task_to_column = AsyncMock(return_value=True)
    kb.add_comment = AsyncMock(return_value=1)
    kb.get_task_by_id = AsyncMock(return_value=None)
    return kb


@pytest.fixture
def mock_branch():
    """Mock BranchManager."""
    bm = MagicMock()
    bm.create_branch = AsyncMock(return_value=True)
    bm.merge_to_main = AsyncMock(return_value=True)
    bm.rebase_on_main = AsyncMock(return_value=True)
    bm.get_branch_commits = AsyncMock(return_value=[])
    bm.config = MagicMock()
    bm.config.main_branch = "main"
    bm.make_branch_name = MagicMock(
        side_effect=lambda provider, tid: f"ticket/{provider}/{tid}"
    )
    return bm


@pytest.fixture
def mock_dev_env():
    """Mock DevEnvironmentManager."""
    de = MagicMock()
    de.stop = AsyncMock()
    de.stop_all = AsyncMock()
    de.start = AsyncMock()
    de.get_info = MagicMock(return_value=None)
    return de


@pytest.fixture
def mock_ac_gen():
    """Mock ACGenerator."""
    gen = MagicMock()
    gen.generate = AsyncMock(return_value="- [ ] Acceptance criterion 1")
    return gen


@pytest.fixture
def workflow(lifecycle, mock_kanban, mock_branch, mock_dev_env, mock_ac_gen):
    """HumanGatedWorkflow wired with mocked dependencies."""
    events = Events()
    wf = HumanGatedWorkflow(
        kanban=mock_kanban,
        events=events,
        provider_name="kanboard",
        lifecycle=lifecycle,
        branch_manager=mock_branch,
        dev_env_manager=mock_dev_env,
        ac_generator=mock_ac_gen,
    )
    with patch(
        "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
        side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
    ):
        yield wf


# ---------------------------------------------------------------------------
# Trigger: human assigns ticket + status already past todo → AI starts
# ---------------------------------------------------------------------------


class TestAssignedTrigger:
    """Human assigning themselves is the signal for AI to start work."""

    @pytest.mark.asyncio
    async def test_assign_when_ready_starts_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Assigning human to a ready ticket causes AI to claim and start."""
        lifecycle.get_or_create("10", "kanboard")
        lifecycle.transition("10", "kanboard", TicketState.READY)

        event = _make_event(
            {"ticket_id": "10", "assignee": "alice", "provider": "kanboard"}
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/10",
        ):
            await workflow._on_ticket_assigned(event)

        rec = lifecycle.get("10", "kanboard")
        assert rec is not None
        assert rec.assignee == "alice"
        assert rec.ai_agent_id is not None
        assert rec.state == TicketState.IN_PROGRESS
        mock_kanban.move_task_to_column.assert_called_with("10", "in progress")

    @pytest.mark.asyncio
    async def test_assign_when_in_progress_starts_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Assigning human to an in_progress ticket causes AI to claim it."""
        lifecycle.get_or_create("11", "kanboard")
        lifecycle.transition("11", "kanboard", TicketState.READY)
        lifecycle.transition("11", "kanboard", TicketState.IN_PROGRESS)

        event = _make_event(
            {"ticket_id": "11", "assignee": "bob", "provider": "kanboard"}
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/11",
        ):
            await workflow._on_ticket_assigned(event)

        rec = lifecycle.get("11", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is not None

    @pytest.mark.asyncio
    async def test_assign_when_todo_does_not_start_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Assigning human to a still-todo ticket does NOT start AI."""
        lifecycle.get_or_create("12", "kanboard")

        event = _make_event(
            {"ticket_id": "12", "assignee": "carol", "provider": "kanboard"}
        )
        await workflow._on_ticket_assigned(event)

        rec = lifecycle.get("12", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is None
        mock_kanban.move_task_to_column.assert_not_called()

    @pytest.mark.asyncio
    async def test_assign_records_human_name(self, workflow, lifecycle):
        """Assignee name is stored on the lifecycle record."""
        lifecycle.get_or_create("13", "kanboard")
        event = _make_event(
            {"ticket_id": "13", "assignee": "dave", "provider": "kanboard"}
        )
        await workflow._on_ticket_assigned(event)

        rec = lifecycle.get("13", "kanboard")
        assert rec is not None
        assert rec.assignee == "dave"


# ---------------------------------------------------------------------------
# Trigger: status changes to ready/in_progress with human owner → AI starts
# ---------------------------------------------------------------------------


class TestStatusChangedTrigger:
    """Status-change event triggers AI only when a human is assigned."""

    @pytest.mark.asyncio
    async def test_ready_with_assignee_starts_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Status → ready AND human assigned → AI claims and starts."""
        lifecycle.get_or_create("20", "kanboard")
        lifecycle.set_assignee("20", "kanboard", "alice")

        event = _make_event(
            {"ticket_id": "20", "new_status": "ready", "old_status": "todo",
             "provider": "kanboard"}
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/20",
        ):
            await workflow._on_status_changed(event)

        rec = lifecycle.get("20", "kanboard")
        assert rec is not None
        assert rec.state == TicketState.IN_PROGRESS
        assert rec.ai_agent_id is not None

    @pytest.mark.asyncio
    async def test_ready_without_assignee_does_not_start_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Status → ready with NO human assigned → AI does nothing."""
        lifecycle.get_or_create("21", "kanboard")

        event = _make_event(
            {"ticket_id": "21", "new_status": "ready", "old_status": "todo",
             "provider": "kanboard"}
        )
        await workflow._on_status_changed(event)

        rec = lifecycle.get("21", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is None
        mock_kanban.move_task_to_column.assert_not_called()

    @pytest.mark.asyncio
    async def test_in_progress_with_assignee_starts_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Status → in_progress AND human assigned → AI claims."""
        lifecycle.get_or_create("22", "kanboard")
        lifecycle.transition("22", "kanboard", TicketState.READY)
        lifecycle.set_assignee("22", "kanboard", "bob")

        event = _make_event(
            {"ticket_id": "22", "new_status": "in_progress",
             "old_status": "ready", "provider": "kanboard"}
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/22",
        ):
            await workflow._on_status_changed(event)

        rec = lifecycle.get("22", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is not None

    @pytest.mark.asyncio
    async def test_waiting_for_human_set_by_human_is_rejected(
        self, workflow, lifecycle
    ):
        """Human moving card to waiting_for_human is silently ignored."""
        lifecycle.get_or_create("23", "kanboard")
        lifecycle.transition("23", "kanboard", TicketState.READY)
        lifecycle.transition("23", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("23", "kanboard", "carol")

        event = _make_event(
            {"ticket_id": "23", "new_status": "waiting_for_human",
             "old_status": "in_progress", "provider": "kanboard"}
        )
        await workflow._on_status_changed(event)

        rec = lifecycle.get("23", "kanboard")
        assert rec is not None
        assert rec.state == TicketState.IN_PROGRESS  # unchanged

    @pytest.mark.asyncio
    async def test_todo_status_resets_lifecycle_state(self, workflow, lifecycle):
        """Human moving card to todo updates internal lifecycle state."""
        lifecycle.get_or_create("24", "kanboard")
        lifecycle.transition("24", "kanboard", TicketState.READY)
        lifecycle.set_assignee("24", "kanboard", "dave")

        event = _make_event(
            {"ticket_id": "24", "new_status": "todo",
             "old_status": "ready", "provider": "kanboard"}
        )
        await workflow._on_status_changed(event)

        rec = lifecycle.get("24", "kanboard")
        assert rec is not None
        assert rec.state == TicketState.TODO

    @pytest.mark.asyncio
    async def test_in_progress_from_waiting_for_human_resumes_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Human moving card from waiting_for_human to in_progress resumes AI."""
        lifecycle.get_or_create("25", "kanboard")
        lifecycle.transition("25", "kanboard", TicketState.READY)
        lifecycle.transition("25", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("25", "kanboard", TicketState.WAITING_FOR_HUMAN)
        lifecycle.set_assignee("25", "kanboard", "eve")

        event = _make_event(
            {"ticket_id": "25", "new_status": "in_progress",
             "old_status": "waiting_for_human", "provider": "kanboard"}
        )
        await workflow._on_status_changed(event)

        rec = lifecycle.get("25", "kanboard")
        assert rec is not None
        assert rec.state == TicketState.IN_PROGRESS
        # Branch creation not called — AI is resuming, not starting fresh.
        mock_kanban.move_task_to_column.assert_not_called()


# ---------------------------------------------------------------------------
# Trigger: ticket unassigned → AI releases claim and stops
# ---------------------------------------------------------------------------


class TestUnassignedTrigger:
    """When a human unassigns, AI releases its claim and stops."""

    @pytest.mark.asyncio
    async def test_unassign_releases_ai_claim(
        self, workflow, lifecycle, mock_kanban
    ):
        """Unassigning clears the AI claim."""
        lifecycle.get_or_create("30", "kanboard")
        lifecycle.claim_ticket("30", "kanboard", "agent-x")
        lifecycle.set_assignee("30", "kanboard", "alice")

        event = _make_event({"ticket_id": "30", "provider": "kanboard"})
        await workflow._on_ticket_unassigned(event)

        rec = lifecycle.get("30", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is None
        assert rec.assignee in (None, "", "0")

    @pytest.mark.asyncio
    async def test_unassign_does_not_start_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Unassigning never starts AI work."""
        lifecycle.get_or_create("31", "kanboard")
        lifecycle.transition("31", "kanboard", TicketState.READY)
        lifecycle.set_assignee("31", "kanboard", "bob")

        event = _make_event({"ticket_id": "31", "provider": "kanboard"})
        await workflow._on_ticket_unassigned(event)

        mock_kanban.move_task_to_column.assert_not_called()
        rec = lifecycle.get("31", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is None


# ---------------------------------------------------------------------------
# Anti-duplication: second claim is rejected
# ---------------------------------------------------------------------------


class TestClaimGate:
    """Two concurrent Marcus instances cannot both claim the same ticket."""

    @pytest.mark.asyncio
    async def test_already_claimed_ticket_is_skipped(
        self, workflow, lifecycle, mock_kanban
    ):
        """If a ticket is already claimed, _start_ai_work exits early."""
        lifecycle.get_or_create("40", "kanboard")
        lifecycle.claim_ticket("40", "kanboard", "other-marcus")

        rec = lifecycle.get("40", "kanboard")
        assert rec is not None
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/40",
        ):
            await workflow._start_ai_work("40", rec)

        mock_kanban.move_task_to_column.assert_not_called()
        rec2 = lifecycle.get("40", "kanboard")
        assert rec2 is not None
        assert rec2.ai_agent_id == "other-marcus"  # original holder unchanged

    @pytest.mark.asyncio
    async def test_branch_failure_releases_claim(
        self, workflow, lifecycle, mock_branch
    ):
        """If branch creation fails, the claim is released so retry is possible."""
        mock_branch.create_branch = AsyncMock(return_value=False)
        lifecycle.get_or_create("41", "kanboard")

        rec = lifecycle.get("41", "kanboard")
        assert rec is not None
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/41",
        ):
            await workflow._start_ai_work("41", rec)

        rec2 = lifecycle.get("41", "kanboard")
        assert rec2 is not None
        assert rec2.ai_agent_id is None  # released after failure


# ---------------------------------------------------------------------------
# get_work_context: includes already_claimed_by
# ---------------------------------------------------------------------------


class TestGetWorkContext:
    """get_work_context exposes the current claimant."""

    @pytest.mark.asyncio
    async def test_unclaimed_ticket_has_none_claimed_by(
        self, workflow, lifecycle
    ):
        """already_claimed_by is None for unclaimed tickets."""
        lifecycle.get_or_create("50", "kanboard")
        ctx = await workflow.get_work_context("50")
        assert ctx is not None
        assert ctx["already_claimed_by"] is None

    @pytest.mark.asyncio
    async def test_claimed_ticket_exposes_agent_id(
        self, workflow, lifecycle
    ):
        """already_claimed_by shows the holding agent's identifier."""
        lifecycle.get_or_create("51", "kanboard")
        lifecycle.claim_ticket("51", "kanboard", "marcus-abc123")
        ctx = await workflow.get_work_context("51")
        assert ctx is not None
        assert ctx["already_claimed_by"] == "marcus-abc123"


# ---------------------------------------------------------------------------
# get_work_context: enriched ticket data (priority/labels/due_date/
# estimated_hours/links/recent_comments)
# ---------------------------------------------------------------------------


def _make_task_mock(
    priority=None, labels=None, due_date=None, estimated_hours=None
):
    """Build a minimal Task-like mock carrying only the fields
    get_work_context reads for the enrichment fields."""
    task = MagicMock()
    task.name = "Enriched ticket"
    task.description = "desc"
    task.source_context = {"kanboard_task": {"project_id": 9}}
    task.priority = priority
    task.labels = labels or []
    task.due_date = due_date
    task.estimated_hours = estimated_hours
    return task


class TestGetWorkContextEnrichedFields:
    """get_work_context surfaces priority/labels/due_date/estimated_hours
    (already parsed onto the Task by the provider) and links/comments
    (fetched via optional provider methods), instead of discarding them."""

    @pytest.mark.asyncio
    async def test_priority_and_labels_surfaced(self, workflow, lifecycle, mock_kanban):
        lifecycle.get_or_create("60", "kanboard")
        from src.core.models import Priority

        mock_kanban.get_task_by_id = AsyncMock(
            return_value=_make_task_mock(
                priority=Priority.HIGH, labels=["backend", "urgent"]
            )
        )
        ctx = await workflow.get_work_context("60")
        assert ctx["priority"] == "high"
        assert ctx["labels"] == ["backend", "urgent"]

    @pytest.mark.asyncio
    async def test_due_date_serialized_to_iso_string(self, workflow, lifecycle, mock_kanban):
        from datetime import datetime, timezone

        lifecycle.get_or_create("61", "kanboard")
        due = datetime(2026, 8, 1, tzinfo=timezone.utc)
        mock_kanban.get_task_by_id = AsyncMock(
            return_value=_make_task_mock(due_date=due)
        )
        ctx = await workflow.get_work_context("61")
        assert ctx["due_date"] == due.isoformat()

    @pytest.mark.asyncio
    async def test_missing_priority_and_due_date_are_none(self, workflow, lifecycle, mock_kanban):
        lifecycle.get_or_create("62", "kanboard")
        mock_kanban.get_task_by_id = AsyncMock(return_value=_make_task_mock())
        ctx = await workflow.get_work_context("62")
        assert ctx["priority"] is None
        assert ctx["due_date"] is None
        assert ctx["labels"] == []
        assert ctx["estimated_hours"] is None

    @pytest.mark.asyncio
    async def test_links_fetched_when_kanban_supports_it(self, workflow, lifecycle, mock_kanban):
        lifecycle.get_or_create("63", "kanboard")
        mock_kanban.get_task_by_id = AsyncMock(return_value=_make_task_mock())
        expected_links = {
            "depends_on": [{"task_id": "1", "title": "x", "column": "Done"}],
            "blocks": [],
            "relates_to": [],
        }
        mock_kanban.get_task_links = AsyncMock(return_value=expected_links)
        ctx = await workflow.get_work_context("63")
        assert ctx["links"] == expected_links

    @pytest.mark.asyncio
    async def test_links_empty_when_provider_lacks_support(self, workflow, lifecycle):
        """A provider that genuinely doesn't implement get_task_links (e.g.
        a non-Kanboard KanbanInterface) must not crash get_work_context —
        links/comments just default to empty."""
        lifecycle.get_or_create("64", "kanboard")
        limited_kanban = MagicMock(spec=["get_task_by_id"])
        limited_kanban.get_task_by_id = AsyncMock(return_value=_make_task_mock())
        workflow._kanban = limited_kanban
        ctx = await workflow.get_work_context("64")
        assert ctx["links"] == {"depends_on": [], "blocks": [], "relates_to": []}
        assert ctx["recent_comments"] == []

    @pytest.mark.asyncio
    async def test_recent_comments_capped_at_ten(self, workflow, lifecycle, mock_kanban):
        lifecycle.get_or_create("65", "kanboard")
        mock_kanban.get_task_by_id = AsyncMock(return_value=_make_task_mock())
        all_comments = [
            {"content": f"c{i}", "author": "alice", "date": i} for i in range(15)
        ]
        mock_kanban.get_comments = AsyncMock(return_value=all_comments)
        ctx = await workflow.get_work_context("65")
        assert ctx["recent_comments"] == all_comments[-10:]
        assert len(ctx["recent_comments"]) == 10


# ---------------------------------------------------------------------------
# get_project_description
# ---------------------------------------------------------------------------


class TestGetProjectDescription:
    """get_project_description resolves the ticket's project and returns
    its description document + parsed tech stack."""

    @pytest.mark.asyncio
    async def test_returns_none_when_ticket_has_no_project_id(
        self, workflow, mock_kanban
    ):
        mock_kanban.get_task_by_id = AsyncMock(return_value=None)
        result = await workflow.get_project_description("70")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_description_and_stack(self, workflow, mock_kanban):
        mock_kanban.get_task_by_id = AsyncMock(
            return_value=_make_task_mock()
        )
        from src.core.project_description import ProjectStack

        fake_stack = ProjectStack(
            language="python", framework="fastapi", install_cmd="pip install -r requirements.txt", dev_cmd="uvicorn main:app"
        )
        with patch(
            "src.core.project_description.ProjectDescriptionManager"
        ) as MockMgr:
            instance = MockMgr.return_value
            instance.get_description.return_value = "# My Project\n..."
            instance.get_stack.return_value = fake_stack
            result = await workflow.get_project_description("71")

        assert result == {
            "project_id": 9,
            "description": "# My Project\n...",
            "stack": {
                "language": "python",
                "framework": "fastapi",
                "install_cmd": "pip install -r requirements.txt",
                "dev_cmd": "uvicorn main:app",
            },
        }

    @pytest.mark.asyncio
    async def test_stack_is_none_when_unparseable(self, workflow, mock_kanban):
        mock_kanban.get_task_by_id = AsyncMock(return_value=_make_task_mock())
        with patch(
            "src.core.project_description.ProjectDescriptionManager"
        ) as MockMgr:
            instance = MockMgr.return_value
            instance.get_description.return_value = "empty doc"
            instance.get_stack.return_value = None
            result = await workflow.get_project_description("72")

        assert result["stack"] is None
        assert result["description"] == "empty doc"


# ---------------------------------------------------------------------------
# _is_unassigned helper
# ---------------------------------------------------------------------------


class TestIsUnassigned:
    """_is_unassigned returns True for None, empty string, and '0'."""

    def _make_record(self, assignee):
        """Build a minimal TicketRecord-like mock."""
        rec = MagicMock()
        rec.assignee = assignee
        return rec

    def test_none_assignee_is_unassigned(self, workflow):
        """assignee=None is treated as unassigned."""
        assert workflow._is_unassigned(self._make_record(None)) is True

    def test_empty_string_is_unassigned(self, workflow):
        """assignee='' is treated as unassigned."""
        assert workflow._is_unassigned(self._make_record("")) is True

    def test_kanboard_zero_is_unassigned(self, workflow):
        """Kanboard owner_id '0' sentinel is treated as unassigned."""
        assert workflow._is_unassigned(self._make_record("0")) is True

    def test_named_assignee_is_not_unassigned(self, workflow):
        """A real username is not treated as unassigned."""
        assert workflow._is_unassigned(self._make_record("alice")) is False


# ---------------------------------------------------------------------------
# One-ticket-per-agent constraint
# ---------------------------------------------------------------------------


class TestOneTicketPerAgent:
    """An agent cannot hold two claims simultaneously."""

    @pytest.mark.asyncio
    async def test_agent_skips_second_ticket_while_first_is_active(
        self, workflow, lifecycle, mock_kanban
    ):
        """If agent is already working on ticket A, it does not start on ticket B."""
        # Set up ticket A: agent already claims it.
        lifecycle.get_or_create("100", "kanboard")
        lifecycle.transition("100", "kanboard", TicketState.READY)
        lifecycle.claim_ticket("100", "kanboard", workflow._agent_id)
        lifecycle.transition("100", "kanboard", TicketState.IN_PROGRESS)

        # Ticket B is available.
        lifecycle.get_or_create("101", "kanboard")
        lifecycle.transition("101", "kanboard", TicketState.READY)
        lifecycle.set_assignee("101", "kanboard", "alice")

        rec_b = lifecycle.get("101", "kanboard")
        assert rec_b is not None
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/101",
        ):
            await workflow._start_ai_work("101", rec_b)

        # Ticket B must NOT be claimed — agent already busy with ticket A.
        rec_b2 = lifecycle.get("101", "kanboard")
        assert rec_b2 is not None
        assert rec_b2.ai_agent_id is None
        # Ticket A still held.
        assert lifecycle.get_agent_ticket(workflow._agent_id) == "100"

    @pytest.mark.asyncio
    async def test_agent_can_reclaim_its_own_current_ticket(
        self, workflow, lifecycle, mock_branch, mock_kanban
    ):
        """_start_ai_work is idempotent on the ticket the agent already holds."""
        lifecycle.get_or_create("102", "kanboard")
        lifecycle.transition("102", "kanboard", TicketState.READY)
        lifecycle.set_assignee("102", "kanboard", "bob")

        rec = lifecycle.get("102", "kanboard")
        assert rec is not None
        # Call twice — second call should not crash or double-create the branch.
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/102",
        ):
            await workflow._start_ai_work("102", rec)
            rec2 = lifecycle.get("102", "kanboard")
            assert rec2 is not None
            await workflow._start_ai_work("102", rec2)

        # Branch created once.
        assert mock_branch.create_branch.call_count == 1


# ---------------------------------------------------------------------------
# Auto-pickup: next ticket in dependency order
# ---------------------------------------------------------------------------


class TestPickupNextTicket:
    """When a ticket is paused/done, the agent picks the next available one."""

    def _setup_waiting_ticket(self, lifecycle, ticket_id: str, agent_id: str) -> None:
        """Put a ticket into WAITING_FOR_HUMAN with an agent claim."""
        lifecycle.get_or_create(ticket_id, "kanboard")
        lifecycle.transition(ticket_id, "kanboard", TicketState.READY)
        lifecycle.claim_ticket(ticket_id, "kanboard", agent_id)
        lifecycle.transition(ticket_id, "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition(ticket_id, "kanboard", TicketState.WAITING_FOR_HUMAN)

    @pytest.mark.asyncio
    async def test_pickup_after_signal_ready_for_review(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """After signal_ready_for_review, agent auto-picks next available ticket."""
        # Ticket A: agent is finishing it.
        self._setup_waiting_ticket(lifecycle, "110", workflow._agent_id)
        # Release the claim (signal_ready_for_review does this internally).
        lifecycle.release_ticket("110", "kanboard")

        # Ticket B: ready, assigned, unclaimed.
        lifecycle.get_or_create("111", "kanboard")
        lifecycle.transition("111", "kanboard", TicketState.READY)
        lifecycle.set_assignee("111", "kanboard", "alice")

        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/111",
        ):
            await workflow._pickup_next_ticket()

        rec_b = lifecycle.get("111", "kanboard")
        assert rec_b is not None
        assert rec_b.ai_agent_id == workflow._agent_id
        assert rec_b.state == TicketState.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_pickup_prefers_ready_over_in_progress(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """READY tickets are preferred over IN_PROGRESS when picking next."""
        # Ticket A (in_progress, unclaimed, assigned).
        lifecycle.get_or_create("120", "kanboard")
        lifecycle.transition("120", "kanboard", TicketState.READY)
        lifecycle.transition("120", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("120", "kanboard", "bob")

        # Ticket B (ready, unclaimed, assigned) — should be preferred.
        lifecycle.get_or_create("121", "kanboard")
        lifecycle.transition("121", "kanboard", TicketState.READY)
        lifecycle.set_assignee("121", "kanboard", "carol")

        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            await workflow._pickup_next_ticket()

        # Ticket B (READY) should have been picked, not ticket A (IN_PROGRESS).
        rec_b = lifecycle.get("121", "kanboard")
        assert rec_b is not None
        assert rec_b.ai_agent_id == workflow._agent_id

        rec_a = lifecycle.get("120", "kanboard")
        assert rec_a is not None
        assert rec_a.ai_agent_id is None

    @pytest.mark.asyncio
    async def test_pickup_prefers_lower_ticket_id(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """Within the same state, lower numeric ticket ID is picked first."""
        for tid in ("200", "100", "150"):
            lifecycle.get_or_create(tid, "kanboard")
            lifecycle.transition(tid, "kanboard", TicketState.READY)
            lifecycle.set_assignee(tid, "kanboard", "dave")

        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            await workflow._pickup_next_ticket()

        # Ticket 100 has the lowest ID → picked first.
        rec = lifecycle.get("100", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id == workflow._agent_id

    @pytest.mark.asyncio
    async def test_no_available_tickets_does_nothing(
        self, workflow, lifecycle, mock_kanban
    ):
        """_pickup_next_ticket does nothing when no tickets are available."""
        # All tickets are either todo, done, or unassigned.
        lifecycle.get_or_create("300", "kanboard")
        lifecycle.get_or_create("301", "kanboard")

        await workflow._pickup_next_ticket()

        # No claims taken.
        assert lifecycle.get_agent_ticket(workflow._agent_id) is None
        mock_kanban.move_task_to_column.assert_not_called()


# ---------------------------------------------------------------------------
# get_agent_ticket and get_available_tickets (lifecycle helpers)
# ---------------------------------------------------------------------------


class TestLifecycleAgentHelpers:
    """Tests for the new lifecycle manager helpers used by pickup logic."""

    def test_get_agent_ticket_returns_claimed_ticket(self, lifecycle):
        """get_agent_ticket returns the ticket held by the given agent."""
        lifecycle.get_or_create("400", "kanboard")
        lifecycle.claim_ticket("400", "kanboard", "agent-q")
        assert lifecycle.get_agent_ticket("agent-q") == "400"

    def test_get_agent_ticket_returns_none_when_unclaimed(self, lifecycle):
        """get_agent_ticket returns None when agent holds no claim."""
        assert lifecycle.get_agent_ticket("agent-z") is None

    def test_get_available_tickets_excludes_unassigned(self, lifecycle):
        """Unassigned tickets are not returned as available."""
        lifecycle.get_or_create("410", "kanboard")
        lifecycle.transition("410", "kanboard", TicketState.READY)
        # No assignee set.
        assert lifecycle.get_available_tickets() == []

    def test_get_available_tickets_excludes_claimed(self, lifecycle):
        """Tickets with an AI claim are not returned as available."""
        lifecycle.get_or_create("411", "kanboard")
        lifecycle.transition("411", "kanboard", TicketState.READY)
        lifecycle.set_assignee("411", "kanboard", "alice")
        lifecycle.claim_ticket("411", "kanboard", "agent-r")
        assert lifecycle.get_available_tickets() == []

    def test_get_available_tickets_excludes_todo_and_done(self, lifecycle):
        """Tickets in TODO and DONE are not available."""
        for tid in ("412", "413"):
            lifecycle.get_or_create(tid, "kanboard")
            lifecycle.set_assignee(tid, "kanboard", "bob")
        # 412 stays in TODO, 413 goes to DONE via full chain.
        lifecycle.transition("413", "kanboard", TicketState.READY)
        lifecycle.transition("413", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("413", "kanboard", TicketState.DONE)
        assert lifecycle.get_available_tickets() == []

    def test_get_available_tickets_returns_ready_and_in_progress(
        self, lifecycle
    ):
        """READY and IN_PROGRESS unclaimed assigned tickets are available."""
        lifecycle.get_or_create("420", "kanboard")
        lifecycle.transition("420", "kanboard", TicketState.READY)
        lifecycle.set_assignee("420", "kanboard", "carol")

        lifecycle.get_or_create("421", "kanboard")
        lifecycle.transition("421", "kanboard", TicketState.READY)
        lifecycle.transition("421", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("421", "kanboard", "dave")

        available = lifecycle.get_available_tickets()
        ids = {r.ticket_id for r in available}
        assert ids == {"420", "421"}
