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

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.events import Events
from src.core.models import TaskStatus
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
    kb.set_task_started_if_unset = AsyncMock(return_value=True)
    kb.set_merge_conflict_flag = AsyncMock(return_value=True)
    kb.get_task_color = AsyncMock(return_value=None)
    kb.get_task_links = AsyncMock(
        return_value={"depends_on": [], "blocks": [], "relates_to": []}
    )
    _STATUS_BY_COLUMN = {
        "done": TaskStatus.DONE,
        "waiting for human": TaskStatus.WAITING_FOR_HUMAN,
        "in progress": TaskStatus.IN_PROGRESS,
        "ready": TaskStatus.READY,
        "blocked": TaskStatus.BLOCKED,
    }
    kb.normalize_status = MagicMock(
        side_effect=lambda col: _STATUS_BY_COLUMN.get(
            (col or "").strip().lower(), TaskStatus.TODO
        )
    )
    return kb


@pytest.fixture
def mock_branch():
    """Mock BranchManager."""
    bm = MagicMock()
    bm.create_branch = AsyncMock(return_value=True)
    bm.merge_to_main = AsyncMock(return_value=True)
    bm.rebase_on_main = AsyncMock(return_value=True)
    bm.sync_branch = AsyncMock(return_value=True)
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
def mock_project_access():
    """Mock ProjectAccessSettingManager, permissive by default.

    The real manager defaults every unconfigured project to DISABLED (see
    TestProjectAccessGate below for the dedicated tests of that
    restriction) — but every OTHER test in this file predates the
    access-gate feature and exercises tickets whose "project" is whatever
    a given test wires up. Defaulting this mock to always-enabled keeps
    those tests exercising what they actually test, instead of all
    incidentally failing at the new gate.
    """
    pa = MagicMock()
    pa.is_enabled = MagicMock(return_value=True)
    return pa


@pytest.fixture
def workflow(
    lifecycle, mock_kanban, mock_branch, mock_dev_env, mock_ac_gen, mock_project_access
):
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
        project_access=mock_project_access,
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


class TestStartAiWorkResumesPriorWork:
    """A claimed ticket is not assumed brand new: if its branch ALREADY had
    commits (BranchManager.create_branch resumed it from the remote rather
    than cutting it fresh — see test_git_branch_manager.py), the "started"
    comment says so explicitly, so a human — and the next agent, which sees
    this comment via get_work_context's recent_comments — reviews the
    existing work and builds on it instead of assuming a greenfield ticket.
    """

    @pytest.mark.asyncio
    async def test_fresh_ticket_posts_plain_started_comment(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """No prior commits on the branch → the ordinary "Work Started"
        comment, no resume language."""
        mock_branch.get_branch_commits = AsyncMock(return_value=[])
        lifecycle.get_or_create("20", "kanboard")
        lifecycle.transition("20", "kanboard", TicketState.READY)

        event = _make_event(
            {"ticket_id": "20", "assignee": "alice", "provider": "kanboard"}
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/20",
        ):
            await workflow._on_ticket_assigned(event)

        body = mock_kanban.add_comment.call_args.args[1]
        assert "resum" not in body.lower()

    @pytest.mark.asyncio
    async def test_resumed_ticket_posts_resume_comment(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """The branch already had commits (BranchManager resumed it from the
        remote) → the comment names the commit count and tells the reader
        to review before continuing."""
        mock_branch.get_branch_commits = AsyncMock(
            return_value=["abc1234 add login form", "def5678 wire up auth"]
        )
        lifecycle.get_or_create("21", "kanboard")
        lifecycle.transition("21", "kanboard", TicketState.READY)

        event = _make_event(
            {"ticket_id": "21", "assignee": "alice", "provider": "kanboard"}
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/21",
        ):
            await workflow._on_ticket_assigned(event)

        body = mock_kanban.add_comment.call_args.args[1]
        low = body.lower()
        assert "resum" in low
        assert "2 commit" in low
        assert "abc1234 add login form" in body


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
        """Status → ready with NO human assigned → AI does not start work."""
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
    async def test_ready_without_assignee_syncs_lifecycle_state(
        self, workflow, lifecycle
    ):
        """Status → ready while unassigned still syncs the record to READY.

        Without this sync, the "move to Ready first, assign second" order
        never starts AI work: _on_ticket_assigned gates on
        ``record.state != TODO``, but nothing had ever advanced the record
        past TODO — the board column and the lifecycle record silently
        disagreed forever.
        """
        lifecycle.get_or_create("26", "kanboard")

        event = _make_event(
            {"ticket_id": "26", "new_status": "ready", "old_status": "todo",
             "provider": "kanboard"}
        )
        await workflow._on_status_changed(event)

        rec = lifecycle.get("26", "kanboard")
        assert rec is not None
        assert rec.state == TicketState.READY
        assert rec.ai_agent_id is None  # still not started — no assignee yet

    @pytest.mark.asyncio
    async def test_move_to_ready_then_assign_starts_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Human moves the card to Ready FIRST, assigns SECOND → AI starts.

        The mirror image of test_ready_with_assignee_starts_ai (assign
        first, move second) — both orderings must start work.
        """
        lifecycle.get_or_create("27", "kanboard")

        move_event = _make_event(
            {"ticket_id": "27", "new_status": "ready", "old_status": "todo",
             "provider": "kanboard"}
        )
        await workflow._on_status_changed(move_event)

        assign_event = _make_event(
            {"ticket_id": "27", "assignee": "alice", "provider": "kanboard"}
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/27",
        ):
            await workflow._on_ticket_assigned(assign_event)

        rec = lifecycle.get("27", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is not None
        assert rec.state == TicketState.IN_PROGRESS
        mock_kanban.move_task_to_column.assert_called_with("27", "in progress")

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
# BLOCKED auto-resume when the blocking ticket completes
# ---------------------------------------------------------------------------


class TestBlockedAutoResume:
    """Closing a blocker resumes tickets recorded as blocked on it.

    set_blocked() now stores the blocker structurally
    (record.blocked_by); when a ticket is closed and merged, BLOCKED
    tickets whose blocked_by references it resume automatically —
    previously the agent's signal_blocked was a one-way street and only
    a manual column drag could ever resume work.
    """

    def _block_on(self, workflow, lifecycle, tid, blocker):
        lifecycle.get_or_create(tid, "kanboard")
        lifecycle.transition(tid, "kanboard", TicketState.READY)
        lifecycle.transition(tid, "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee(tid, "kanboard", "alice")

    @pytest.mark.asyncio
    async def test_set_blocked_records_blocker(self, workflow, lifecycle):
        """set_blocked stores blocked_by on the lifecycle record."""
        self._block_on(workflow, lifecycle, "90", "89")
        await workflow.set_blocked("90", blocked_by="89")

        rec = lifecycle.get("90", "kanboard")
        assert rec.state == TicketState.BLOCKED
        assert rec.blocked_by == "89"

    @pytest.mark.asyncio
    async def test_closing_blocker_resumes_blocked_ticket(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """Blocker merged+closed → dependent ticket back to work, claimed."""
        # Blocker ticket 89, in progress and claimed.
        lifecycle.get_or_create("89", "kanboard")
        lifecycle.transition("89", "kanboard", TicketState.READY)
        lifecycle.transition("89", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("89", "kanboard", "alice")
        # Dependent ticket 90, blocked on 89.
        self._block_on(workflow, lifecycle, "90", "89")
        await workflow.set_blocked("90", blocked_by="ticket #89")

        close_event = _make_event({"ticket_id": "89", "provider": "kanboard"})
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/90",
        ):
            await workflow._on_ticket_closed(close_event)

        rec = lifecycle.get("90", "kanboard")
        assert rec.state == TicketState.IN_PROGRESS
        assert rec.ai_agent_id is not None
        assert rec.blocked_by is None  # cleared on leaving BLOCKED

    @pytest.mark.asyncio
    async def test_unrelated_blocker_stays_blocked(
        self, workflow, lifecycle, mock_kanban
    ):
        """A ticket blocked on something else is untouched."""
        lifecycle.get_or_create("89", "kanboard")
        lifecycle.transition("89", "kanboard", TicketState.READY)
        lifecycle.transition("89", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("89", "kanboard", "alice")
        self._block_on(workflow, lifecycle, "91", "77")
        await workflow.set_blocked("91", blocked_by="external API access #77")

        close_event = _make_event({"ticket_id": "89", "provider": "kanboard"})
        await workflow._on_ticket_closed(close_event)

        rec = lifecycle.get("91", "kanboard")
        assert rec.state == TicketState.BLOCKED
        assert rec.blocked_by == "external API access #77"


# ---------------------------------------------------------------------------
# Dead-end states: BLOCKED / WAITING_FOR_HUMAN re-entry into work
# ---------------------------------------------------------------------------


class TestDeadEndStateRecovery:
    """Re-entering work from BLOCKED or WAITING_FOR_HUMAN must fully work.

    _start_ai_work previously advanced the state machine only from
    TODO/READY: for a BLOCKED or WFH record it would claim the ticket and
    post "Started" while silently leaving the old state in place — and
    signal_ready_for_review cannot legally fire from BLOCKED or WFH, so
    the ticket became claimed, announced, and un-completable. BLOCKED was
    a full dead end: no code path ever executed the (permitted)
    BLOCKED → IN_PROGRESS transition.
    """

    def _blocked_ticket(self, workflow, lifecycle, tid):
        lifecycle.get_or_create(tid, "kanboard")
        lifecycle.transition(tid, "kanboard", TicketState.READY)
        lifecycle.transition(tid, "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition(tid, "kanboard", TicketState.BLOCKED)
        lifecycle.set_assignee(tid, "kanboard", "alice")
        return lifecycle.get(tid, "kanboard")

    @pytest.mark.asyncio
    async def test_unblock_via_column_move_reaches_in_progress(
        self, workflow, lifecycle, mock_kanban
    ):
        """Human drags a blocked card to 'in progress' → record follows."""
        rec = self._blocked_ticket(workflow, lifecycle, "70")

        event = _make_event(
            {"ticket_id": "70", "new_status": "in_progress",
             "old_status": "blocked", "provider": "kanboard"}
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/70",
        ):
            await workflow._on_status_changed(event)

        rec = lifecycle.get("70", "kanboard")
        assert rec.state == TicketState.IN_PROGRESS
        assert rec.ai_agent_id is not None

    @pytest.mark.asyncio
    async def test_wfh_unassign_reassign_reaches_in_progress(
        self, workflow, lifecycle, mock_kanban
    ):
        """WFH ticket unassigned then reassigned → work resumes completable."""
        lifecycle.get_or_create("71", "kanboard")
        lifecycle.transition("71", "kanboard", TicketState.READY)
        lifecycle.transition("71", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("71", "kanboard", TicketState.WAITING_FOR_HUMAN)

        unassign = _make_event({"ticket_id": "71", "provider": "kanboard"})
        await workflow._on_ticket_unassigned(unassign)

        assign = _make_event(
            {"ticket_id": "71", "assignee": "bob", "provider": "kanboard"}
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/71",
        ):
            await workflow._on_ticket_assigned(assign)

        rec = lifecycle.get("71", "kanboard")
        assert rec.state == TicketState.IN_PROGRESS
        assert rec.ai_agent_id is not None


# ---------------------------------------------------------------------------
# AC edited mid-work: keep working, don't silently flip to WFH
# ---------------------------------------------------------------------------


class TestAcChangedMidWork:
    """An AC edit while the agent works must not brick completion.

    The old behavior flipped IN_PROGRESS → WAITING_FOR_HUMAN while the
    posted comment said "I'll re-read them now and adjust" (i.e. AI
    continues) and the board column stayed 'in progress' — then
    signal_ready_for_review could never legally transition WFH → WFH and
    returned False forever.
    """

    @pytest.mark.asyncio
    async def test_in_progress_stays_in_progress(
        self, workflow, lifecycle, mock_kanban
    ):
        """AC edit during IN_PROGRESS keeps the state and notifies."""
        lifecycle.get_or_create("75", "kanboard")
        lifecycle.transition("75", "kanboard", TicketState.READY)
        lifecycle.transition("75", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.claim_ticket("75", "kanboard", workflow._agent_id)

        event = _make_event(
            {"ticket_id": "75", "new_ac_text": "- [ ] new AC",
             "new_hash": "abc", "provider": "kanboard"}
        )
        await workflow._on_ac_changed(event)

        rec = lifecycle.get("75", "kanboard")
        assert rec.state == TicketState.IN_PROGRESS
        assert rec.acceptance_criteria == "- [ ] new AC"
        mock_kanban.add_comment.assert_called_once()

    @pytest.mark.asyncio
    async def test_completion_still_possible_after_ac_edit(
        self, workflow, lifecycle, mock_kanban
    ):
        """The agent can still hand off for review after an AC edit."""
        lifecycle.get_or_create("76", "kanboard")
        lifecycle.transition("76", "kanboard", TicketState.READY)
        lifecycle.transition("76", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.claim_ticket("76", "kanboard", workflow._agent_id)

        event = _make_event(
            {"ticket_id": "76", "new_ac_text": "- [ ] new AC",
             "new_hash": "abc", "provider": "kanboard"}
        )
        await workflow._on_ac_changed(event)

        result = await workflow.signal_ready_for_review("76")

        assert result is True
        rec = lifecycle.get("76", "kanboard")
        assert rec.state == TicketState.WAITING_FOR_HUMAN


# ---------------------------------------------------------------------------
# Webhook/poll echo: WFH resume re-claims, no duplicate "Started"
# ---------------------------------------------------------------------------


class TestResumeReclaimAndEchoSuppression:
    """WFH resumes re-acquire the claim so poll echoes can't double-start.

    signal_ready_for_review releases the claim; the WFH → in-progress
    resume paths previously did NOT re-claim, so BoardWatcher's poll echo
    of the same column move (snapshots are only updated during polls)
    found an unclaimed IN_PROGRESS record and ran _start_ai_work — a
    fresh claim plus a duplicate, contradictory "Started" comment right
    after the "resuming" comment.
    """

    def _wfh_ticket(self, workflow, lifecycle, tid):
        lifecycle.get_or_create(tid, "kanboard")
        lifecycle.transition(tid, "kanboard", TicketState.READY)
        lifecycle.transition(tid, "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition(tid, "kanboard", TicketState.WAITING_FOR_HUMAN)
        lifecycle.set_assignee(tid, "kanboard", "alice")
        return lifecycle.get(tid, "kanboard")

    @pytest.mark.asyncio
    async def test_column_resume_reclaims(self, workflow, lifecycle):
        """WFH → in-progress column move re-acquires the AI claim."""
        self._wfh_ticket(workflow, lifecycle, "80")

        event = _make_event(
            {"ticket_id": "80", "new_status": "in_progress",
             "old_status": "waiting_for_human", "provider": "kanboard"}
        )
        await workflow._on_status_changed(event)

        rec = lifecycle.get("80", "kanboard")
        assert rec.state == TicketState.IN_PROGRESS
        assert rec.ai_agent_id == workflow._agent_id

    @pytest.mark.asyncio
    async def test_poll_echo_does_not_double_start(
        self, workflow, lifecycle, mock_kanban
    ):
        """The poll's echo of the same move must not claim or comment again."""
        self._wfh_ticket(workflow, lifecycle, "81")

        webhook_event = _make_event(
            {"ticket_id": "81", "new_status": "in_progress",
             "old_status": "waiting_for_human", "provider": "kanboard"}
        )
        await workflow._on_status_changed(webhook_event)
        comments_after_resume = mock_kanban.add_comment.call_count

        # BoardWatcher's next poll diffs the same column change again, but
        # by then the record is already IN_PROGRESS (not WFH).
        echo_event = _make_event(
            {"ticket_id": "81", "new_status": "in_progress",
             "old_status": "waiting_for_human", "provider": "kanboard"}
        )
        await workflow._on_status_changed(echo_event)

        assert mock_kanban.add_comment.call_count == comments_after_resume
        mock_kanban.move_task_to_column.assert_not_called()

    @pytest.mark.asyncio
    async def test_comment_resume_reclaims(self, workflow, lifecycle):
        """A human reply to a WFH ticket also re-acquires the claim."""
        self._wfh_ticket(workflow, lifecycle, "82")

        event = _make_event(
            {"ticket_id": "82", "comment_body": "please also add dark mode",
             "comment_author": "alice", "provider": "kanboard"}
        )
        await workflow._on_comment_added(event)

        rec = lifecycle.get("82", "kanboard")
        assert rec.state == TicketState.IN_PROGRESS
        assert rec.ai_agent_id == workflow._agent_id


# ---------------------------------------------------------------------------
# Review-signal ordering: no state change before the comment lands
# ---------------------------------------------------------------------------


class TestReviewSignalOrdering:
    """State must not advance until the human-facing signal is delivered.

    The old order transitioned to WAITING_FOR_HUMAN and released the AI
    claim BEFORE posting the review comment and moving the column. A brief
    Kanboard outage at that moment lost the human's only "please review"
    signal, and a retry was impossible forever: the record was already
    WAITING_FOR_HUMAN, so the transition raised InvalidTransitionError and
    the tool returned False on every subsequent call — a permanently
    stranded ticket.
    """

    def _in_progress_ticket(self, workflow, lifecycle, tid="60"):
        lifecycle.get_or_create(tid, "kanboard")
        lifecycle.transition(tid, "kanboard", TicketState.READY)
        lifecycle.transition(tid, "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee(tid, "kanboard", "alice")
        lifecycle.claim_ticket(tid, "kanboard", workflow._agent_id)
        return lifecycle.get(tid, "kanboard")

    @pytest.mark.asyncio
    async def test_failed_comment_leaves_state_recoverable(
        self, workflow, lifecycle, mock_kanban
    ):
        """Comment post fails → still IN_PROGRESS, still claimed, False."""
        self._in_progress_ticket(workflow, lifecycle)
        mock_kanban.add_comment = AsyncMock(side_effect=RuntimeError("kanboard down"))

        result = await workflow.signal_ready_for_review("60")

        assert result is False
        rec = lifecycle.get("60", "kanboard")
        assert rec.state == TicketState.IN_PROGRESS
        assert rec.ai_agent_id is not None
        mock_kanban.move_task_to_column.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_after_recovery_succeeds(
        self, workflow, lifecycle, mock_kanban
    ):
        """A retry once Kanboard is back completes the review handoff."""
        self._in_progress_ticket(workflow, lifecycle, tid="61")
        mock_kanban.add_comment = AsyncMock(side_effect=RuntimeError("down"))
        assert await workflow.signal_ready_for_review("61") is False

        mock_kanban.add_comment = AsyncMock(return_value=1)
        result = await workflow.signal_ready_for_review("61")

        assert result is True
        rec = lifecycle.get("61", "kanboard")
        assert rec.state == TicketState.WAITING_FOR_HUMAN
        assert rec.ai_agent_id is None
        mock_kanban.move_task_to_column.assert_called_with(
            "61", "waiting for human"
        )

    @pytest.mark.asyncio
    async def test_set_waiting_for_human_same_ordering(
        self, workflow, lifecycle, mock_kanban
    ):
        """set_waiting_for_human gets the same recoverability guarantee."""
        self._in_progress_ticket(workflow, lifecycle, tid="62")
        mock_kanban.add_comment = AsyncMock(side_effect=RuntimeError("down"))

        result = await workflow.set_waiting_for_human("62", "need input")

        assert result is False
        rec = lifecycle.get("62", "kanboard")
        assert rec.state == TicketState.IN_PROGRESS
        assert rec.ai_agent_id is not None

        mock_kanban.add_comment = AsyncMock(return_value=1)
        assert await workflow.set_waiting_for_human("62", "need input") is True
        rec = lifecycle.get("62", "kanboard")
        assert rec.state == TicketState.WAITING_FOR_HUMAN


# ---------------------------------------------------------------------------
# Claim-release gaps: todo reset and restart ghosts
# ---------------------------------------------------------------------------


class TestClaimReleaseGaps:
    """A held AI claim must be released whenever work legitimately stops.

    Two previously-missed paths: (1) a human dragging an in-flight card
    back to 'todo' reset the lifecycle state but left the claim held, so
    the one-ticket-per-agent gate skipped every future ticket forever;
    (2) after a restart, persisted claims belong to the dead process's
    UUID (the agent id is regenerated every start), and no event could
    ever release them — the first-sight recovery deliberately skips
    claimed records, so those tickets stayed 'in progress' indefinitely.
    """

    @pytest.mark.asyncio
    async def test_todo_reset_releases_claim(self, workflow, lifecycle):
        """Human moves an AI-claimed card back to todo → claim released."""
        lifecycle.get_or_create("50", "kanboard")
        lifecycle.transition("50", "kanboard", TicketState.READY)
        lifecycle.transition("50", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("50", "kanboard", "alice")
        lifecycle.claim_ticket("50", "kanboard", workflow._agent_id)

        event = _make_event(
            {"ticket_id": "50", "new_status": "todo",
             "old_status": "in_progress", "provider": "kanboard"}
        )
        await workflow._on_status_changed(event)

        rec = lifecycle.get("50", "kanboard")
        assert rec is not None
        assert rec.state == TicketState.TODO
        assert rec.ai_agent_id is None

    @pytest.mark.asyncio
    async def test_blocked_move_releases_claim(self, workflow, lifecycle):
        """Human drags an AI-claimed card to 'blocked' → claim released.

        Unlike the todo-reset path, nothing in the BLOCKED branch of
        _on_status_changed used to call release_ticket at all — the claim
        stayed held with no later event ever able to free it (the same
        one-ticket-per-agent deadlock risk as the todo-reset gap above).
        Covered generically now: a claim survives ONLY in In Progress,
        released on a move to anything else.
        """
        lifecycle.get_or_create("53", "kanboard")
        lifecycle.transition("53", "kanboard", TicketState.READY)
        lifecycle.transition("53", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("53", "kanboard", "alice")
        lifecycle.claim_ticket("53", "kanboard", workflow._agent_id)

        event = _make_event(
            {"ticket_id": "53", "new_status": "blocked",
             "old_status": "in_progress", "provider": "kanboard"}
        )
        await workflow._on_status_changed(event)

        rec = lifecycle.get("53", "kanboard")
        assert rec is not None
        assert rec.state == TicketState.BLOCKED
        assert rec.ai_agent_id is None

    @pytest.mark.asyncio
    async def test_ready_move_releases_a_claim_too(self, workflow, lifecycle):
        """Human drags an ACTIVE (claimed, IN_PROGRESS) card BACKWARD to
        'ready' → the claim is released and state mirrors down to READY.

        The generic "already claimed and in progress — ignore" branch
        does NOT cover this: it only fires when new_status matches the
        record's CURRENT state (a genuine poll-echo), and dragging to
        'ready' while internally still IN_PROGRESS is a real, distinct
        board change, not an echo. Without a dedicated branch for this,
        Marcus's internal state (and the golden-ring claim) would
        silently stay IN_PROGRESS forever while the board visibly showed
        Ready. This is "un-starting" the ticket: the claim releases, and
        _start_ai_work re-claims it from scratch the next time a worker
        is handed it."""
        lifecycle.get_or_create("54", "kanboard")
        lifecycle.transition("54", "kanboard", TicketState.READY)
        lifecycle.transition("54", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("54", "kanboard", "alice")
        lifecycle.claim_ticket("54", "kanboard", workflow._agent_id)

        event = _make_event(
            {"ticket_id": "54", "new_status": "ready",
             "old_status": "in_progress", "provider": "kanboard"}
        )
        await workflow._on_status_changed(event)

        rec = lifecycle.get("54", "kanboard")
        assert rec is not None
        assert rec.state == TicketState.READY
        assert rec.ai_agent_id is None

    @pytest.mark.asyncio
    async def test_todo_reset_unblocks_other_tickets(
        self, workflow, lifecycle, mock_kanban
    ):
        """After a todo reset, the agent can start work on another ticket."""
        lifecycle.get_or_create("51", "kanboard")
        lifecycle.transition("51", "kanboard", TicketState.READY)
        lifecycle.claim_ticket("51", "kanboard", workflow._agent_id)
        event = _make_event(
            {"ticket_id": "51", "new_status": "todo",
             "old_status": "ready", "provider": "kanboard"}
        )
        await workflow._on_status_changed(event)

        # A different assigned+ready ticket must now be startable.
        lifecycle.get_or_create("52", "kanboard")
        lifecycle.set_assignee("52", "kanboard", "bob")
        move = _make_event(
            {"ticket_id": "52", "new_status": "ready",
             "old_status": "todo", "provider": "kanboard"}
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/52",
        ):
            await workflow._on_status_changed(move)

        rec = lifecycle.get("52", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is not None

    @pytest.mark.asyncio
    async def test_start_releases_ghost_claims(
        self, workflow, lifecycle, mock_kanban
    ):
        """workflow.start() releases claims persisted by a dead process."""
        lifecycle.get_or_create("53", "kanboard")
        lifecycle.transition("53", "kanboard", TicketState.READY)
        lifecycle.transition("53", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.claim_ticket("53", "kanboard", "marcus-deadbeef")
        # The ticket still exists on the board — otherwise start()'s
        # reconcile correctly purges it as deleted (see
        # TestStartupReconcile) and there is no claim left to release.
        mock_kanban.get_task_by_id = AsyncMock(return_value=MagicMock())

        workflow._watcher.start = AsyncMock()
        await workflow.start()

        rec = lifecycle.get("53", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is None


# ---------------------------------------------------------------------------
# Per-project branch manager resolution
# ---------------------------------------------------------------------------


class TestPerProjectBranchManager:
    """Branch operations must target the ticket's project repo.

    A single default BranchManager binds to os.getcwd() — Marcus's own
    directory, never the project's clone under data/repos/<slug>. Branch
    create/merge/rebase/diff running there either all fail (CWD not a git
    repo) or, worse, "succeed" against the wrong repository — tickets get
    marked DONE and Merged while the agent's real commits are never merged.
    _branch_for_ticket resolves the ticket → project → local_repo_path
    mapping and returns a BranchManager bound to that path.
    """

    def _wire_project(self, workflow, mock_kanban, repo_path="/data/repos/app"):
        """Wire a project_sync mock + a kanban task that resolves project 3."""
        task = MagicMock()
        task.source_context = {"kanboard_task": {"project_id": 3}}
        mock_kanban.get_task_by_id = AsyncMock(return_value=task)
        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(
            return_value={
                "local_repo_path": repo_path,
                "gitea_repo_url": "http://gitea:3000/root/app.git",
            }
        )
        workflow._project_sync = project_sync

    @pytest.mark.asyncio
    async def test_falls_back_to_default_without_project_sync(
        self, workflow, mock_branch
    ):
        """No project sync wired → the constructor-supplied manager is used."""
        mgr = await workflow._branch_for_ticket("5")
        assert mgr is mock_branch

    @pytest.mark.asyncio
    async def test_resolves_manager_bound_to_project_repo(
        self, workflow, mock_kanban, mock_branch
    ):
        """With a repo mapping, the manager's repo_path is the project clone."""
        self._wire_project(workflow, mock_kanban)

        mgr = await workflow._branch_for_ticket("5")

        assert mgr is not mock_branch
        assert mgr.config.repo_path == "/data/repos/app"

    @pytest.mark.asyncio
    async def test_manager_is_cached_per_repo_path(
        self, workflow, mock_kanban
    ):
        """Two tickets in the same project share one BranchManager."""
        self._wire_project(workflow, mock_kanban)

        first = await workflow._branch_for_ticket("5")
        second = await workflow._branch_for_ticket("6")

        assert first is second

    @pytest.mark.asyncio
    async def test_start_ai_work_uses_project_branch_manager(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """_start_ai_work creates the branch in the PROJECT repo, not CWD."""
        self._wire_project(workflow, mock_kanban)
        per_project = MagicMock()
        per_project.create_branch = AsyncMock(return_value=True)
        per_project.config = MagicMock()
        per_project.config.main_branch = "main"
        workflow._branch_managers["/data/repos/app"] = per_project

        lifecycle.get_or_create("40", "kanboard")
        lifecycle.set_assignee("40", "kanboard", "alice")
        rec = lifecycle.get("40", "kanboard")

        # The tech-stack gate is not under test here (it consults a real
        # ProjectDescriptionManager once a project id resolves, which this
        # test's mock task makes possible for the first time in this suite).
        workflow._check_project_stack = AsyncMock(return_value=True)

        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/40",
        ):
            await workflow._start_ai_work("40", rec)

        per_project.create_branch.assert_called_once()
        mock_branch.create_branch.assert_not_called()


# ---------------------------------------------------------------------------
# Trigger: ticket seen for the first time already assigned + workable
# ---------------------------------------------------------------------------


class TestFirstSightRecovery:
    """A ticket first seen already assigned and in Ready must start AI work.

    BoardWatcher emits only ``ticket.new`` the first time it sees a ticket
    — including one that was assigned and moved to Ready while Marcus was
    down (or while now-fixed webhook bugs were dropping those events). The
    assignment and column state get absorbed into the watcher's baseline
    snapshot, so no ``ticket.assigned``/``ticket.status_changed`` diff ever
    fires afterwards. ``_on_ticket_new`` must therefore reconcile against
    the board state carried in the event itself, or such tickets stay
    unworked forever with no log trace.
    """

    @pytest.mark.asyncio
    async def test_new_ticket_assigned_and_ready_starts_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """First sight of an assigned ticket in Ready → AI claims and starts."""
        event = _make_event(
            {
                "ticket_id": "30",
                "provider": "kanboard",
                "task": {
                    "id": "30",
                    "title": "Stuck ticket",
                    "description": "something",
                    "status": "ready",
                    "assignee": "alice",
                },
            }
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/30",
        ):
            await workflow._on_ticket_new(event)

        rec = lifecycle.get("30", "kanboard")
        assert rec is not None
        assert rec.assignee == "alice"
        assert rec.ai_agent_id is not None
        assert rec.state == TicketState.IN_PROGRESS
        mock_kanban.move_task_to_column.assert_called_with("30", "in progress")

    @pytest.mark.asyncio
    async def test_new_ticket_assigned_but_todo_does_not_start_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """First sight of an assigned ticket still in todo → AI waits."""
        event = _make_event(
            {
                "ticket_id": "31",
                "provider": "kanboard",
                "task": {
                    "id": "31",
                    "title": "Fresh ticket",
                    "description": "",
                    "status": "todo",
                    "assignee": "bob",
                },
            }
        )
        await workflow._on_ticket_new(event)

        rec = lifecycle.get("31", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is None
        mock_kanban.move_task_to_column.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_ticket_assigned_but_todo_still_records_assignee(
        self, workflow, lifecycle, mock_kanban
    ):
        """First sight of an assigned-but-Todo ticket must still record the
        assignee, even though AI must not start work yet.

        BoardWatcher only emits ticket.assigned on a CHANGE relative to its
        own in-memory snapshot — and that snapshot's baseline already
        matches the board's assignee from the moment of first sight (this
        ticket was already assigned before Marcus ever polled it), so no
        LATER ticket.assigned event ever fires to backfill it. If first
        sight doesn't record the assignee here, when a human later moves
        the card to Ready — without touching assignment again, since it's
        already assigned — no event corrects the gap either: Marcus's own
        lifecycle record permanently shows unassigned even though the
        board has always shown a real owner. _is_unassigned/
        _is_human_owner (and therefore _next_worker_ticket's hand-out
        gate) read this persisted field, not a live Kanboard lookup, so
        this silently and indefinitely blocks the ticket from ever being
        handed to an agent."""
        event = _make_event(
            {
                "ticket_id": "34",
                "provider": "kanboard",
                "task": {
                    "id": "34",
                    "title": "Assigned but not yet ready",
                    "description": "",
                    "status": "todo",
                    "assignee": "dave",
                },
            }
        )
        await workflow._on_ticket_new(event)

        rec = lifecycle.get("34", "kanboard")
        assert rec is not None
        assert rec.assignee == "dave"
        # Still must not start AI work — that gate is unchanged.
        assert rec.ai_agent_id is None
        mock_kanban.move_task_to_column.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_ticket_ready_but_unassigned_does_not_start_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """First sight of an unassigned Ready ticket → AI does not start."""
        event = _make_event(
            {
                "ticket_id": "32",
                "provider": "kanboard",
                "task": {
                    "id": "32",
                    "title": "Unowned ticket",
                    "description": "",
                    "status": "ready",
                },
            }
        )
        await workflow._on_ticket_new(event)

        rec = lifecycle.get("32", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is None
        mock_kanban.move_task_to_column.assert_not_called()

    @pytest.mark.asyncio
    async def test_webhook_shaped_payload_without_status_is_harmless(
        self, workflow, lifecycle, mock_kanban
    ):
        """The Kanboard task.create webhook payload (no 'status'/'assignee'
        keys, raw Kanboard fields instead) must not trigger recovery."""
        event = _make_event(
            {
                "ticket_id": "33",
                "provider": "kanboard",
                "task": {
                    "id": 33,
                    "title": "Webhook ticket",
                    "description": "",
                    "owner_id": "5",
                    "column_title": "Todo",
                },
            }
        )
        await workflow._on_ticket_new(event)

        rec = lifecycle.get("33", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is None

    @pytest.mark.asyncio
    async def test_mirrors_state_when_start_is_refused_for_a_reason_unrelated_to_the_ticket(
        self, workflow, lifecycle, mock_kanban
    ):
        """If ``_start_ai_work`` refuses (e.g. every agent slot busy), the
        lifecycle record must still mirror the board's Ready/In-Progress
        state — not stay stuck at TODO.

        ``ticket.new`` fires only ONCE per ticket for the life of a
        BoardWatcher snapshot — a fresh process only ever sees a given
        ticket as "new" the very first time. A record left at TODO here is
        therefore invisible forever: ``_next_worker_ticket`` only considers
        READY/IN_PROGRESS records, and no later event re-examines a column
        that never moves again. This mirrors the same fix already applied
        in ``_on_status_changed`` for the equivalent "started refused" gap.
        """
        # Occupy the workflow's only slot (default max_parallel_agents=1).
        lifecycle.get_or_create("99", "kanboard")
        lifecycle.transition("99", "kanboard", TicketState.READY)
        lifecycle.set_assignee("99", "kanboard", "carol")
        await workflow._start_ai_work("99", lifecycle.get("99", "kanboard"))
        assert lifecycle.get("99", "kanboard").ai_agent_id is not None

        event = _make_event(
            {
                "ticket_id": "40",
                "provider": "kanboard",
                "task": {
                    "id": "40",
                    "title": "Stuck on restart",
                    "description": "",
                    "status": "ready",
                    "assignee": "alice",
                },
            }
        )
        await workflow._on_ticket_new(event)

        rec = lifecycle.get("40", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is None  # correctly refused — no free slot
        assert rec.state == TicketState.READY  # but NOT stuck at TODO


class TestStartupReconcile:
    """On startup Marcus re-reads every board and drops what is gone.

    BoardWatcher's disappeared-ticket check compares against snapshots it
    built during THIS process — and those start empty, so a ticket deleted
    while Marcus was down is never noticed by it. Its lifecycle record
    would otherwise outlive the ticket forever, and keep being handed to
    agents.
    """

    @pytest.mark.asyncio
    async def test_purges_tickets_deleted_while_marcus_was_down(
        self, workflow, lifecycle, mock_kanban
    ):
        """A tracked ticket that no longer exists is dropped at startup."""
        lifecycle.get_or_create("21", "kanboard")
        lifecycle.transition("21", "kanboard", TicketState.READY)
        lifecycle.set_assignee("21", "kanboard", "alice")
        mock_kanban.get_task_by_id = AsyncMock(return_value=None)

        await workflow._reconcile_deleted_tickets()

        assert lifecycle.get("21", "kanboard") is None

    @pytest.mark.asyncio
    async def test_keeps_tickets_that_still_exist(
        self, workflow, lifecycle, mock_kanban
    ):
        """A ticket still on the board — including one on a project that is
        merely DISABLED — is kept. Disabling a project must never delete
        its tickets from Marcus's view."""
        lifecycle.get_or_create("22", "kanboard")
        lifecycle.set_assignee("22", "kanboard", "alice")
        task = MagicMock()
        task.source_context = {"kanboard_task": {"project_id": 9}}
        mock_kanban.get_task_by_id = AsyncMock(return_value=task)

        await workflow._reconcile_deleted_tickets()

        assert lifecycle.get("22", "kanboard") is not None

    @pytest.mark.asyncio
    async def test_a_lookup_failure_never_purges(
        self, workflow, lifecycle, mock_kanban
    ):
        """Kanboard being unreachable at boot must not wipe every record."""
        lifecycle.get_or_create("23", "kanboard")
        mock_kanban.get_task_by_id = AsyncMock(
            side_effect=RuntimeError("kanboard unreachable")
        )

        await workflow._reconcile_deleted_tickets()

        assert lifecycle.get("23", "kanboard") is not None


class TestTicketDeleted:
    """A ticket deleted in Kanboard must stop being tracked by Marcus.

    Marcus hands out work from lifecycle records, not from the board, so a
    record that outlives its ticket is still selected — the agent is then
    told to work a ticket that no longer exists, and the slot it occupies
    is never freed.
    """

    @pytest.mark.asyncio
    async def test_deleted_ticket_is_purged(self, workflow, lifecycle):
        """The record is dropped so it can never be handed out again."""
        lifecycle.get_or_create("21", "kanboard")
        lifecycle.transition("21", "kanboard", TicketState.READY)
        lifecycle.set_assignee("21", "kanboard", "alice")

        await workflow._on_ticket_deleted(
            _make_event({"ticket_id": "21", "provider": "kanboard"})
        )

        assert lifecycle.get("21", "kanboard") is None

    @pytest.mark.asyncio
    async def test_deleting_a_claimed_ticket_frees_its_agent(
        self, workflow, lifecycle
    ):
        """An agent holding the deleted ticket is freed to take new work,
        rather than staying pinned to a ticket that no longer exists."""
        lifecycle.get_or_create("21", "kanboard")
        lifecycle.transition("21", "kanboard", TicketState.READY)
        lifecycle.set_assignee("21", "kanboard", "alice")
        lifecycle.claim_ticket("21", "kanboard", "worker-1")

        await workflow._on_ticket_deleted(
            _make_event({"ticket_id": "21", "provider": "kanboard"})
        )

        assert lifecycle.get_agent_ticket("worker-1") is None

    @pytest.mark.asyncio
    async def test_deleted_ticket_stops_its_dev_environment(
        self, workflow, lifecycle, mock_dev_env
    ):
        """Its preview container is torn down — nothing will ever stop it
        later, since every other cleanup path keys off the ticket."""
        lifecycle.get_or_create("21", "kanboard")

        await workflow._on_ticket_deleted(
            _make_event({"ticket_id": "21", "provider": "kanboard"})
        )

        mock_dev_env.stop.assert_awaited()

    @pytest.mark.asyncio
    async def test_deleting_an_untracked_ticket_is_harmless(
        self, workflow, lifecycle
    ):
        """The same deletion can arrive from both a webhook and a poll."""
        await workflow._on_ticket_deleted(
            _make_event({"ticket_id": "999", "provider": "kanboard"})
        )

    @pytest.mark.asyncio
    async def test_deleted_ticket_is_not_handed_to_an_agent(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """End to end: the exact reported symptom — an agent being assigned
        a ticket the human had already deleted."""
        mock_project_access.is_enabled = MagicMock(return_value=True)
        task = MagicMock()
        task.source_context = {"kanboard_task": {"project_id": 1}}
        mock_kanban.get_task_by_id = AsyncMock(return_value=task)
        lifecycle.get_or_create("21", "kanboard")
        lifecycle.transition("21", "kanboard", TicketState.READY)
        lifecycle.set_assignee("21", "kanboard", "alice")

        await workflow._on_ticket_deleted(
            _make_event({"ticket_id": "21", "provider": "kanboard"})
        )
        result = await workflow.orchestrate_work(agent_id="worker-1")

        assert result["status"] == "no_work"


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


class TestAgentGitUrls:
    """_agent_git_urls rehosts + credentials the URLs handed to agents."""

    def _wire_gitea(self, workflow, username="root", token="adminTok"):
        gitea = MagicMock()
        gitea._username = username
        gitea._token = token
        ps = MagicMock()
        ps._gitea = gitea
        workflow._project_sync = ps

    def test_embeds_admin_token_and_rehosts_by_default(
        self, workflow, monkeypatch
    ):
        """Default: browser host + admin creds embedded in clone_url."""
        monkeypatch.delenv("GITEA_AGENT_TOKEN", raising=False)
        monkeypatch.delenv("GITEA_PUBLIC_URL", raising=False)
        monkeypatch.delenv("MARCUS_EMBED_GIT_CREDENTIALS", raising=False)
        self._wire_gitea(workflow)

        urls = workflow._agent_git_urls(
            "http://gitea:3000/root/app.git", "ticket/kanboard/5"
        )
        assert urls["clone_url"] == "http://root:adminTok@localhost:3000/root/app.git"
        assert urls["repo_web_url"] == "http://localhost:3000/root/app"
        assert (
            urls["branch_web_url"]
            == "http://localhost:3000/root/app/src/branch/ticket/kanboard/5"
        )

    def test_dedicated_agent_token_takes_precedence(self, workflow, monkeypatch):
        """GITEA_AGENT_TOKEN/USERNAME override the admin token."""
        monkeypatch.setenv("GITEA_AGENT_TOKEN", "scopedTok")
        monkeypatch.setenv("GITEA_AGENT_USERNAME", "marcus-agent")
        monkeypatch.delenv("GITEA_PUBLIC_URL", raising=False)
        monkeypatch.delenv("MARCUS_EMBED_GIT_CREDENTIALS", raising=False)
        self._wire_gitea(workflow)

        urls = workflow._agent_git_urls(
            "http://gitea:3000/root/app.git", "ticket/kanboard/5"
        )
        assert (
            urls["clone_url"]
            == "http://marcus-agent:scopedTok@localhost:3000/root/app.git"
        )

    def test_embed_disabled_returns_plain_clone_url(self, workflow, monkeypatch):
        """MARCUS_EMBED_GIT_CREDENTIALS=false → no creds in clone_url."""
        monkeypatch.setenv("MARCUS_EMBED_GIT_CREDENTIALS", "false")
        monkeypatch.setenv("GITEA_PUBLIC_URL", "https://git.example.com")
        self._wire_gitea(workflow)

        urls = workflow._agent_git_urls(
            "http://gitea:3000/root/app.git", "ticket/kanboard/5"
        )
        assert urls["clone_url"] == "https://git.example.com/root/app.git"
        assert "@" not in urls["clone_url"]

    @pytest.mark.asyncio
    async def test_get_work_context_includes_clone_and_branch_urls(
        self, workflow, lifecycle, mock_kanban, monkeypatch
    ):
        """get_work_context surfaces clone_url + branch_web_url from the mapping."""
        monkeypatch.delenv("GITEA_PUBLIC_URL", raising=False)
        monkeypatch.delenv("MARCUS_EMBED_GIT_CREDENTIALS", raising=False)
        lifecycle.get_or_create("60", "kanboard", branch_name="ticket/kanboard/60")

        task = MagicMock()
        task.name = "Build it"
        task.description = ""
        task.source_context = {"kanboard_task": {"project_id": 3}}
        task.labels = []
        mock_kanban.get_task_by_id = AsyncMock(return_value=task)

        gitea = MagicMock()
        gitea._username = "root"
        gitea._token = "adminTok"
        ps = MagicMock()
        ps._gitea = gitea
        ps.get_repo_for_project = MagicMock(
            return_value={
                "local_repo_path": "/data/repos/app",
                "gitea_repo_url": "http://gitea:3000/root/app.git",
            }
        )
        workflow._project_sync = ps

        ctx = await workflow.get_work_context("60")
        assert ctx is not None
        assert ctx["clone_url"] == "http://root:adminTok@localhost:3000/root/app.git"
        assert (
            ctx["branch_web_url"]
            == "http://localhost:3000/root/app/src/branch/ticket/kanboard/60"
        )
        assert ctx["repo_web_url"] == "http://localhost:3000/root/app"
        # Instructions tell the agent to clone, not reuse Marcus's path.
        assert "git clone" in ctx["instructions"]


class TestRepoLinksForKanboardUI:
    """get_repo_links / get_project_repo_url feed the Kanboard UI links."""

    def _wire(self, workflow, mock_kanban, mapping=True):
        task = MagicMock()
        task.source_context = {"kanboard_task": {"project_id": 3}}
        mock_kanban.get_task_by_id = AsyncMock(return_value=task)
        gitea = MagicMock()
        gitea._username = "root"
        gitea._token = "adminTok"
        ps = MagicMock()
        ps._gitea = gitea
        ps.get_repo_for_project = MagicMock(
            return_value=(
                {"gitea_repo_url": "http://gitea:3000/root/app.git"}
                if mapping
                else None
            )
        )
        workflow._project_sync = ps

    @pytest.mark.asyncio
    async def test_get_repo_links_returns_credential_free_urls(
        self, workflow, lifecycle, mock_kanban, monkeypatch
    ):
        """Repo + branch links are browser-facing and carry NO credentials."""
        monkeypatch.delenv("GITEA_PUBLIC_URL", raising=False)
        lifecycle.get_or_create("70", "kanboard", branch_name="ticket/kanboard/70")
        self._wire(workflow, mock_kanban)

        links = await workflow.get_repo_links("70")
        assert links == {
            "repo_web_url": "http://localhost:3000/root/app",
            "branch_web_url": "http://localhost:3000/root/app/src/branch/ticket/kanboard/70",
        }
        assert "@" not in links["branch_web_url"]  # no embedded creds

    @pytest.mark.asyncio
    async def test_get_repo_links_none_when_repo_not_provisioned(
        self, workflow, mock_kanban
    ):
        """No mapping yet → None (and no provisioning side effect)."""
        self._wire(workflow, mock_kanban, mapping=False)
        assert await workflow.get_repo_links("71") is None
        # Non-provisioning: read-only lookup, ensure_repo never called.
        workflow._project_sync.get_repo_for_project.assert_called()

    def test_get_project_repo_url(self, workflow, mock_kanban, monkeypatch):
        """Project repo URL is the browser repo link, or None if unprovisioned."""
        monkeypatch.delenv("GITEA_PUBLIC_URL", raising=False)
        self._wire(workflow, mock_kanban)
        assert workflow.get_project_repo_url(3) == "http://localhost:3000/root/app"

        self._wire(workflow, mock_kanban, mapping=False)
        assert workflow.get_project_repo_url(3) is None


# ---------------------------------------------------------------------------
# get_work_context: enriched ticket data (labels/links/recent_comments)
# ---------------------------------------------------------------------------


def _make_task_mock(labels=None):
    """Build a minimal Task-like mock carrying only the fields
    get_work_context reads for the enrichment fields."""
    task = MagicMock()
    task.name = "Enriched ticket"
    task.description = "desc"
    task.source_context = {"kanboard_task": {"project_id": 9}}
    task.labels = labels or []
    return task


class TestGetWorkContextEnrichedFields:
    """get_work_context surfaces labels (parsed onto the Task by the
    provider) and links/comments (fetched via optional provider methods).

    Priority, due date, and estimated hours are deliberately NOT surfaced —
    they don't help an agent do the work."""

    @pytest.mark.asyncio
    async def test_labels_surfaced(self, workflow, lifecycle, mock_kanban):
        lifecycle.get_or_create("60", "kanboard")
        mock_kanban.get_task_by_id = AsyncMock(
            return_value=_make_task_mock(labels=["backend", "urgent"])
        )
        ctx = await workflow.get_work_context("60")
        assert ctx["labels"] == ["backend", "urgent"]

    @pytest.mark.asyncio
    async def test_priority_due_date_estimated_hours_not_returned(
        self, workflow, lifecycle, mock_kanban
    ):
        """Removed context fields must not appear in the response at all."""
        lifecycle.get_or_create("62", "kanboard")
        mock_kanban.get_task_by_id = AsyncMock(return_value=_make_task_mock())
        ctx = await workflow.get_work_context("62")
        assert "priority" not in ctx
        assert "due_date" not in ctx
        assert "estimated_hours" not in ctx
        assert ctx["labels"] == []

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
# get_work_context: on-demand Gitea repo provisioning via ProjectSyncWorkflow
# ---------------------------------------------------------------------------


class TestGetWorkContextEnsuresRepo:
    """get_work_context() provisions the Gitea repo on first lookup.

    Nothing in Marcus currently publishes a `project.created` event, so
    ProjectSyncWorkflow.ensure_repo() is only ever reached this way — a
    ticket's project must get a repo mapping the first time an agent asks
    for work context, not stay permanently unset.
    """

    @pytest.fixture
    def mock_project_sync(self):
        ps = MagicMock()
        ps.get_repo_for_project = MagicMock(return_value=None)
        ps.ensure_repo = AsyncMock(
            return_value={
                "local_repo_path": "./data/repos/shopping-cart",
                "gitea_repo_url": "http://localhost:3000/root/shopping-cart.git",
            }
        )
        return ps

    @pytest.fixture
    def workflow_with_sync(
        self, lifecycle, mock_kanban, mock_branch, mock_dev_env, mock_ac_gen,
        mock_project_sync,
    ):
        events = Events()
        wf = HumanGatedWorkflow(
            kanban=mock_kanban,
            events=events,
            provider_name="kanboard",
            lifecycle=lifecycle,
            branch_manager=mock_branch,
            dev_env_manager=mock_dev_env,
            ac_generator=mock_ac_gen,
            project_sync=mock_project_sync,
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            yield wf

    @pytest.mark.asyncio
    async def test_ensure_repo_called_when_no_mapping_exists(
        self, workflow_with_sync, lifecycle, mock_kanban, mock_project_sync
    ):
        """No cached mapping + a resolvable project name → ensure_repo runs."""
        lifecycle.get_or_create("70", "kanboard")
        mock_kanban.get_task_by_id = AsyncMock(return_value=_make_task_mock())
        mock_kanban.get_project_name = AsyncMock(return_value="Shopping Cart")

        ctx = await workflow_with_sync.get_work_context("70")

        mock_project_sync.ensure_repo.assert_awaited_once_with(9, "Shopping Cart")
        assert ctx["local_repo_path"] == "./data/repos/shopping-cart"
        assert ctx["gitea_repo_url"] == "http://localhost:3000/root/shopping-cart.git"

    @pytest.mark.asyncio
    async def test_ensure_repo_skipped_when_mapping_already_cached(
        self, workflow_with_sync, lifecycle, mock_kanban, mock_project_sync
    ):
        """A cached mapping short-circuits — no repo-creation call at all."""
        mock_project_sync.get_repo_for_project = MagicMock(
            return_value={
                "local_repo_path": "./data/repos/cached",
                "gitea_repo_url": "http://localhost:3000/root/cached.git",
            }
        )
        lifecycle.get_or_create("71", "kanboard")
        mock_kanban.get_task_by_id = AsyncMock(return_value=_make_task_mock())

        ctx = await workflow_with_sync.get_work_context("71")

        mock_project_sync.ensure_repo.assert_not_called()
        assert ctx["local_repo_path"] == "./data/repos/cached"

    @pytest.mark.asyncio
    async def test_skipped_when_kanban_has_no_get_project_name(
        self, workflow_with_sync, lifecycle, mock_project_sync
    ):
        """Provider without get_project_name (non-Kanboard) → no crash, no call."""
        lifecycle.get_or_create("72", "kanboard")
        limited_kanban = MagicMock(spec=["get_task_by_id"])
        limited_kanban.get_task_by_id = AsyncMock(return_value=_make_task_mock())
        workflow_with_sync._kanban = limited_kanban

        ctx = await workflow_with_sync.get_work_context("72")

        mock_project_sync.ensure_repo.assert_not_called()
        assert ctx["local_repo_path"] is None

    @pytest.mark.asyncio
    async def test_ensure_repo_failure_does_not_crash_get_work_context(
        self, workflow_with_sync, lifecycle, mock_kanban, mock_project_sync
    ):
        """A repo-provisioning failure degrades to no repo info, not an error."""
        lifecycle.get_or_create("73", "kanboard")
        mock_kanban.get_task_by_id = AsyncMock(return_value=_make_task_mock())
        mock_kanban.get_project_name = AsyncMock(return_value="Shopping Cart")
        mock_project_sync.ensure_repo = AsyncMock(return_value=None)

        ctx = await workflow_with_sync.get_work_context("73")

        assert ctx["local_repo_path"] is None
        assert ctx["gitea_repo_url"] is None

    @pytest.mark.asyncio
    async def test_no_project_sync_wired_leaves_repo_fields_none(
        self, workflow, lifecycle, mock_kanban
    ):
        """workflow fixture has no project_sync — unchanged pre-existing behaviour."""
        lifecycle.get_or_create("74", "kanboard")
        mock_kanban.get_task_by_id = AsyncMock(return_value=_make_task_mock())
        ctx = await workflow.get_work_context("74")
        assert ctx["local_repo_path"] is None
        assert ctx["gitea_repo_url"] is None


# ---------------------------------------------------------------------------
# start_dev_environment: resolves the ticket's real per-project repo path,
# same as get_work_context — this is the AI-agent-facing MCP tool path,
# separate from (and previously missed by) the HTTP /dev-env/view button.
# ---------------------------------------------------------------------------


class TestStartDevEnvironmentResolvesRepoPath:
    @pytest.fixture
    def mock_project_sync(self):
        ps = MagicMock()
        ps.get_repo_for_project = MagicMock(return_value=None)
        ps.ensure_repo = AsyncMock(
            return_value={
                "local_repo_path": "./data/repos/shopping-cart",
                "gitea_repo_url": "http://localhost:3000/root/shopping-cart.git",
            }
        )
        return ps

    @pytest.fixture
    def workflow_with_sync(
        self, lifecycle, mock_kanban, mock_branch, mock_dev_env, mock_ac_gen,
        mock_project_sync,
    ):
        events = Events()
        wf = HumanGatedWorkflow(
            kanban=mock_kanban,
            events=events,
            provider_name="kanboard",
            lifecycle=lifecycle,
            branch_manager=mock_branch,
            dev_env_manager=mock_dev_env,
            ac_generator=mock_ac_gen,
            project_sync=mock_project_sync,
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            yield wf

    @pytest.mark.asyncio
    async def test_returns_none_when_ticket_untracked(self, workflow):
        assert await workflow.start_dev_environment("999") is None

    @pytest.mark.asyncio
    async def test_passes_resolved_repo_path_to_dev_env_start(
        self, workflow_with_sync, lifecycle, mock_kanban, mock_dev_env, mock_project_sync
    ):
        lifecycle.get_or_create("80", "kanboard")
        mock_kanban.get_task_by_id = AsyncMock(return_value=_make_task_mock())
        mock_kanban.get_project_name = AsyncMock(return_value="Shopping Cart")

        await workflow_with_sync.start_dev_environment("80")

        mock_project_sync.ensure_repo.assert_awaited_once()
        call_kwargs = mock_dev_env.start.call_args.kwargs
        assert call_kwargs["repo_path"] == "./data/repos/shopping-cart"
        assert call_kwargs["ticket_id"] == "80"

    @pytest.mark.asyncio
    async def test_syncs_remote_branch_before_starting_preview(
        self, workflow, lifecycle, mock_kanban, mock_branch, mock_dev_env
    ):
        """Marcus fetches the agent's pushed commits into its local clone
        before the preview container is built, so the preview reflects the
        REMOTE branch's committed work — not a stale local copy. (No project
        sync here → _branch_for_repo_path(None) returns the injected manager.)"""
        rec = lifecycle.get_or_create("83", "kanboard")
        rec.branch_name = "ticket/kanboard/83"
        mock_kanban.get_task_by_id = AsyncMock(return_value=_make_task_mock())

        await workflow.start_dev_environment("83")

        mock_branch.sync_branch.assert_awaited_once_with("ticket/kanboard/83")

    @pytest.mark.asyncio
    async def test_no_project_sync_passes_none_repo_path(
        self, workflow, lifecycle, mock_kanban, mock_dev_env
    ):
        """workflow fixture has no project_sync — repo_path stays None,
        matching the pre-existing behaviour (DevEnvironmentManager falls
        back to its own configured default)."""
        lifecycle.get_or_create("81", "kanboard")
        mock_kanban.get_task_by_id = AsyncMock(return_value=_make_task_mock())

        await workflow.start_dev_environment("81")

        call_kwargs = mock_dev_env.start.call_args.kwargs
        assert call_kwargs["repo_path"] is None

    @pytest.mark.asyncio
    async def test_kanban_task_lookup_failure_does_not_crash(
        self, workflow_with_sync, lifecycle, mock_kanban, mock_dev_env
    ):
        lifecycle.get_or_create("82", "kanboard")
        mock_kanban.get_task_by_id = AsyncMock(side_effect=RuntimeError("kanban down"))

        url = await workflow_with_sync.start_dev_environment("82")

        assert url is not None  # dev env still starts, just without repo_path
        call_kwargs = mock_dev_env.start.call_args.kwargs
        assert call_kwargs["repo_path"] is None

    @pytest.mark.asyncio
    async def test_dev_env_start_failure_returns_none(
        self, workflow, lifecycle, mock_kanban, mock_dev_env
    ):
        lifecycle.get_or_create("83", "kanboard")
        mock_kanban.get_task_by_id = AsyncMock(return_value=_make_task_mock())
        mock_dev_env.start = AsyncMock(side_effect=RuntimeError("docker unreachable"))

        assert await workflow.start_dev_environment("83") is None

    @pytest.mark.asyncio
    async def test_posts_comment_and_returns_url_on_success(
        self, workflow, lifecycle, mock_kanban, mock_dev_env
    ):
        from types import SimpleNamespace

        lifecycle.get_or_create("84", "kanboard")
        mock_kanban.get_task_by_id = AsyncMock(return_value=_make_task_mock())
        mock_dev_env.start = AsyncMock(
            return_value=SimpleNamespace(port=9100, url="http://localhost:9100")
        )

        url = await workflow.start_dev_environment("84")

        assert url == "http://localhost:9100"
        mock_kanban.add_comment.assert_awaited_once()


# ---------------------------------------------------------------------------
# sync_main_branch_for_project: pulls a project's main branch into Marcus's
# local clone, for the project-level "main branch preview" feature — the
# project_id-keyed analog of _sync_branch_for_ticket, used when there's no
# ticket to resolve a project from (the caller already has the project_id).
# ---------------------------------------------------------------------------


class TestSyncMainBranchForProject:
    @pytest.mark.asyncio
    async def test_no_project_sync_uses_injected_branch_manager(
        self, workflow, mock_branch
    ):
        """No project_sync configured on the base `workflow` fixture →
        _branch_for_repo_path(None) returns the injected mock_branch (same
        as _sync_branch_for_ticket's equivalent no-project-sync case), and
        the sync still runs against it."""
        repo_path = await workflow.sync_main_branch_for_project(7)

        mock_branch.sync_branch.assert_awaited_once_with(
            mock_branch.config.main_branch
        )
        assert repo_path is None

    @pytest.mark.asyncio
    async def test_syncs_via_resolved_project_mapping(self, workflow):
        """A resolvable project mapping's repo_path is used to build a
        per-repo BranchManager (a real one, distinct from the injected
        default — same as _branch_for_repo_path's own behavior for any
        truthy repo_path), and the resolved path is returned to the caller
        so it doesn't have to redo the project->repo lookup."""
        workflow._project_sync = MagicMock()
        workflow._project_sync.get_repo_for_project = MagicMock(
            return_value={"local_repo_path": "./data/repos/shopping-cart"}
        )

        with patch(
            "src.workflows.human_gated_workflow.BranchManager.sync_branch",
            new_callable=AsyncMock,
        ) as sync_branch:
            repo_path = await workflow.sync_main_branch_for_project(7)

        assert repo_path == "./data/repos/shopping-cart"
        sync_branch.assert_awaited_once_with("main")

    @pytest.mark.asyncio
    async def test_sync_branch_exception_is_swallowed(self, workflow, mock_branch):
        """A failed sync must not raise — a stale preview is better than a
        crashed route."""
        mock_branch.sync_branch = AsyncMock(side_effect=RuntimeError("git fetch failed"))

        repo_path = await workflow.sync_main_branch_for_project(7)

        assert repo_path is None  # base workflow fixture has no configured repo_path


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


# ---------------------------------------------------------------------------
# Multi-agent parallelism (max_parallel_agents > 1)
# ---------------------------------------------------------------------------


class TestMultiAgentParallelism:
    """The human-gated workflow can run up to N tickets in parallel.

    ``N = max_parallel_agents``.  Each concurrently in-progress ticket is
    held by a distinct AI *slot*; the first slot's id is
    ``workflow._agent_id`` (kept for back-compat with the single-agent
    callers).  A slot frees ONLY when its ticket naturally releases
    (waiting-for-human / blocked / done) — a busy slot is never preempted,
    so in-flight work and its saved ticket context are never lost.

    Every external dependency is mocked; no I/O or network occurs.
    """

    @pytest.fixture
    def make_workflow(
        self, lifecycle, mock_kanban, mock_branch, mock_dev_env, mock_ac_gen
    ):
        """Factory building a workflow with a chosen parallel-agent count."""

        def _factory(n: int) -> HumanGatedWorkflow:
            return HumanGatedWorkflow(
                kanban=mock_kanban,
                events=Events(),
                provider_name="kanboard",
                lifecycle=lifecycle,
                branch_manager=mock_branch,
                dev_env_manager=mock_dev_env,
                ac_generator=mock_ac_gen,
                max_parallel_agents=n,
            )

        return _factory

    def _ready_assigned(self, lifecycle, tid: str, who: str = "alice") -> Any:
        """Create a READY, human-assigned, unclaimed ticket and return it."""
        lifecycle.get_or_create(tid, "kanboard")
        lifecycle.transition(tid, "kanboard", TicketState.READY)
        lifecycle.set_assignee(tid, "kanboard", who)
        return lifecycle.get(tid, "kanboard")

    @pytest.mark.asyncio
    async def test_two_tickets_run_in_parallel(
        self, make_workflow, lifecycle, mock_kanban
    ):
        """With N=2, two assigned tickets are both claimed and started."""
        wf = make_workflow(2)
        rec_a = self._ready_assigned(lifecycle, "10")
        rec_b = self._ready_assigned(lifecycle, "11")

        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            await wf._start_ai_work("10", rec_a)
            await wf._start_ai_work("11", rec_b)

        a = lifecycle.get("10", "kanboard")
        b = lifecycle.get("11", "kanboard")
        assert a.ai_agent_id is not None
        assert b.ai_agent_id is not None
        # Two parallel tickets are held by two DIFFERENT slots.
        assert a.ai_agent_id != b.ai_agent_id
        assert a.state == TicketState.IN_PROGRESS
        assert b.state == TicketState.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_first_slot_is_agent_id(
        self, make_workflow, lifecycle, mock_kanban
    ):
        """The first claimed slot equals workflow._agent_id (back-compat)."""
        wf = make_workflow(3)
        rec_a = self._ready_assigned(lifecycle, "10")

        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            await wf._start_ai_work("10", rec_a)

        assert lifecycle.get("10", "kanboard").ai_agent_id == wf._agent_id

    @pytest.mark.asyncio
    async def test_capacity_cap_refuses_extra_ticket(
        self, make_workflow, lifecycle, mock_kanban
    ):
        """With N=2 and both slots busy, a third ticket is not claimed."""
        wf = make_workflow(2)
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            for tid in ("10", "11"):
                await wf._start_ai_work(tid, self._ready_assigned(lifecycle, tid))
            rec_c = self._ready_assigned(lifecycle, "12")
            await wf._start_ai_work("12", rec_c)

        # Third ticket waits — no free slot.
        assert lifecycle.get("12", "kanboard").ai_agent_id is None
        assert lifecycle.get("12", "kanboard").state == TicketState.READY

    @pytest.mark.asyncio
    async def test_pickup_fills_all_free_slots(
        self, make_workflow, lifecycle, mock_kanban
    ):
        """_pickup_next_ticket claims up to N available tickets at once."""
        wf = make_workflow(3)
        for tid in ("10", "11", "12", "13"):
            self._ready_assigned(lifecycle, tid)

        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            await wf._pickup_next_ticket()

        claimed = [
            tid
            for tid in ("10", "11", "12", "13")
            if lifecycle.get(tid, "kanboard").ai_agent_id is not None
        ]
        # Exactly N=3 claimed; the 4th waits for a free slot.
        assert len(claimed) == 3
        assert lifecycle.get("13", "kanboard").ai_agent_id is None

    @pytest.mark.asyncio
    async def test_freed_slot_reused_without_preempting_the_other(
        self, make_workflow, lifecycle, mock_kanban
    ):
        """Completing one ticket frees its slot for a waiting ticket.

        The OTHER in-flight ticket must never be preempted — its claim and
        slot stay exactly as they were.
        """
        wf = make_workflow(2)
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            # Two tickets running in parallel; a third waiting.
            for tid in ("10", "11"):
                await wf._start_ai_work(tid, self._ready_assigned(lifecycle, tid))
            self._ready_assigned(lifecycle, "12")

            slot_of_11_before = lifecycle.get("11", "kanboard").ai_agent_id

            # Ticket 10 hands off for review → releases its slot, triggers pickup.
            result = await wf.signal_ready_for_review("10")

        assert result is True
        # Ticket 10 released and waiting for human.
        rec10 = lifecycle.get("10", "kanboard")
        assert rec10.state == TicketState.WAITING_FOR_HUMAN
        assert rec10.ai_agent_id is None
        # The freed slot was reused: waiting ticket 12 is now claimed + started.
        rec12 = lifecycle.get("12", "kanboard")
        assert rec12.ai_agent_id is not None
        assert rec12.state == TicketState.IN_PROGRESS
        # Ticket 11 was NOT preempted — same claim, same slot.
        assert lifecycle.get("11", "kanboard").ai_agent_id == slot_of_11_before

    @pytest.mark.asyncio
    async def test_default_is_single_agent(
        self, lifecycle, mock_kanban, mock_branch, mock_dev_env, mock_ac_gen
    ):
        """Omitting max_parallel_agents keeps the one-ticket-at-a-time gate."""
        wf = HumanGatedWorkflow(
            kanban=mock_kanban,
            events=Events(),
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
            await wf._start_ai_work("10", self._ready_assigned(lifecycle, "10"))
            await wf._start_ai_work("11", self._ready_assigned(lifecycle, "11"))

        # Only the first ticket is claimed; the second waits.
        assert lifecycle.get("10", "kanboard").ai_agent_id is not None
        assert lifecycle.get("11", "kanboard").ai_agent_id is None

    @pytest.mark.asyncio
    async def test_resume_waits_when_all_slots_busy(
        self, make_workflow, lifecycle, mock_kanban
    ):
        """A resumed ticket waits (unclaimed) when no slot is free.

        With N=1 and the single slot busy on another ticket, a human
        comment on a waiting ticket must NOT exceed the cap: the ticket
        transitions back to IN_PROGRESS but stays unclaimed until a slot
        frees. This is the backpressure that keeps the parallel cap honest
        without preempting the in-flight ticket.
        """
        wf = make_workflow(1)
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            # The single slot is busy on ticket 10.
            await wf._start_ai_work("10", self._ready_assigned(lifecycle, "10"))

            # Ticket 20 is waiting-for-human; a human comments to resume it.
            lifecycle.get_or_create("20", "kanboard")
            lifecycle.transition("20", "kanboard", TicketState.READY)
            lifecycle.transition("20", "kanboard", TicketState.IN_PROGRESS)
            lifecycle.transition("20", "kanboard", TicketState.WAITING_FOR_HUMAN)
            lifecycle.set_assignee("20", "kanboard", "bob")

            event = _make_event(
                {"ticket_id": "20", "comment_body": "please continue",
                 "comment_author": "bob", "provider": "kanboard"}
            )
            await wf._on_comment_added(event)

        rec20 = lifecycle.get("20", "kanboard")
        # Transitioned back to IN_PROGRESS but left unclaimed (cap reached).
        assert rec20.state == TicketState.IN_PROGRESS
        assert rec20.ai_agent_id is None
        # Ticket 10 keeps its claim — never preempted.
        assert lifecycle.get("10", "kanboard").ai_agent_id == wf._agent_id

    @pytest.mark.asyncio
    async def test_unassign_frees_slot_and_picks_up_waiting_ticket(
        self, make_workflow, lifecycle, mock_kanban
    ):
        """Unassigning a busy ticket frees its slot for a waiting ticket.

        Under the parallel-agent cap, freeing capacity must immediately let
        waiting assigned work start — not sit idle until some unrelated
        completion event happens to trigger pickup.
        """
        wf = make_workflow(1)
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            # The single slot is busy on ticket 10.
            await wf._start_ai_work("10", self._ready_assigned(lifecycle, "10"))
            # Ticket 11 is ready + assigned, waiting for a free slot.
            self._ready_assigned(lifecycle, "11")

            # Human unassigns ticket 10 → its slot frees.
            event = _make_event(
                {"ticket_id": "10", "provider": "kanboard"}
            )
            await wf._on_ticket_unassigned(event)

        # Ticket 10 released; ticket 11 picked up into the freed slot.
        assert lifecycle.get("10", "kanboard").ai_agent_id is None
        rec11 = lifecycle.get("11", "kanboard")
        assert rec11.ai_agent_id is not None
        assert rec11.state == TicketState.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_todo_reset_frees_slot_and_picks_up_waiting_ticket(
        self, make_workflow, lifecycle, mock_kanban
    ):
        """Resetting a busy ticket to todo frees its slot for waiting work."""
        wf = make_workflow(1)
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            await wf._start_ai_work("10", self._ready_assigned(lifecycle, "10"))
            self._ready_assigned(lifecycle, "11")

            # Human drags ticket 10 back to the todo column.
            event = _make_event(
                {"ticket_id": "10", "new_status": "todo", "provider": "kanboard"}
            )
            await wf._on_status_changed(event)

        assert lifecycle.get("10", "kanboard").ai_agent_id is None
        rec11 = lifecycle.get("11", "kanboard")
        assert rec11.ai_agent_id is not None
        assert rec11.state == TicketState.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_invalid_count_coerced_to_one(
        self, lifecycle, mock_kanban, mock_branch, mock_dev_env, mock_ac_gen
    ):
        """A non-positive max_parallel_agents is clamped up to 1 (never zero)."""
        wf = HumanGatedWorkflow(
            kanban=mock_kanban,
            events=Events(),
            provider_name="kanboard",
            lifecycle=lifecycle,
            branch_manager=mock_branch,
            dev_env_manager=mock_dev_env,
            ac_generator=mock_ac_gen,
            max_parallel_agents=0,
        )
        assert wf._max_parallel_agents == 1
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            await wf._start_ai_work("10", self._ready_assigned(lifecycle, "10"))
        # Still works as a single agent.
        assert lifecycle.get("10", "kanboard").ai_agent_id is not None


# ---------------------------------------------------------------------------
# Deep-review fixes: close/merge/stack/duplicate-signal edge cases
# ---------------------------------------------------------------------------


class TestReviewFixes:
    """Regression tests for bugs found in the multi-agent deep review."""

    def _ready_assigned(self, lifecycle, tid: str, who: str = "alice") -> Any:
        """Create a READY, human-assigned, unclaimed ticket."""
        lifecycle.get_or_create(tid, "kanboard")
        lifecycle.transition(tid, "kanboard", TicketState.READY)
        lifecycle.set_assignee(tid, "kanboard", who)
        return lifecycle.get(tid, "kanboard")

    @pytest.mark.asyncio
    async def test_closing_unstarted_ready_ticket_marks_done_not_resurrected(
        self, workflow, lifecycle, mock_kanban
    ):
        """Human closing a waiting READY ticket → DONE, never re-picked-up."""
        self._ready_assigned(lifecycle, "50")

        event = _make_event({"ticket_id": "50", "provider": "kanboard"})
        await workflow._on_ticket_closed(event)

        rec = lifecycle.get("50", "kanboard")
        assert rec.state == TicketState.DONE
        assert rec.ai_agent_id is None
        # No longer available → a later pickup can never resurrect it.
        assert "50" not in {r.ticket_id for r in lifecycle.get_available_tickets()}

        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            await workflow._pickup_next_ticket()
        assert lifecycle.get("50", "kanboard").ai_agent_id is None

    @pytest.mark.asyncio
    async def test_merge_failure_sends_ticket_back_to_ai_for_rebase(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """A failed merge parks the ticket in READY (not a human), posts a
        rebase-needed comment, and moves the kanban card to "ready" — so
        an AI agent picks it up to rebase and resolve the conflict itself.

        _park_in_ready_for_rebase frees the slot and puts the ticket back
        in _next_worker_ticket's candidate pool (READY/IN_PROGRESS), so
        the _pickup_next_ticket() call right after synchronously reclaims
        THIS same ticket under a fresh internal "marcus-" slot — a
        worker-adoptable claim, not a real one (see _next_worker_ticket's
        _held_by_worker check) — rather than leaving it idle until the
        next poll. The old behavior left it IN_PROGRESS and claimed by
        the ORIGINAL slot forever — a permanent slot leak (deadlock at
        cap=1) — this proves that leak is gone: the ticket is workable
        again, not stuck.
        """
        mock_branch.merge_to_main = AsyncMock(return_value=False)
        lifecycle.get_or_create("60", "kanboard")
        lifecycle.transition("60", "kanboard", TicketState.READY)
        lifecycle.claim_ticket("60", "kanboard", workflow._agent_id)
        lifecycle.transition("60", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("60", "kanboard", "alice")

        event = _make_event({"ticket_id": "60", "provider": "kanboard"})
        await workflow._on_ticket_closed(event)

        rec = lifecycle.get("60", "kanboard")
        assert rec.state in (TicketState.READY, TicketState.IN_PROGRESS)
        # Not stuck under the ORIGINAL claim — either unclaimed, or
        # re-claimed by an internal (worker-adoptable) slot.
        assert rec.ai_agent_id is None or str(rec.ai_agent_id).startswith("marcus-")
        assert rec.assignee == "alice"  # still assigned — worker-eligible
        mock_kanban.move_task_to_column.assert_any_call("60", "ready")
        # The comment tells the AI agent what to do, not a human.
        posted_bodies = [c.args[-1] for c in mock_kanban.add_comment.call_args_list]
        assert any("rebase" in body.lower() for body in posted_bodies)

    @pytest.mark.asyncio
    async def test_merge_failure_flags_the_card_visibly(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """Regression: a card bouncing back to Ready after a failed merge
        was previously only explained in a comment INSIDE the ticket —
        invisible from the board view, so it looked identical to a fresh,
        never-started ticket. The card must be visibly flagged too."""
        mock_branch.merge_to_main = AsyncMock(return_value=False)
        lifecycle.get_or_create("61", "kanboard")
        lifecycle.transition("61", "kanboard", TicketState.READY)
        lifecycle.claim_ticket("61", "kanboard", workflow._agent_id)
        lifecycle.transition("61", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("61", "kanboard", "alice")

        event = _make_event({"ticket_id": "61", "provider": "kanboard"})
        await workflow._on_ticket_closed(event)

        mock_kanban.set_merge_conflict_flag.assert_awaited_once_with(
            "61", present=True
        )

    @pytest.mark.asyncio
    async def test_successful_merge_clears_the_flag(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """A ticket that merges successfully must have any leftover
        merge-conflict flag (from a prior failed attempt) cleared."""
        mock_branch.merge_to_main = AsyncMock(return_value=True)
        lifecycle.get_or_create("62", "kanboard")
        lifecycle.transition("62", "kanboard", TicketState.READY)
        lifecycle.claim_ticket("62", "kanboard", workflow._agent_id)
        lifecycle.transition("62", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("62", "kanboard", "alice")

        event = _make_event({"ticket_id": "62", "provider": "kanboard"})
        await workflow._on_ticket_closed(event)

        mock_kanban.set_merge_conflict_flag.assert_awaited_once_with(
            "62", present=False
        )

    @pytest.mark.asyncio
    async def test_resubmitting_for_review_clears_the_flag(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """Regression: a ticket that had a merge conflict, got fixed, and
        is resubmitted for human review (human-gate signal_ready_for_review)
        must have the stale flag cleared THEN — not only on a later
        successful merge. In human-gate mode the actual merge attempt
        only happens when a human accepts the ticket
        (_on_ticket_closed), which is LATER than when it lands in
        Waiting-for-Human; without this, the card would sit in "waiting
        for human" still showing "merge-conflict" even though the agent
        already fixed and resubmitted it."""
        lifecycle.get_or_create("63", "kanboard")
        lifecycle.transition("63", "kanboard", TicketState.READY)
        lifecycle.claim_ticket("63", "kanboard", "w-fix")
        lifecycle.transition("63", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("63", "kanboard", "alice")

        ok = await workflow.signal_ready_for_review("63")

        assert ok is True
        assert lifecycle.get("63", "kanboard").state == TicketState.WAITING_FOR_HUMAN
        mock_kanban.set_merge_conflict_flag.assert_awaited_once_with(
            "63", present=False
        )

    @pytest.mark.asyncio
    async def test_duplicate_signal_ready_does_not_repost_comment(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """A second signal_ready_for_review is a no-op (no duplicate comment)."""
        lifecycle.get_or_create("70", "kanboard")
        lifecycle.transition("70", "kanboard", TicketState.READY)
        lifecycle.claim_ticket("70", "kanboard", workflow._agent_id)
        lifecycle.transition("70", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("70", "kanboard", "alice")

        first = await workflow.signal_ready_for_review("70")
        comments_after_first = mock_kanban.add_comment.call_count

        second = await workflow.signal_ready_for_review("70")

        assert first is True
        assert second is False
        assert mock_kanban.add_comment.call_count == comments_after_first
        assert lifecycle.get("70", "kanboard").state == TicketState.WAITING_FOR_HUMAN

    @pytest.mark.asyncio
    async def test_stack_check_infers_description_instead_of_blocking(
        self, workflow, lifecycle, mock_kanban, tmp_path, monkeypatch
    ):
        """A thin project description is inferred so the ticket proceeds."""
        from src.core import project_description as pd

        # Point the manager at a temp dir so the test is isolated.
        monkeypatch.setattr(pd, "_DEFAULT_DATA_DIR", tmp_path)

        # An inferrer that returns a description WITH a parseable stack.
        inferrer = MagicMock()
        inferrer.infer = AsyncMock(
            return_value=(
                "# Shop\n\n## Tech Stack\n- **Language**: Python\n"
                "- **Dev server command**: uvicorn main:app --port 3000\n"
            )
        )
        workflow._desc_inferrer = inferrer

        task = MagicMock()
        task.name = "Add checkout"
        task.description = "A FastAPI service"
        task.source_context = {"kanboard_task": {"project_id": 42}}
        mock_kanban.get_task_by_id = AsyncMock(return_value=task)
        mock_kanban.get_project_name = AsyncMock(return_value="Shop")

        ok = await workflow._check_project_stack("5")

        assert ok is True  # proceeded, did NOT pause on the human
        inferrer.infer.assert_awaited_once()
        # Description was stored as inferred (auto-updatable), stack parses.
        mgr = pd.ProjectDescriptionManager(data_dir=tmp_path)
        assert mgr.get_source(42) == pd.SOURCE_INFERRED
        assert mgr.get_stack(42) is not None

    @pytest.mark.asyncio
    async def test_stack_check_does_not_overwrite_human_description(
        self, workflow, lifecycle, mock_kanban, tmp_path, monkeypatch
    ):
        """A human-edited description is never overwritten by inference."""
        from src.core import project_description as pd

        monkeypatch.setattr(pd, "_DEFAULT_DATA_DIR", tmp_path)
        mgr = pd.ProjectDescriptionManager(data_dir=tmp_path)
        # Human wrote a description with NO parseable stack, and it's locked.
        mgr.update_description(42, "# Shop\n\nSome prose, no stack.\n")
        assert mgr.get_source(42) == pd.SOURCE_HUMAN

        inferrer = MagicMock()
        inferrer.infer = AsyncMock(return_value="# inferred\n")
        workflow._desc_inferrer = inferrer

        task = MagicMock()
        task.name = "t"
        task.description = ""
        task.source_context = {"kanboard_task": {"project_id": 42}}
        mock_kanban.get_task_by_id = AsyncMock(return_value=task)

        ok = await workflow._check_project_stack("5")

        assert ok is False  # blocked (human must fix), no overwrite
        inferrer.infer.assert_not_called()
        assert mgr.get_description(42) == "# Shop\n\nSome prose, no stack.\n"

    @pytest.mark.asyncio
    async def test_stack_check_failure_parks_ticket_out_of_available_pool(
        self, workflow, lifecycle, mock_kanban
    ):
        """A stack-check failure parks the ticket in WFH (no re-pickup spam)."""
        workflow._check_project_stack = AsyncMock(return_value=False)  # type: ignore[method-assign]
        rec = self._ready_assigned(lifecycle, "80")

        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            await workflow._start_ai_work("80", rec)

        parked = lifecycle.get("80", "kanboard")
        assert parked.state == TicketState.WAITING_FOR_HUMAN
        assert parked.ai_agent_id is None
        # Not available → not re-selected on the next pickup (no comment spam).
        assert "80" not in {r.ticket_id for r in lifecycle.get_available_tickets()}

    @pytest.mark.asyncio
    async def test_drag_to_done_column_merges(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """A status_changed to 'done' triggers the merge (not just close)."""
        lifecycle.get_or_create("30", "kanboard")
        lifecycle.transition("30", "kanboard", TicketState.READY)
        lifecycle.claim_ticket("30", "kanboard", workflow._agent_id)
        lifecycle.transition("30", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("30", "kanboard", TicketState.WAITING_FOR_HUMAN)
        lifecycle.set_assignee("30", "kanboard", "alice")

        event = _make_event(
            {"ticket_id": "30", "new_status": "done", "provider": "kanboard"}
        )
        await workflow._on_status_changed(event)

        mock_branch.merge_to_main.assert_awaited()
        assert lifecycle.get("30", "kanboard").state == TicketState.DONE

    @pytest.mark.asyncio
    async def test_approve_comment_merges(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """A human "approve" comment on a waiting ticket merges to main."""
        lifecycle.get_or_create("31", "kanboard")
        lifecycle.transition("31", "kanboard", TicketState.READY)
        lifecycle.claim_ticket("31", "kanboard", workflow._agent_id)
        lifecycle.transition("31", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("31", "kanboard", TicketState.WAITING_FOR_HUMAN)
        lifecycle.set_assignee("31", "kanboard", "alice")

        event = _make_event(
            {"ticket_id": "31", "comment_body": "approved. merge to main",
             "comment_author": "alice", "provider": "kanboard"}
        )
        await workflow._on_comment_added(event)

        mock_branch.merge_to_main.assert_awaited()
        assert lifecycle.get("31", "kanboard").state == TicketState.DONE

    @pytest.mark.asyncio
    async def test_post_comment_emits_ui_refresh(
        self, workflow, lifecycle, mock_kanban
    ):
        """Every posted comment publishes ui.refresh (drives the SSE push)."""
        received = []

        async def _capture(event):
            received.append(event)

        workflow._events.subscribe("ui.refresh", _capture)

        lifecycle.get_or_create("40", "kanboard")
        await workflow._post_comment("40", "hello")

        assert len(received) == 1
        assert received[0].data["ticket_id"] == "40"

    def test_is_approval_comment_recognizes_and_rejects(self, workflow):
        """Approval matcher: accepts approvals, rejects negated/conditional."""
        assert workflow._is_approval_comment("approved. merge to main") is True
        assert workflow._is_approval_comment("LGTM") is True
        assert workflow._is_approval_comment("@marcus approve") is True
        assert workflow._is_approval_comment("looks good, ship it") is True
        # Negated / conditional must NOT be treated as approval.
        assert workflow._is_approval_comment("don't merge yet") is False
        assert workflow._is_approval_comment("approve after you fix the test") is False
        assert workflow._is_approval_comment("please change the button color") is False

    @pytest.mark.asyncio
    async def test_non_approval_comment_still_requests_changes(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """A normal comment on a waiting ticket resumes the agent (no merge)."""
        lifecycle.get_or_create("32", "kanboard")
        lifecycle.transition("32", "kanboard", TicketState.READY)
        lifecycle.claim_ticket("32", "kanboard", workflow._agent_id)
        lifecycle.transition("32", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("32", "kanboard", TicketState.WAITING_FOR_HUMAN)
        lifecycle.set_assignee("32", "kanboard", "alice")

        event = _make_event(
            {"ticket_id": "32", "comment_body": "change the button to blue",
             "comment_author": "alice", "provider": "kanboard"}
        )
        await workflow._on_comment_added(event)

        mock_branch.merge_to_main.assert_not_awaited()
        assert lifecycle.get("32", "kanboard").state == TicketState.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_pickup_ignores_foreign_provider_records(
        self, workflow, lifecycle, mock_kanban
    ):
        """Pickup skips available records from a different provider (no KeyError)."""
        # A workable, assigned, unclaimed record under a DIFFERENT provider.
        lifecycle.get_or_create("90", "jira")
        lifecycle.transition("90", "jira", TicketState.READY)
        lifecycle.set_assignee("90", "jira", "alice")

        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            # Must not raise KeyError trying to claim jira:90 under kanboard.
            await workflow._pickup_next_ticket()

        assert lifecycle.get("90", "jira").ai_agent_id is None


class TestOrchestrateWork:
    """Marcus-as-orchestrator: the marcus_work single-tool loop."""

    @pytest.mark.asyncio
    async def test_first_call_adopts_human_readied_ticket(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """Human-triggered: only a READY, human-assigned ticket is handed out.

        The human stays the assignee (owner); the worker takes the claim.
        """
        lifecycle.get_or_create("5", "kanboard")
        lifecycle.transition("5", "kanboard", TicketState.READY)
        lifecycle.set_assignee("5", "kanboard", "alice")  # human owner
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            res = await workflow.orchestrate_work(agent_id="w1")

        assert res["status"] == "assigned"
        assert res["ticket_id"] == "5"
        assert lifecycle.get_agent_ticket("w1") == "5"
        assert lifecycle.get("5", "kanboard").state == TicketState.IN_PROGRESS
        # Human remains the owner; worker only holds the claim.
        assert lifecycle.get("5", "kanboard").assignee == "alice"

    @pytest.mark.asyncio
    async def test_starting_work_sets_the_kanboard_start_date(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """Marcus sets Kanboard's native date_started when AI work
        actually begins, mirroring the "Start now" link so a human never
        has to click it."""
        lifecycle.get_or_create("5c", "kanboard")
        lifecycle.transition("5c", "kanboard", TicketState.READY)
        lifecycle.set_assignee("5c", "kanboard", "alice")
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            await workflow.orchestrate_work(agent_id="w1")

        mock_kanban.set_task_started_if_unset.assert_awaited_once_with("5c")

    @pytest.mark.asyncio
    async def test_ticket_assigned_to_anyone_is_handed_out(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """Assigned to ANY human (not necessarily the requester) → handed out."""
        lifecycle.get_or_create("15", "kanboard")
        lifecycle.transition("15", "kanboard", TicketState.READY)
        lifecycle.set_assignee("15", "kanboard", "bob")  # some other human
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
        ):
            res = await workflow.orchestrate_work(agent_id="worker-xyz")
        assert res["status"] == "assigned"
        assert res["ticket_id"] == "15"
        assert lifecycle.get("15", "kanboard").assignee == "bob"  # owner unchanged

    @pytest.mark.asyncio
    async def test_unassigned_ready_ticket_is_not_handed_out(
        self, workflow, lifecycle, mock_kanban
    ):
        """A READY ticket with NO human assignee is not autonomous work."""
        lifecycle.get_or_create("6", "kanboard")
        lifecycle.transition("6", "kanboard", TicketState.READY)
        # no assignee
        res = await workflow.orchestrate_work(agent_id="w2")
        assert res["status"] == "no_work"

    @pytest.mark.asyncio
    async def test_no_available_work(self, workflow, lifecycle, mock_kanban):
        """Nothing human-readied → status no_work."""
        lifecycle.get_or_create("9", "kanboard")  # a TODO record, no assignee
        res = await workflow.orchestrate_work(agent_id="w2")
        assert res["status"] == "no_work"

    @pytest.mark.asyncio
    async def test_progress_report_summarized_to_comment(
        self, workflow, lifecycle, mock_kanban
    ):
        """A progress report is posted as a comment; status continue."""
        lifecycle.get_or_create("7", "kanboard")
        lifecycle.transition("7", "kanboard", TicketState.READY)
        lifecycle.claim_ticket("7", "kanboard", "w3")
        lifecycle.transition("7", "kanboard", TicketState.IN_PROGRESS)

        res = await workflow.orchestrate_work(
            agent_id="w3", ticket_id="7", report="wrote the model layer"
        )
        assert res["status"] == "continue"
        mock_kanban.add_comment.assert_awaited()

    @pytest.mark.asyncio
    async def test_done_report_completes_ticket(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """A 'DONE' report hands the ticket off via the gate (human → WFH)."""
        lifecycle.get_or_create("8", "kanboard")
        lifecycle.transition("8", "kanboard", TicketState.READY)
        lifecycle.claim_ticket("8", "kanboard", "w4")
        lifecycle.transition("8", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("8", "kanboard", "w4")

        res = await workflow.orchestrate_work(
            agent_id="w4", ticket_id="8", report="DONE - shipped the feature"
        )
        assert res["status"] == "done"
        assert lifecycle.get("8", "kanboard").state == TicketState.WAITING_FOR_HUMAN

    @pytest.mark.asyncio
    async def test_done_report_posts_a_work_finished_comment(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """Marcus records when the AI agent finished as a comment, not
        Kanboard's native "Completed" date field (that field only sets
        via Kanboard's "close task" action, which also archives the card
        and hides it from every board column — see the docstring on
        set_task_started_if_unset)."""
        lifecycle.get_or_create("8b", "kanboard")
        lifecycle.transition("8b", "kanboard", TicketState.READY)
        lifecycle.claim_ticket("8b", "kanboard", "w4")
        lifecycle.transition("8b", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("8b", "kanboard", "w4")

        await workflow.orchestrate_work(
            agent_id="w4", ticket_id="8b", report="DONE - shipped the feature"
        )

        posted = [c.args[-1] for c in mock_kanban.add_comment.call_args_list]
        assert any("Work Finished" in body for body in posted)

    def test_classify_report_intent(self, workflow):
        """Report-prefix protocol maps to intents."""
        assert workflow._classify_report_intent("DONE - x") == "done"
        assert workflow._classify_report_intent("BLOCKED - y") == "blocked"
        assert workflow._classify_report_intent("wrote a test") == "progress"

    @pytest.mark.asyncio
    async def test_done_report_ignores_a_ticket_id_the_agent_does_not_own(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """Regression: ticket_id is an unauthenticated echo of what Marcus
        previously returned to the caller, not an authority — an agent
        that actually holds ticket A but reports 'DONE' against a
        DIFFERENT ticket_id (e.g. a stale/hallucinated value, or another
        agent's ticket) must have the report applied to its OWN real
        ticket, not the arbitrary one it named. Previously the caller-
        supplied ticket_id was trusted outright, so a mismatched id could
        mark someone ELSE's in-progress ticket ready-for-review (or, on
        an AI gate, merge it to main) while that other agent's real work
        was still incomplete."""
        # Agent w5 actually holds ticket 10.
        lifecycle.get_or_create("10", "kanboard")
        lifecycle.transition("10", "kanboard", TicketState.READY)
        lifecycle.claim_ticket("10", "kanboard", "w5")
        lifecycle.transition("10", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("10", "kanboard", "w5")

        # Ticket 11 is a DIFFERENT agent's real in-progress work.
        lifecycle.get_or_create("11", "kanboard")
        lifecycle.transition("11", "kanboard", TicketState.READY)
        lifecycle.claim_ticket("11", "kanboard", "w6")
        lifecycle.transition("11", "kanboard", TicketState.IN_PROGRESS)

        res = await workflow.orchestrate_work(
            agent_id="w5", ticket_id="11", report="DONE - shipped it"
        )

        # The report must land on w5's own ticket (10), never on 11.
        assert res["ticket_id"] == "10"
        assert lifecycle.get("10", "kanboard").state == TicketState.WAITING_FOR_HUMAN
        # Ticket 11 — owned by a different agent — must be untouched.
        assert lifecycle.get("11", "kanboard").state == TicketState.IN_PROGRESS
        assert lifecycle.get("11", "kanboard").ai_agent_id == "w6"


class TestReclaimStuckTicket:
    """An agent that loses track of its own ticket must not leave it
    abandoned forever — a genuinely stuck ticket is reassigned to
    whichever agent asks next, with full context, instead of Marcus
    starting fresh work while the old ticket silently rots."""

    def _stuck_ticket(self, lifecycle, tid, *, held_by="agent-old", stale=True):
        """An IN_PROGRESS, human-assigned ticket claimed by *held_by*,
        backdated past (or within, if stale=False) the staleness window."""
        lifecycle.get_or_create(tid, "kanboard")
        lifecycle.transition(tid, "kanboard", TicketState.READY)
        lifecycle.set_assignee(tid, "kanboard", "alice")
        lifecycle.claim_ticket(tid, "kanboard", held_by)
        lifecycle.transition(tid, "kanboard", TicketState.IN_PROGRESS)
        rec = lifecycle.get(tid, "kanboard")
        age = timedelta(seconds=700) if stale else timedelta(seconds=10)
        rec.updated_at = datetime.now(timezone.utc) - age
        return rec

    @pytest.mark.asyncio
    async def test_stuck_ticket_is_reassigned_with_full_context(
        self, workflow, lifecycle, mock_kanban
    ):
        """A ticket stuck past the timeout is handed to the new agent,
        claim reassigned, same shape as resuming one's own ticket."""
        self._stuck_ticket(lifecycle, "40")

        res = await workflow.orchestrate_work(agent_id="agent-new")

        assert res["status"] == "working"
        assert res["ticket_id"] == "40"
        assert "context" in res
        assert lifecycle.get_agent_ticket("agent-new") == "40"
        assert lifecycle.get("40", "kanboard").ai_agent_id == "agent-new"

    @pytest.mark.asyncio
    async def test_recently_claimed_ticket_is_not_reclaimed(
        self, workflow, lifecycle, mock_kanban
    ):
        """A ticket claimed well within the timeout is left alone —
        the new agent gets told there's no work, not someone else's
        in-flight ticket."""
        self._stuck_ticket(lifecycle, "41", stale=False)

        res = await workflow.orchestrate_work(agent_id="agent-new")

        assert res["status"] == "no_work"
        assert lifecycle.get("41", "kanboard").ai_agent_id == "agent-old"

    @pytest.mark.asyncio
    async def test_reclaim_takes_priority_over_a_fresh_ready_ticket(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """Recovering abandoned work wins over starting something new,
        even when a perfectly good fresh ticket is also available."""
        self._stuck_ticket(lifecycle, "42")
        lifecycle.get_or_create("43", "kanboard")
        lifecycle.transition("43", "kanboard", TicketState.READY)
        lifecycle.set_assignee("43", "kanboard", "bob")

        res = await workflow.orchestrate_work(agent_id="agent-new")

        assert res["ticket_id"] == "42"

    @pytest.mark.asyncio
    async def test_recent_progress_heartbeat_keeps_a_ticket_from_reclaim(
        self, workflow, lifecycle, mock_kanban
    ):
        """Even with an old updated_at, a LIVE progress heartbeat (the
        agent has reported recently) must win — staleness is judged by
        the more recent of the two signals, not just the state-machine
        timestamp."""
        self._stuck_ticket(lifecycle, "44")
        workflow._mark_progress_activity("44")

        res = await workflow.orchestrate_work(agent_id="agent-new")

        assert res["status"] == "no_work"
        assert lifecycle.get("44", "kanboard").ai_agent_id == "agent-old"

    @pytest.mark.asyncio
    async def test_internal_slot_claim_is_never_reclaimed_as_stuck(
        self, workflow, lifecycle, mock_kanban
    ):
        """A 'marcus-' internal auto-start slot is bookkeeping, not a live
        agent — _reclaim_stuck_ticket must never treat it as a stuck
        session (it's already adoptable via the normal hand-out path;
        see _held_by_worker / orchestrate_work's own IN_PROGRESS-adoption
        branch, a separate, pre-existing mechanism from this one)."""
        self._stuck_ticket(lifecycle, "45", held_by="marcus-slot-0")

        result = await workflow._reclaim_stuck_ticket("agent-new")

        assert result is None
        assert lifecycle.get("45", "kanboard").ai_agent_id == "marcus-slot-0"

    @pytest.mark.asyncio
    async def test_reclaimed_ticket_posts_a_visible_comment(
        self, workflow, lifecycle, mock_kanban
    ):
        """A human watching the board must be able to see a reclaim
        happened, not just have the claim silently swapped."""
        self._stuck_ticket(lifecycle, "46")

        await workflow.orchestrate_work(agent_id="agent-new")

        posted = [c.args[-1] for c in mock_kanban.add_comment.call_args_list]
        assert any("reassign" in body.lower() for body in posted)

    @pytest.mark.asyncio
    async def test_most_stale_ticket_is_reclaimed_first(
        self, workflow, lifecycle, mock_kanban
    ):
        """With two stuck tickets, the one that has been silent longest
        is recovered first."""
        self._stuck_ticket(lifecycle, "47", held_by="agent-a")
        lifecycle.get("47", "kanboard").updated_at = (
            datetime.now(timezone.utc) - timedelta(seconds=700)
        )
        self._stuck_ticket(lifecycle, "48", held_by="agent-b")
        lifecycle.get("48", "kanboard").updated_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1200)
        )

        res = await workflow.orchestrate_work(agent_id="agent-new")

        assert res["ticket_id"] == "48"

    @pytest.mark.asyncio
    async def test_concurrent_claim_is_resumed_not_raced(
        self, workflow, lifecycle, mock_kanban
    ):
        """If a concurrent orchestrate_work call for this SAME agent_id
        already claimed a ticket in the window between the initial
        checks and the fresh-ticket lookup, resume THAT ticket instead
        of racing to claim a different one. claim_ticket itself now
        refuses to give one agent a second claim (one agent, one ticket
        at a time) — this proves orchestrate_work handles that refusal
        gracefully by resuming, not by erroring or double-claiming."""
        lifecycle.get_or_create("60", "kanboard")
        lifecycle.transition("60", "kanboard", TicketState.READY)
        lifecycle.set_assignee("60", "kanboard", "alice")

        async def fake_reclaim(agent_id):
            # Simulate another concurrent call claiming ticket 60 for this
            # same agent_id while _reclaim_stuck_ticket was in flight.
            lifecycle.claim_ticket("60", "kanboard", agent_id)
            lifecycle.transition("60", "kanboard", TicketState.IN_PROGRESS)
            return None

        workflow._reclaim_stuck_ticket = fake_reclaim

        res = await workflow.orchestrate_work(agent_id="agent-race")

        assert res["status"] == "working"
        assert res["ticket_id"] == "60"

    @pytest.mark.asyncio
    async def test_two_different_new_agents_cannot_both_reclaim_the_same_ticket(
        self, workflow, lifecycle, mock_kanban
    ):
        """Regression: _reclaim_stuck_ticket ignored claim_ticket()'s
        return value AND unconditionally called release_ticket() right
        before claiming — so two concurrent _reclaim_stuck_ticket calls
        for two DIFFERENT new agent_ids, both racing for the same stuck
        ticket, could each release-then-claim it in turn. Both would
        then believe they own it (both return the ticket id), even
        though the lifecycle store — the single source of truth for
        "who owns this ticket" — can only actually record one of them.
        This defeats claim_ticket's own documented guarantee that "at
        most one AI agent holds a claim on a given ticket at any time"."""
        self._stuck_ticket(lifecycle, "61", held_by="agent-old")

        # Force a genuine event-loop yield at the same await point the
        # real code hits (_may_touch), so both concurrent calls reach
        # their release/claim pair having both already computed "ticket
        # 61 is the most-stale candidate" from the SAME pre-race state —
        # reproducing the actual interleaving a live deployment would see
        # (two real network-bound _resolve_kanboard_project_id calls),
        # which an all-synchronous AsyncMock chain doesn't naturally
        # produce (nothing truly suspends, so gather() would otherwise
        # just run the two calls back-to-back with no real overlap).
        real_may_touch = workflow._may_touch

        async def yielding_may_touch(ticket_id):
            await asyncio.sleep(0)
            return await real_may_touch(ticket_id)

        workflow._may_touch = yielding_may_touch

        result_a, result_b = await asyncio.gather(
            workflow._reclaim_stuck_ticket("agent-A"),
            workflow._reclaim_stuck_ticket("agent-B"),
        )

        results = {result_a, result_b}
        # At most one of the two concurrent callers may believe it
        # reclaimed ticket 61 — the other must get None (nothing left to
        # reclaim), never both claiming success on the same ticket.
        assert results in ({"61", None}, {None})
        # And the lifecycle record's actual owner must match whichever
        # caller "won" (if either did) — no split-brain state where
        # both callers were told a different story than the store holds.
        current_owner = lifecycle.get("61", "kanboard").ai_agent_id
        if result_a == "61":
            assert current_owner == "agent-A"
        if result_b == "61":
            assert current_owner == "agent-B"


class TestReopenGuard:
    """ticket.reopened is only honored for genuinely DONE records.

    Regression for the production feedback loop: task.open events on
    board-closed tickets with stale IN_PROGRESS records triggered
    reopen → release claim → pickup re-claims → move column → openTask →
    task.open → reopen … until Kanboard's SQLite locked up.
    """

    @pytest.mark.asyncio
    async def test_reopen_ignored_for_in_progress_record(
        self, workflow, lifecycle, mock_branch
    ):
        """A reopen event on an IN_PROGRESS record is a no-op."""
        lifecycle.get_or_create("50", "kanboard")
        lifecycle.transition("50", "kanboard", TicketState.READY)
        lifecycle.claim_ticket("50", "kanboard", workflow._agent_id)
        lifecycle.transition("50", "kanboard", TicketState.IN_PROGRESS)

        event = _make_event({"ticket_id": "50", "provider": "kanboard"})
        await workflow._on_ticket_reopened(event)

        # No rebase, claim intact, state unchanged — the loop is broken.
        mock_branch.rebase_on_main.assert_not_awaited()
        rec = lifecycle.get("50", "kanboard")
        assert rec.ai_agent_id == workflow._agent_id
        assert rec.state == TicketState.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_reopen_still_works_for_done_record(
        self, workflow, lifecycle, mock_branch, mock_kanban
    ):
        """A genuine reopen (record DONE) still rebases and resumes."""
        lifecycle.get_or_create("51", "kanboard")
        lifecycle.transition("51", "kanboard", TicketState.READY)
        lifecycle.transition("51", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("51", "kanboard", TicketState.DONE)

        event = _make_event({"ticket_id": "51", "provider": "kanboard"})
        await workflow._on_ticket_reopened(event)

        mock_branch.rebase_on_main.assert_awaited()
        assert lifecycle.get("51", "kanboard").state == TicketState.IN_PROGRESS


class TestDecomposition:
    """Marcus splits a large ticket into linked, status-inheriting children."""

    def _big_ticket(self, lifecycle, tid="100", owner="alice"):
        lifecycle.get_or_create(tid, "kanboard")
        lifecycle.transition(tid, "kanboard", TicketState.READY)
        lifecycle.set_assignee(tid, "kanboard", owner)
        # 4 AC items → passes the cheap decompose gate.
        lifecycle.update_acceptance_criteria(
            tid, "kanboard",
            "- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d", "hash",
        )
        return lifecycle.get(tid, "kanboard")

    async def _decompose_with(self, workflow, mock_kanban, parent_column):
        """Run a 2-child decompose against a parent in *parent_column*."""

        async def fake_llm(prompt):
            return (
                '{"subtasks": [{"title": "Backend", "description": "api", '
                '"acceptance_criteria": "- [ ] api"}, {"title": "Frontend", '
                '"description": "ui", "acceptance_criteria": "- [ ] ui"}]}'
            )

        workflow._llm_generate = fake_llm
        created = {"n": 200}

        async def fake_create(data):
            created["n"] += 1
            t = MagicMock()
            t.id = str(created["n"])
            t.name = data["name"]
            return t

        mock_kanban.create_task = AsyncMock(side_effect=fake_create)
        mock_kanban.create_task_link = AsyncMock(return_value=True)
        mock_kanban.assign_task = AsyncMock(return_value=True)
        parent = MagicMock(description="do a lot")
        parent.name = "Big"
        parent.source_context = {"kanboard_task": {"project_id": 3}}
        parent.status = parent_column
        mock_kanban.get_task_by_id = AsyncMock(return_value=parent)
        return await workflow.decompose_ticket("100")

    @pytest.mark.asyncio
    async def test_refuses_to_decompose_a_disabled_project_ticket(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """decompose_ticket must not call the LLM or write to the board at
        all for a ticket whose project is disabled.

        Without its own gate, decompose_ticket's multi-second LLM call
        (seconds, not microseconds) creates a window where a human can
        disable the project mid-call — after which decompose still creates
        child tickets, assigns them, and parks the parent as BLOCKED, all
        on a project Marcus is not supposed to touch. Relying on the
        caller (orchestrate_work) to have checked the gate before calling
        in is not enough, since the call itself is what takes the time.
        """
        self._big_ticket(lifecycle)
        mock_project_access.is_enabled = MagicMock(return_value=False)
        llm_called = {"yes": False}

        async def fake_llm(prompt):
            llm_called["yes"] = True
            return '{"subtasks": []}'

        workflow._llm_generate = fake_llm
        parent = MagicMock(description="do a lot")
        parent.name = "Big"
        parent.source_context = {"kanboard_task": {"project_id": 3}}
        mock_kanban.get_task_by_id = AsyncMock(return_value=parent)
        mock_kanban.create_task = AsyncMock()

        children = await workflow.decompose_ticket("100")

        assert children == []
        assert llm_called["yes"] is False
        mock_kanban.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_to_write_children_disabled_during_the_llm_call(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """Even when the LLM DOES return subtasks, nothing gets written if
        the project was disabled while that call was in flight.

        The pre-call gate only stops a call that was already pointless; it
        cannot see a disable that happens during the call itself. Without a
        second check right after the LLM returns, decompose_ticket would
        still create child tickets, assign them, link them, and park the
        parent as BLOCKED — all writes to a project Marcus is not supposed
        to touch. That the resulting children would be re-filtered before
        ever reaching an agent (orchestrate_work recurses through
        _next_worker_ticket) does not make the writes themselves OK.
        """
        self._big_ticket(lifecycle)
        calls = {"n": 0}

        def is_enabled(pid):
            calls["n"] += 1
            return calls["n"] == 1  # enabled for the pre-call check only

        mock_project_access.is_enabled = MagicMock(side_effect=is_enabled)

        async def fake_llm(prompt):
            return (
                '{"subtasks": [{"title": "Backend", "description": "api", '
                '"acceptance_criteria": "- [ ] api"}, {"title": "Frontend", '
                '"description": "ui", "acceptance_criteria": "- [ ] ui"}]}'
            )

        workflow._llm_generate = fake_llm
        parent = MagicMock(description="do a lot")
        parent.name = "Big"
        parent.source_context = {"kanboard_task": {"project_id": 3}}
        mock_kanban.get_task_by_id = AsyncMock(return_value=parent)
        mock_kanban.create_task = AsyncMock()

        children = await workflow.decompose_ticket("100")

        assert children == []
        mock_kanban.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_children_are_assigned_on_the_board_not_just_internally(
        self, workflow, lifecycle, mock_kanban
    ):
        """Sub-tickets must be assigned to the parent's user ON THE BOARD.

        Inheriting the owner into Marcus's own lifecycle record only is not
        enough: the Kanboard card shows no assignee, so a human looking at
        the board sees unowned sub-tickets, and any path that re-derives
        state from the board (a fresh Marcus against an existing board)
        loses the owner entirely — at which point the children stop being
        handout candidates, since _next_worker_ticket requires a human
        owner.
        """
        self._big_ticket(lifecycle)

        children = await self._decompose_with(workflow, mock_kanban, "Ready")

        assigned = {
            call.args[0]: call.args[1]
            for call in mock_kanban.assign_task.await_args_list
        }
        assert set(assigned) == set(children)
        assert set(assigned.values()) == {"alice"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "parent_column", ["In Progress", "Blocked", "Waiting for Human", "Ready"]
    )
    async def test_children_always_start_in_ready(
        self, workflow, lifecycle, mock_kanban, parent_column
    ):
        """Sub-tickets ALWAYS start in Ready, whatever column the parent is in.

        A column says who is working a card, not where it sits in the plan.
        A freshly created child has not been claimed by any agent, so
        putting it in In Progress just because its parent was there would
        advertise work nobody is doing; and copying a Blocked/Waiting
        column would create children no worker can pick up at all. Ready is
        precisely "assigned and available to claim", which is what these
        are.
        """
        self._big_ticket(lifecycle)

        children = await self._decompose_with(
            workflow, mock_kanban, parent_column
        )

        moves = {
            call.args[0]: call.args[1]
            for call in mock_kanban.move_task_to_column.await_args_list
            if call.args[0] in children
        }
        assert moves == {c: "ready" for c in children}

    @pytest.mark.asyncio
    async def test_decompose_creates_linked_children(
        self, workflow, lifecycle, mock_kanban
    ):
        """Two sub-tickets are created, linked to the parent, and inherit owner."""
        self._big_ticket(lifecycle)

        # LLM returns two subtasks.
        async def fake_llm(prompt):
            return (
                '{"subtasks": [{"title": "Backend", "description": "api", '
                '"acceptance_criteria": "- [ ] api"}, {"title": "Frontend", '
                '"description": "ui", "acceptance_criteria": "- [ ] ui"}]}'
            )
        workflow._llm_generate = fake_llm

        # create_task returns tasks with ids 201, 202; capture links.
        created = {"n": 200}

        async def fake_create(data):
            created["n"] += 1
            t = MagicMock()
            t.id = str(created["n"])
            t.name = data["name"]
            return t

        mock_kanban.create_task = AsyncMock(side_effect=fake_create)
        mock_kanban.create_task_link = AsyncMock(return_value=True)
        parent = MagicMock(description="do a lot")
        parent.name = "Big"
        parent.source_context = {"kanboard_task": {"project_id": 3}}
        mock_kanban.get_task_by_id = AsyncMock(return_value=parent)

        children = await workflow.decompose_ticket("100")

        assert children == ["201", "202"]
        # Children are created on the PARENT's board, not the configured one.
        for call in mock_kanban.create_task.await_args_list:
            assert call.args[0]["project_id"] == 3
        # Children inherit the human owner + are Ready (workable).
        for c in children:
            rec = lifecycle.get(c, "kanboard")
            assert rec.assignee == "alice"
            assert rec.state == TicketState.READY
        # The PARENT is blocked by each child: link(parent="100", child, 3)
        # ("is blocked by"). Parent is args[0]; type is args[2].
        link_calls = mock_kanban.create_task_link.await_args_list
        assert all(call.args[0] == "100" and call.args[2] == 3 for call in link_calls)
        assert {call.args[1] for call in link_calls} == set(children)
        # Parent parked so it isn't handed to a worker...
        assert lifecycle.get("100", "kanboard").state == TicketState.BLOCKED
        # ...and its Kanboard card is moved to the Blocked column.
        mock_kanban.move_task_to_column.assert_any_await("100", "blocked")
        # Each child's card is moved to Ready (not left in Kanboard's
        # createTask default column) — this is the visible "inherits
        # status from parent" behaviour on the board itself.
        mock_kanban.move_task_to_column.assert_any_await("201", "ready")
        mock_kanban.move_task_to_column.assert_any_await("202", "ready")

    @pytest.mark.asyncio
    async def test_decompose_patches_marker_if_child_record_won_a_race(
        self, workflow, lifecycle, mock_kanban
    ):
        """Regression: get_or_create() only applies acceptance_criteria on
        FIRST creation of a record. create(payload) makes a child ticket
        visible on the board via an RPC round trip BEFORE decompose_ticket
        gets to register its own lifecycle record with the "Sub-ticket
        of #N" parent marker — a concurrent BoardWatcher poll (its own
        background loop, or another agent's on-demand marcus_work
        triggering one) can observe the new ticket and fire ticket.new
        first. _on_ticket_new's own bare get_or_create (no AC) then wins
        the race, silently dropping the marker forever — after which
        _parent_of/_children_of can never recognize this child, and the
        parent stays BLOCKED even once every child is actually Done
        (exactly the reported symptom: children show Done in Kanboard's
        Internal Links, but the parent never auto-advances).

        Simulated here by pre-creating child "201"'s lifecycle record
        with no AC, mirroring what _on_ticket_new would have done had it
        won the race."""
        self._big_ticket(lifecycle)
        lifecycle.get_or_create("201", "kanboard")  # simulates the race

        async def fake_llm(prompt):
            return (
                '{"subtasks": [{"title": "Backend", "description": "api", '
                '"acceptance_criteria": "- [ ] api"}, {"title": "Frontend", '
                '"description": "ui", "acceptance_criteria": "- [ ] ui"}]}'
            )

        workflow._llm_generate = fake_llm
        created = {"n": 200}

        async def fake_create(data):
            created["n"] += 1
            t = MagicMock()
            t.id = str(created["n"])
            t.name = data["name"]
            return t

        mock_kanban.create_task = AsyncMock(side_effect=fake_create)
        mock_kanban.create_task_link = AsyncMock(return_value=True)
        parent = MagicMock(description="do a lot")
        parent.name = "Big"
        parent.source_context = {"kanboard_task": {"project_id": 3}}
        mock_kanban.get_task_by_id = AsyncMock(return_value=parent)

        children = await workflow.decompose_ticket("100")

        assert children == ["201", "202"]
        # Both children — including the one that lost the race — must
        # carry the marker, so the parent can recognize them.
        assert workflow._children_of("100")
        child_ids = {c.ticket_id for c in workflow._children_of("100")}
        assert child_ids == {"201", "202"}

    @pytest.mark.asyncio
    async def test_decompose_children_inherit_parent_color(
        self, workflow, lifecycle, mock_kanban
    ):
        """Children get the parent's card color — the most visible way to
        show "these belong together" on the board itself, since the "is
        a child of" link is only visible once a card is opened."""
        self._big_ticket(lifecycle)

        async def fake_llm(prompt):
            return (
                '{"subtasks": [{"title": "Backend", "description": "api", '
                '"acceptance_criteria": "- [ ] api"}, {"title": "Frontend", '
                '"description": "ui", "acceptance_criteria": "- [ ] ui"}]}'
            )

        workflow._llm_generate = fake_llm

        created = {"n": 300}

        async def fake_create(data):
            created["n"] += 1
            t = MagicMock()
            t.id = str(created["n"])
            t.name = data["name"]
            return t

        mock_kanban.create_task = AsyncMock(side_effect=fake_create)
        mock_kanban.create_task_link = AsyncMock(return_value=True)
        mock_kanban.get_task_color = AsyncMock(return_value="yellow")
        parent = MagicMock(description="do a lot")
        parent.name = "Big"
        parent.source_context = {"kanboard_task": {"project_id": 3}}
        mock_kanban.get_task_by_id = AsyncMock(return_value=parent)

        await workflow.decompose_ticket("100")

        mock_kanban.get_task_color.assert_awaited_once_with("100")
        call = mock_kanban.create_task.await_args_list[0]
        assert call.args[0]["color_id"] == "yellow"

    @pytest.mark.asyncio
    async def test_decompose_children_omit_color_when_parent_has_none(
        self, workflow, lifecycle, mock_kanban
    ):
        """No parent color resolvable -> children are created without a
        color_id override rather than an incorrect hardcoded one."""
        self._big_ticket(lifecycle)

        async def fake_llm(prompt):
            return (
                '{"subtasks": [{"title": "Backend", "description": "api", '
                '"acceptance_criteria": "- [ ] api"}, {"title": "Frontend", '
                '"description": "ui", "acceptance_criteria": "- [ ] ui"}]}'
            )

        workflow._llm_generate = fake_llm

        created = {"n": 300}

        async def fake_create(data):
            created["n"] += 1
            t = MagicMock()
            t.id = str(created["n"])
            t.name = data["name"]
            return t

        mock_kanban.create_task = AsyncMock(side_effect=fake_create)
        mock_kanban.create_task_link = AsyncMock(return_value=True)
        mock_kanban.get_task_color = AsyncMock(return_value=None)
        parent = MagicMock(description="do a lot")
        parent.name = "Big"
        parent.source_context = {"kanboard_task": {"project_id": 3}}
        mock_kanban.get_task_by_id = AsyncMock(return_value=parent)

        await workflow.decompose_ticket("100")

        call = mock_kanban.create_task.await_args_list[0]
        assert "color_id" not in call.args[0]

    @pytest.mark.asyncio
    async def test_decompose_creates_a_subtask_entry_per_child(
        self, workflow, lifecycle, mock_kanban
    ):
        """Each child also gets a native Kanboard Subtask on the PARENT —
        a separate, dedicated "Subtasks" section distinct from the
        functional "is blocked by" link asserted above. The title embeds
        the child's id as a prefix so _maybe_complete_parent can find and
        update the right entry later without storing a subtask id."""
        self._big_ticket(lifecycle)
        mock_kanban.create_subtask = AsyncMock(return_value="90")

        async def fake_llm(prompt):
            return (
                '{"subtasks": [{"title": "Backend", "description": "api", '
                '"acceptance_criteria": "- [ ] api"}, {"title": "Frontend", '
                '"description": "ui", "acceptance_criteria": "- [ ] ui"}]}'
            )
        workflow._llm_generate = fake_llm

        created = {"n": 200}

        async def fake_create(data):
            created["n"] += 1
            t = MagicMock()
            t.id = str(created["n"])
            t.name = data["name"]
            return t

        mock_kanban.create_task = AsyncMock(side_effect=fake_create)
        mock_kanban.create_task_link = AsyncMock(return_value=True)
        parent = MagicMock(description="do a lot")
        parent.name = "Big"
        parent.source_context = {"kanboard_task": {"project_id": 3}}
        mock_kanban.get_task_by_id = AsyncMock(return_value=parent)

        children = await workflow.decompose_ticket("100")

        assert children == ["201", "202"]
        subtask_titles = [
            call.args[1] for call in mock_kanban.create_subtask.await_args_list
        ]
        assert subtask_titles == ["#201 Backend", "#202 Frontend"]
        # Created ON the parent (args[0]), not on the child itself.
        assert all(
            call.args[0] == "100"
            for call in mock_kanban.create_subtask.await_args_list
        )

    @pytest.mark.asyncio
    async def test_child_move_to_ready_failure_is_logged(
        self, workflow, lifecycle, mock_kanban, caplog
    ):
        """A child's card failing to move to Ready is surfaced at WARNING —
        not swallowed. Marcus's internal lifecycle state still ends up
        READY (unaffected by the board-side failure), but a human has no
        way to notice a stuck card without this log."""
        self._big_ticket(lifecycle)

        async def fake_llm(prompt):
            return (
                '{"subtasks": [{"title": "Backend", "description": "api", '
                '"acceptance_criteria": "- [ ] api"}, {"title": "Frontend", '
                '"description": "ui", "acceptance_criteria": "- [ ] ui"}]}'
            )
        workflow._llm_generate = fake_llm

        created = {"n": 200}

        async def fake_create(data):
            created["n"] += 1
            t = MagicMock()
            t.id = str(created["n"])
            t.name = data["name"]
            return t

        mock_kanban.create_task = AsyncMock(side_effect=fake_create)
        mock_kanban.create_task_link = AsyncMock(return_value=True)
        parent = MagicMock(description="do a lot")
        parent.name = "Big"
        parent.source_context = {"kanboard_task": {"project_id": 3}}
        mock_kanban.get_task_by_id = AsyncMock(return_value=parent)

        # Child 201's move to Ready fails; 202's succeeds.
        async def move(ticket_id, column):
            return not (ticket_id == "201" and column == "ready")

        mock_kanban.move_task_to_column = AsyncMock(side_effect=move)

        import logging

        with caplog.at_level(logging.WARNING):
            children = await workflow.decompose_ticket("100")

        assert children == ["201", "202"]
        # Internal lifecycle state is still READY regardless of the
        # board-side move outcome for either child.
        assert lifecycle.get("201", "kanboard").state == TicketState.READY
        assert lifecycle.get("202", "kanboard").state == TicketState.READY
        assert any(
            "201" in r.message and "ready" in r.message.lower()
            for r in caplog.records
        )
        # 202 (which DID move successfully) is not spuriously warned about.
        assert not any(
            "202" in r.message and "ready" in r.message.lower()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_parent_move_to_blocked_exception_logs_once_not_twice(
        self, workflow, lifecycle, mock_kanban, caplog
    ):
        """When the parent's move to Blocked RAISES (e.g. an RPC timeout)
        rather than cleanly returning False, only the exception itself
        should be logged — not also the generic "no such column?"
        diagnostic, which is misleading for a transient error that has
        nothing to do with a missing column."""
        self._big_ticket(lifecycle)

        async def fake_llm(prompt):
            return (
                '{"subtasks": [{"title": "Backend", "description": "api", '
                '"acceptance_criteria": "- [ ] api"}, {"title": "Frontend", '
                '"description": "ui", "acceptance_criteria": "- [ ] ui"}]}'
            )
        workflow._llm_generate = fake_llm

        created = {"n": 200}

        async def fake_create(data):
            created["n"] += 1
            t = MagicMock()
            t.id = str(created["n"])
            t.name = data["name"]
            return t

        mock_kanban.create_task = AsyncMock(side_effect=fake_create)
        mock_kanban.create_task_link = AsyncMock(return_value=True)
        parent = MagicMock(description="do a lot")
        parent.name = "Big"
        parent.source_context = {"kanboard_task": {"project_id": 3}}
        mock_kanban.get_task_by_id = AsyncMock(return_value=parent)

        async def move(ticket_id, column):
            if ticket_id == "100" and column == "blocked":
                raise RuntimeError("kanboard RPC timeout")
            return True

        mock_kanban.move_task_to_column = AsyncMock(side_effect=move)

        import logging

        with caplog.at_level(logging.WARNING):
            children = await workflow.decompose_ticket("100")

        assert children == ["201", "202"]
        assert lifecycle.get("100", "kanboard").state == TicketState.BLOCKED
        parent_warnings = [
            r for r in caplog.records
            if "100" in r.message and "blocked" in r.message.lower()
        ]
        assert len(parent_warnings) == 1
        assert "kanboard RPC timeout" in parent_warnings[0].message
        assert "does this project" not in parent_warnings[0].message

    @pytest.mark.asyncio
    async def test_atomic_ticket_not_decomposed(
        self, workflow, lifecycle, mock_kanban
    ):
        """LLM returning no subtasks → nothing created, parent untouched."""
        self._big_ticket(lifecycle, tid="101")

        async def fake_llm(prompt):
            return '{"subtasks": []}'
        workflow._llm_generate = fake_llm
        mock_kanban.create_task = AsyncMock()
        mock_kanban.get_task_by_id = AsyncMock(
            return_value=MagicMock(name="x", description="y")
        )

        children = await workflow.decompose_ticket("101")
        assert children == []
        mock_kanban.create_task.assert_not_awaited()
        assert lifecycle.get("101", "kanboard").state == TicketState.READY

    def test_subticket_not_re_decomposed(self, workflow, lifecycle):
        """A ticket marked as a sub-ticket is never a decompose candidate."""
        lifecycle.get_or_create("102", "kanboard")
        lifecycle.update_acceptance_criteria(
            "102", "kanboard",
            "- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d\n<!-- Sub-ticket of #100 -->",
            "h",
        )
        workflow._llm_generate = AsyncMock()
        rec = lifecycle.get("102", "kanboard")
        assert workflow._should_attempt_decompose(rec) is False


class TestDependencyGate:
    """A ticket doesn't start until its dependencies are Done+merged."""

    def _make_links(self, depends_on_ids):
        return {
            "depends_on": [{"task_id": d, "title": "", "column": ""}
                           for d in depends_on_ids],
            "blocks": [],
            "relates_to": [],
        }

    @pytest.mark.asyncio
    async def test_blocks_when_dependency_not_done(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """Ready ticket with an unfinished dependency is parked BLOCKED."""
        # Dependency #40 exists but is not DONE.
        lifecycle.get_or_create("40", "kanboard")
        lifecycle.transition("40", "kanboard", TicketState.READY)
        # Dependent ticket #41, assigned + ready, depends on #40.
        lifecycle.get_or_create("41", "kanboard")
        lifecycle.transition("41", "kanboard", TicketState.READY)
        lifecycle.set_assignee("41", "kanboard", "alice")
        mock_kanban.get_task_links = AsyncMock(
            return_value=self._make_links(["40"])
        )

        rec = lifecycle.get("41", "kanboard")
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda p, t: f"ticket/{p}/{t}",
        ):
            await workflow._start_ai_work("41", rec)

        blocked = lifecycle.get("41", "kanboard")
        assert blocked.state == TicketState.BLOCKED
        assert blocked.ai_agent_id is None  # never claimed/started
        mock_branch.create_branch.assert_not_awaited()
        assert "#40" in (blocked.blocked_by or "")

    @pytest.mark.asyncio
    async def test_resumes_when_last_dependency_completes(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """When the blocking ticket merges, the dependent auto-resumes."""
        lifecycle.get_or_create("50", "kanboard")
        lifecycle.transition("50", "kanboard", TicketState.READY)
        # #51 blocked on #50.
        lifecycle.get_or_create("51", "kanboard")
        lifecycle.transition("51", "kanboard", TicketState.READY)
        lifecycle.set_assignee("51", "kanboard", "alice")
        lifecycle.human_transition("51", "kanboard", TicketState.BLOCKED)
        lifecycle.set_blocked_by("51", "kanboard", "#50")
        # Now #50 is done; links for #51 report #50 as its (now satisfied) dep.
        lifecycle.transition("50", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("50", "kanboard", TicketState.DONE)
        mock_kanban.get_task_links = AsyncMock(
            return_value=self._make_links(["50"])
        )

        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda p, t: f"ticket/{p}/{t}",
        ):
            await workflow._resume_tickets_blocked_by("50")

        resumed = lifecycle.get("51", "kanboard")
        assert resumed.state == TicketState.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_subticket_not_blocked_on_its_parent(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """A child's 'is a child of' link must NOT block it on the parent."""
        lifecycle.get_or_create("100", "kanboard")  # parent (not done)
        # Child #101 depends_on #100 (Kanboard classifies is-a-child-of so).
        lifecycle.get_or_create(
            "101", "kanboard",
            acceptance_criteria="- [ ] x\n<!-- Sub-ticket of #100 -->",
        )
        lifecycle.transition("101", "kanboard", TicketState.READY)
        lifecycle.set_assignee("101", "kanboard", "alice")
        mock_kanban.get_task_links = AsyncMock(
            return_value=self._make_links(["100"])
        )

        rec = lifecycle.get("101", "kanboard")
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            side_effect=lambda p, t: f"ticket/{p}/{t}",
        ):
            await workflow._start_ai_work("101", rec)

        # Not blocked — the parent link is excluded → it started.
        assert lifecycle.get("101", "kanboard").state == TicketState.IN_PROGRESS


class TestParentAutoComplete:
    """A parent goes to Waiting for Human once all its sub-tickets are
    Done — never straight to Done automatically, since a human should
    review the completed work first."""

    def _child(self, lifecycle, tid, parent):
        lifecycle.get_or_create(
            tid, "kanboard",
            acceptance_criteria=f"- [ ] x\n<!-- Sub-ticket of #{parent} -->",
        )

    @pytest.mark.asyncio
    async def test_parent_awaits_review_when_all_children_done(
        self, workflow, lifecycle, mock_kanban
    ):
        """All children Done → parent goes to Waiting for Human, not Done."""
        lifecycle.get_or_create("200", "kanboard")  # parent
        lifecycle.human_transition("200", "kanboard", TicketState.BLOCKED)
        self._child(lifecycle, "201", "200")
        self._child(lifecycle, "202", "200")
        # Both children done.
        for c in ("201", "202"):
            lifecycle.transition(c, "kanboard", TicketState.READY)
            lifecycle.transition(c, "kanboard", TicketState.IN_PROGRESS)
            lifecycle.transition(c, "kanboard", TicketState.DONE)

        await workflow._maybe_complete_parent("202")

        rec = lifecycle.get("200", "kanboard")
        assert rec.state == TicketState.WAITING_FOR_HUMAN
        assert rec.ai_agent_id is None
        mock_kanban.move_task_to_column.assert_any_call("200", "waiting for human")

    @pytest.mark.asyncio
    async def test_ai_gate_parent_completes_directly_without_human_review(
        self, workflow, lifecycle, mock_kanban
    ):
        """Regression: under the AI gate, a parent whose children are ALL
        done must go straight to Done — never parked in Waiting for
        Human for a review step that gate mode doesn't want. Each child
        already went through its own gate/verification individually
        before being merged, so no separate AI-verify round runs here
        either (the parent has no branch/diff of its own to verify)."""
        workflow._get_effective_gate = AsyncMock(return_value="ai")
        lifecycle.get_or_create("203", "kanboard")  # parent
        lifecycle.human_transition("203", "kanboard", TicketState.BLOCKED)
        self._child(lifecycle, "204", "203")
        self._child(lifecycle, "205", "203")
        for c in ("204", "205"):
            lifecycle.transition(c, "kanboard", TicketState.READY)
            lifecycle.transition(c, "kanboard", TicketState.IN_PROGRESS)
            lifecycle.transition(c, "kanboard", TicketState.DONE)

        await workflow._maybe_complete_parent("205")

        rec = lifecycle.get("203", "kanboard")
        assert rec.state == TicketState.DONE
        mock_kanban.move_task_to_column.assert_any_call("203", "done")
        # Never routed through the human-review parking step.
        moved_to = [c.args for c in mock_kanban.move_task_to_column.await_args_list]
        assert ("203", "waiting for human") not in moved_to

    @pytest.mark.asyncio
    async def test_parent_not_completed_while_a_child_pending(
        self, workflow, lifecycle, mock_kanban
    ):
        lifecycle.get_or_create("210", "kanboard")
        lifecycle.human_transition("210", "kanboard", TicketState.BLOCKED)
        self._child(lifecycle, "211", "210")
        self._child(lifecycle, "212", "210")
        lifecycle.transition("211", "kanboard", TicketState.READY)
        lifecycle.transition("211", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("211", "kanboard", TicketState.DONE)
        # #212 still in progress.
        lifecycle.transition("212", "kanboard", TicketState.READY)
        lifecycle.transition("212", "kanboard", TicketState.IN_PROGRESS)

        await workflow._maybe_complete_parent("211")

        assert lifecycle.get("210", "kanboard").state == TicketState.BLOCKED

    @pytest.mark.asyncio
    async def test_finished_child_syncs_its_own_subtask_entry_immediately(
        self, workflow, lifecycle, mock_kanban
    ):
        """A single child finishing marks ITS subtask entry done right
        away — not only once every sibling is also done — so the parent's
        Subtasks section reflects real progress as it happens."""
        mock_kanban.mark_subtask_done = AsyncMock(return_value=True)
        lifecycle.get_or_create("215", "kanboard")  # parent
        lifecycle.human_transition("215", "kanboard", TicketState.BLOCKED)
        self._child(lifecycle, "216", "215")
        self._child(lifecycle, "217", "215")
        lifecycle.transition("216", "kanboard", TicketState.READY)
        lifecycle.transition("216", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("216", "kanboard", TicketState.DONE)
        # #217 still in progress — parent must not complete yet.
        lifecycle.transition("217", "kanboard", TicketState.READY)
        lifecycle.transition("217", "kanboard", TicketState.IN_PROGRESS)

        await workflow._maybe_complete_parent("216")

        mock_kanban.mark_subtask_done.assert_awaited_once_with("215", "#216 ")
        assert lifecycle.get("215", "kanboard").state == TicketState.BLOCKED

    @pytest.mark.asyncio
    async def test_human_closing_reviewed_parent_marks_it_done(
        self, workflow, lifecycle, mock_kanban
    ):
        """A human dragging the reviewed parent to Done completes it
        directly — no git merge is attempted (the parent has no branch)."""
        lifecycle.get_or_create("220", "kanboard")  # parent
        lifecycle.human_transition("220", "kanboard", TicketState.BLOCKED)
        self._child(lifecycle, "221", "220")
        for c in ("221",):
            lifecycle.transition(c, "kanboard", TicketState.READY)
            lifecycle.transition(c, "kanboard", TicketState.IN_PROGRESS)
            lifecycle.transition(c, "kanboard", TicketState.DONE)
        await workflow._maybe_complete_parent("221")
        assert lifecycle.get("220", "kanboard").state == TicketState.WAITING_FOR_HUMAN

        event = _make_event({"ticket_id": "220", "provider": "kanboard"})
        await workflow._on_ticket_closed(event)

        rec = lifecycle.get("220", "kanboard")
        assert rec.state == TicketState.DONE
        mock_kanban.move_task_to_column.assert_any_call("220", "done")

    @pytest.mark.asyncio
    async def test_closing_parent_never_attempts_a_merge(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """A parent ticket has no branch — closing it must not call
        merge_to_main at all (which would fail and wrongly trigger the
        merge-conflict rebase-recovery flow meant for real branches)."""
        lifecycle.get_or_create("230", "kanboard")  # parent
        lifecycle.human_transition("230", "kanboard", TicketState.BLOCKED)
        self._child(lifecycle, "231", "230")
        lifecycle.transition("231", "kanboard", TicketState.READY)
        lifecycle.transition("231", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("231", "kanboard", TicketState.DONE)
        await workflow._maybe_complete_parent("231")

        event = _make_event({"ticket_id": "230", "provider": "kanboard"})
        await workflow._on_ticket_closed(event)

        mock_branch.merge_to_main.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_ticket_new_does_not_clobber_subticket_marker(
        self, workflow, lifecycle, mock_kanban
    ):
        """First-sight of a Marcus-created sub-ticket must NOT regenerate its
        acceptance criteria: doing so drops the `<!-- Sub-ticket of #N -->`
        marker, which strands the parent in BLOCKED forever once the children
        finish (regression guard for that path)."""
        lifecycle.get_or_create(
            "301", "kanboard",
            acceptance_criteria="- [ ] do X\n<!-- Sub-ticket of #300 -->",
        )
        # The child's board description uses a '## Acceptance Criteria'
        # heading, which ACParser.extract does NOT recognise — so without the
        # guard, _on_ticket_new would regenerate the AC and lose the marker.
        event = _make_event({
            "ticket_id": "301",
            "provider": "kanboard",
            "task": {
                "id": "301",
                "title": "Child",
                "description": (
                    "Do X\n\n## Acceptance Criteria\n- [ ] do X\n\n"
                    "_Sub-ticket of #300._"
                ),
                "status": "todo",
                "assignee": "0",
            },
        })
        workflow._generate_and_post_ac = AsyncMock()

        await workflow._on_ticket_new(event)

        rec = lifecycle.get("301", "kanboard")
        assert "<!-- Sub-ticket of #300 -->" in rec.acceptance_criteria
        assert workflow._parent_of(rec) == "300"
        workflow._generate_and_post_ac.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_ac_changed_preserves_subticket_marker(
        self, workflow, lifecycle
    ):
        """A human editing a sub-ticket's AC on the board (no marker in the
        edited text) must keep the parent marker so _parent_of still works."""
        lifecycle.get_or_create(
            "311", "kanboard",
            acceptance_criteria="- [ ] old\n<!-- Sub-ticket of #310 -->",
        )
        event = _make_event({
            "ticket_id": "311",
            "provider": "kanboard",
            "new_ac_text": "- [ ] new edited by human",
            "new_hash": "abc123",
        })

        await workflow._on_ac_changed(event)

        rec = lifecycle.get("311", "kanboard")
        assert "new edited by human" in rec.acceptance_criteria
        assert "<!-- Sub-ticket of #310 -->" in rec.acceptance_criteria
        assert workflow._parent_of(rec) == "310"


class TestReconcileBlockedParents:
    """Safety-net sweep: a BLOCKED parent whose children are ALL Done
    must be caught even when the event-driven _maybe_complete_parent
    trigger (fired when a child ticket closes) was missed entirely —
    a dropped webhook, a restart landing at the wrong moment, or any
    other gap."""

    def _child(self, lifecycle, tid, parent):
        lifecycle.get_or_create(
            tid, "kanboard",
            acceptance_criteria=f"- [ ] x\n<!-- Sub-ticket of #{parent} -->",
        )

    @pytest.mark.asyncio
    async def test_sweep_catches_a_missed_completion(
        self, workflow, lifecycle, mock_kanban
    ):
        """Children reach DONE WITHOUT _maybe_complete_parent ever being
        called for them (simulating a missed trigger) — the sweep alone
        must still move the parent to Waiting for Human."""
        lifecycle.get_or_create("230", "kanboard")  # parent
        lifecycle.human_transition("230", "kanboard", TicketState.BLOCKED)
        self._child(lifecycle, "231", "230")
        self._child(lifecycle, "232", "230")
        for c in ("231", "232"):
            lifecycle.transition(c, "kanboard", TicketState.READY)
            lifecycle.transition(c, "kanboard", TicketState.IN_PROGRESS)
            lifecycle.transition(c, "kanboard", TicketState.DONE)

        await workflow._reconcile_blocked_parents()

        rec = lifecycle.get("230", "kanboard")
        assert rec.state == TicketState.WAITING_FOR_HUMAN
        mock_kanban.move_task_to_column.assert_any_call("230", "waiting for human")

    @pytest.mark.asyncio
    async def test_sweep_respects_ai_gate(
        self, workflow, lifecycle, mock_kanban
    ):
        """The sweep goes through the same gate-aware completion path as
        the event-driven trigger — AI gate completes straight to Done."""
        workflow._get_effective_gate = AsyncMock(return_value="ai")
        lifecycle.get_or_create("233", "kanboard")  # parent
        lifecycle.human_transition("233", "kanboard", TicketState.BLOCKED)
        self._child(lifecycle, "234", "233")
        lifecycle.transition("234", "kanboard", TicketState.READY)
        lifecycle.transition("234", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("234", "kanboard", TicketState.DONE)

        await workflow._reconcile_blocked_parents()

        rec = lifecycle.get("233", "kanboard")
        assert rec.state == TicketState.DONE
        mock_kanban.move_task_to_column.assert_any_call("233", "done")

    @pytest.mark.asyncio
    async def test_sweep_ignores_parents_with_a_pending_child(
        self, workflow, lifecycle, mock_kanban
    ):
        lifecycle.get_or_create("235", "kanboard")
        lifecycle.human_transition("235", "kanboard", TicketState.BLOCKED)
        self._child(lifecycle, "236", "235")
        lifecycle.transition("236", "kanboard", TicketState.READY)
        lifecycle.transition("236", "kanboard", TicketState.IN_PROGRESS)
        # #236 not done yet.

        await workflow._reconcile_blocked_parents()

        assert lifecycle.get("235", "kanboard").state == TicketState.BLOCKED

    @pytest.mark.asyncio
    async def test_sweep_ignores_non_blocked_tickets(
        self, workflow, lifecycle, mock_kanban
    ):
        """A plain in-progress ticket (no children, not Blocked) is
        untouched — the sweep must not raise or misfire on it."""
        lifecycle.get_or_create("237", "kanboard")
        lifecycle.transition("237", "kanboard", TicketState.READY)
        lifecycle.transition("237", "kanboard", TicketState.IN_PROGRESS)

        await workflow._reconcile_blocked_parents()

        assert lifecycle.get("237", "kanboard").state == TicketState.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_sweep_ignores_a_blocked_ticket_with_no_children(
        self, workflow, lifecycle, mock_kanban
    ):
        """A ticket BLOCKED for an unrelated reason (e.g. a dependency,
        not decomposition) has no children and must be left alone."""
        lifecycle.get_or_create("238", "kanboard")
        lifecycle.human_transition("238", "kanboard", TicketState.BLOCKED)

        await workflow._reconcile_blocked_parents()

        assert lifecycle.get("238", "kanboard").state == TicketState.BLOCKED

    @pytest.mark.asyncio
    async def test_start_immediately_catches_a_stuck_parent(
        self, workflow, lifecycle, mock_kanban
    ):
        """A parent stuck in Blocked with all children already Done from
        BEFORE a restart is caught immediately by start() itself — not
        only after waiting a full sweep interval."""
        lifecycle.get_or_create("239", "kanboard")
        lifecycle.human_transition("239", "kanboard", TicketState.BLOCKED)
        self._child(lifecycle, "241", "239")
        lifecycle.transition("241", "kanboard", TicketState.READY)
        lifecycle.transition("241", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("241", "kanboard", TicketState.DONE)
        mock_kanban.get_task_by_id = AsyncMock(return_value=MagicMock())

        workflow._watcher.start = AsyncMock()
        try:
            await workflow.start()
        finally:
            await workflow.stop()

        assert lifecycle.get("239", "kanboard").state == TicketState.WAITING_FOR_HUMAN

    @pytest.mark.asyncio
    async def test_sweep_falls_back_to_kanboard_links_when_no_children_recognized(
        self, workflow, lifecycle, mock_kanban
    ):
        """Regression: decompose_ticket links every child unconditionally
        via create_task_link, independent of the separate AC-marker
        write _children_of relies on — a write that has, in production,
        lost a race and silently dropped the marker for EVERY child of a
        parent (now fixed at the source, but historical tickets can
        still be affected). When _children_of finds nothing at all, the
        sweep must fall back to Kanboard's own "is blocked by" links and
        their live column state."""
        lifecycle.get_or_create("250", "kanboard")  # parent, no recognized children
        lifecycle.human_transition("250", "kanboard", TicketState.BLOCKED)
        mock_kanban.get_task_links = AsyncMock(
            return_value={
                "depends_on": [
                    {"task_id": "251", "title": "Backend", "column": "Done"},
                    {"task_id": "252", "title": "Frontend", "column": "done"},
                ],
                "blocks": [],
                "relates_to": [],
            }
        )

        await workflow._reconcile_blocked_parents()

        rec = lifecycle.get("250", "kanboard")
        assert rec.state == TicketState.WAITING_FOR_HUMAN
        mock_kanban.get_task_links.assert_awaited_once_with("250")

    @pytest.mark.asyncio
    async def test_link_fallback_requires_every_linked_ticket_done(
        self, workflow, lifecycle, mock_kanban
    ):
        """The link fallback must not complete a parent early — ALL
        linked tickets must show a Done-like column, not just some."""
        lifecycle.get_or_create("253", "kanboard")
        lifecycle.human_transition("253", "kanboard", TicketState.BLOCKED)
        mock_kanban.get_task_links = AsyncMock(
            return_value={
                "depends_on": [
                    {"task_id": "254", "title": "Backend", "column": "Done"},
                    {"task_id": "255", "title": "Frontend", "column": "In Progress"},
                ],
                "blocks": [],
                "relates_to": [],
            }
        )

        await workflow._reconcile_blocked_parents()

        assert lifecycle.get("253", "kanboard").state == TicketState.BLOCKED

    @pytest.mark.asyncio
    async def test_sweep_skips_link_fallback_for_ordinary_dependency_blocks(
        self, workflow, lifecycle, mock_kanban
    ):
        """A ticket BLOCKED with a recorded blocker (record.blocked_by)
        is an ordinary dependency block (_resume_tickets_blocked_by
        already owns that), not a decompose parent — must never trigger
        the extra get_task_links RPC."""
        lifecycle.get_or_create("256", "kanboard")
        lifecycle.human_transition("256", "kanboard", TicketState.BLOCKED)
        lifecycle.set_blocked_by("256", "kanboard", "257")

        await workflow._reconcile_blocked_parents()

        mock_kanban.get_task_links.assert_not_awaited()
        assert lifecycle.get("256", "kanboard").state == TicketState.BLOCKED

    @pytest.mark.asyncio
    async def test_sweep_trusts_recognized_children_over_links(
        self, workflow, lifecycle, mock_kanban
    ):
        """A parent WITH at least one AC-marker-recognized child must
        never fall back to the links check at all, even if that child
        isn't done yet — the marker-based path is authoritative when it
        finds something."""
        lifecycle.get_or_create("258", "kanboard")
        lifecycle.human_transition("258", "kanboard", TicketState.BLOCKED)
        self._child(lifecycle, "259", "258")
        lifecycle.transition("259", "kanboard", TicketState.READY)
        lifecycle.transition("259", "kanboard", TicketState.IN_PROGRESS)
        # #259 not done yet.

        await workflow._reconcile_blocked_parents()

        mock_kanban.get_task_links.assert_not_awaited()
        assert lifecycle.get("258", "kanboard").state == TicketState.BLOCKED


class TestParentAutoCompleteEndToEnd:
    """Same guarantee as TestParentAutoComplete (all children Done -> parent
    to Waiting for Human), but driven through the REAL event handlers
    (_on_ticket_closed) instead of calling _maybe_complete_parent directly,
    so a bug in the surrounding event plumbing — not just the helper's own
    logic — would show up here."""

    async def _decompose_with(self, workflow, mock_kanban, lifecycle):
        """Run a 2-child decompose against a Ready parent owned by alice."""
        lifecycle.get_or_create("400", "kanboard")
        lifecycle.transition("400", "kanboard", TicketState.READY)
        lifecycle.set_assignee("400", "kanboard", "alice")
        lifecycle.update_acceptance_criteria(
            "400", "kanboard",
            "- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d", "hash",
        )

        async def fake_llm(prompt):
            return (
                '{"subtasks": [{"title": "Backend", "description": "api", '
                '"acceptance_criteria": "- [ ] api"}, {"title": "Frontend", '
                '"description": "ui", "acceptance_criteria": "- [ ] ui"}]}'
            )

        workflow._llm_generate = fake_llm
        created = {"n": 400}

        async def fake_create(data):
            created["n"] += 1
            t = MagicMock()
            t.id = str(created["n"])
            t.name = data["name"]
            return t

        mock_kanban.create_task = AsyncMock(side_effect=fake_create)
        mock_kanban.create_task_link = AsyncMock(return_value=True)
        mock_kanban.assign_task = AsyncMock(return_value=True)
        parent = MagicMock(description="do a lot")
        parent.name = "Big"
        parent.source_context = {"kanboard_task": {"project_id": 3}}
        parent.status = "ready"
        mock_kanban.get_task_by_id = AsyncMock(return_value=parent)
        return await workflow.decompose_ticket("400")

    @pytest.mark.asyncio
    async def test_blocked_parent_moves_to_waiting_for_human_via_real_events(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """Decompose a parent, drive both children to Done through the same
        _on_ticket_closed path a human's "drag card to Done" produces, and
        confirm the parent's board card is actually moved — not just its
        internal lifecycle record."""
        child_ids = await self._decompose_with(workflow, mock_kanban, lifecycle)
        assert len(child_ids) == 2
        assert lifecycle.get("400", "kanboard").state == TicketState.BLOCKED

        for cid in child_ids:
            lifecycle.transition(cid, "kanboard", TicketState.IN_PROGRESS)

        mock_kanban.move_task_to_column.reset_mock()

        # A human drags each child's card to Done. Kanboard fires this as a
        # column move (ticket.status_changed), NOT a task-close event — so
        # _on_status_changed, not _on_ticket_closed directly, is the real
        # entry point (see _on_status_changed's DONE branch).
        def _done_event(cid):
            return _make_event(
                {"ticket_id": cid, "provider": "kanboard", "new_status": "done"}
            )

        # First child closes: parent has one sub-ticket still open, stays put.
        await workflow._on_status_changed(_done_event(child_ids[0]))
        assert lifecycle.get("400", "kanboard").state == TicketState.BLOCKED
        assert mock_kanban.move_task_to_column.call_args_list[-1].args != (
            "400",
            "waiting for human",
        )

        # Second (last) child closes: parent must now move to WFH — both in
        # Marcus's own record AND on the board itself.
        await workflow._on_status_changed(_done_event(child_ids[1]))
        rec = lifecycle.get("400", "kanboard")
        assert rec.state == TicketState.WAITING_FOR_HUMAN
        mock_kanban.move_task_to_column.assert_any_call("400", "waiting for human")

    @pytest.mark.asyncio
    async def test_parent_wfh_move_failure_is_logged(
        self, workflow, lifecycle, mock_kanban, caplog
    ):
        """If the board has no 'Waiting for Human' column for this project,
        move_task_to_column returns False (no exception) and the parent's
        card silently stays in Blocked forever, even though Marcus's own
        record already says WAITING_FOR_HUMAN — exactly the "children are
        all done but the card never leaves Blocked" symptom a human sees
        on the board. That gap must be surfaced at WARNING, not swallowed
        (matches the same check already done for the parent's earlier move
        to Blocked in decompose_ticket)."""
        child_ids = await self._decompose_with(workflow, mock_kanban, lifecycle)
        for cid in child_ids:
            lifecycle.transition(cid, "kanboard", TicketState.IN_PROGRESS)
            lifecycle.transition(cid, "kanboard", TicketState.DONE)

        async def move(ticket_id, column):
            return not (ticket_id == "400" and column == "waiting for human")

        mock_kanban.move_task_to_column = AsyncMock(side_effect=move)

        import logging

        with caplog.at_level(logging.WARNING):
            await workflow._maybe_complete_parent(child_ids[0])
            await workflow._maybe_complete_parent(child_ids[1])

        # Internal lifecycle state still reaches WAITING_FOR_HUMAN
        # regardless of the board-side move outcome.
        assert lifecycle.get("400", "kanboard").state == TicketState.WAITING_FOR_HUMAN
        assert any(
            "400" in r.message and "waiting for human" in r.message.lower()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_parent_wfh_move_exception_logs_once_not_twice(
        self, workflow, lifecycle, mock_kanban, caplog
    ):
        """When move_task_to_column RAISES (e.g. an RPC timeout) rather
        than cleanly returning False, only the exception itself should be
        logged — not also the generic "no such column?" diagnostic, which
        is misleading for a transient error that has nothing to do with a
        missing column."""
        child_ids = await self._decompose_with(workflow, mock_kanban, lifecycle)
        for cid in child_ids:
            lifecycle.transition(cid, "kanboard", TicketState.IN_PROGRESS)
            lifecycle.transition(cid, "kanboard", TicketState.DONE)

        async def move(ticket_id, column):
            if ticket_id == "400" and column == "waiting for human":
                raise RuntimeError("kanboard RPC timeout")
            return True

        mock_kanban.move_task_to_column = AsyncMock(side_effect=move)

        import logging

        with caplog.at_level(logging.WARNING):
            await workflow._maybe_complete_parent(child_ids[0])
            await workflow._maybe_complete_parent(child_ids[1])

        assert lifecycle.get("400", "kanboard").state == TicketState.WAITING_FOR_HUMAN
        parent_warnings = [
            r for r in caplog.records
            if "400" in r.message and "waiting" in r.message.lower()
        ]
        assert len(parent_warnings) == 1
        assert "kanboard RPC timeout" in parent_warnings[0].message
        assert "does this project" not in parent_warnings[0].message


class TestActivityHeartbeat:
    """The board 'actively worked' highlight is driven by a liveness heartbeat
    (agent progress reports), decoupled from ticket state.

    get_working_ticket_ids() returns tickets an agent reported progress on
    within the activity window; a longer silence, or a terminal signal
    (done/blocked/waiting), drops the ticket from the set.
    """

    def test_mark_then_get_returns_ticket(self, workflow):
        """A just-marked ticket is reported as actively worked."""
        workflow._mark_progress_activity("7")
        assert "7" in workflow.get_working_ticket_ids()

    def test_clear_removes_ticket(self, workflow):
        """Clearing a heartbeat drops the ticket immediately."""
        workflow._mark_progress_activity("7")
        workflow._clear_progress_activity("7")
        assert "7" not in workflow.get_working_ticket_ids()

    def test_stale_entry_excluded_and_pruned(self, workflow):
        """A report older than the window is excluded and pruned from memory."""
        with patch(
            "src.workflows.human_gated_workflow.time.monotonic", return_value=1000.0
        ):
            workflow._mark_progress_activity("7")
        # 100s later — well past the ~40s window.
        with patch(
            "src.workflows.human_gated_workflow.time.monotonic", return_value=1100.0
        ):
            assert workflow.get_working_ticket_ids() == []
        # Pruned: the internal map no longer holds the stale key.
        assert workflow._progress_activity == {}

    def test_within_window_still_active(self, workflow):
        """A recent report (inside the window) still counts as active."""
        with patch(
            "src.workflows.human_gated_workflow.time.monotonic", return_value=1000.0
        ):
            workflow._mark_progress_activity("7")
        with patch(
            "src.workflows.human_gated_workflow.time.monotonic", return_value=1010.0
        ):  # 10s later
            assert workflow.get_working_ticket_ids() == ["7"]

    def test_only_this_providers_tickets(self, workflow):
        """The returned ids are bare ticket ids for this workflow's provider."""
        workflow._mark_progress_activity("42")
        ids = workflow.get_working_ticket_ids()
        assert ids == ["42"]  # not "kanboard:42"

    @pytest.mark.asyncio
    async def test_report_progress_marks_activity(self, workflow, lifecycle):
        """Posting a progress report stamps the ticket's heartbeat."""
        lifecycle.get_or_create("50", "kanboard")
        await workflow.report_progress("50", 50, "halfway")
        assert "50" in workflow.get_working_ticket_ids()

    @pytest.mark.asyncio
    async def test_set_blocked_clears_activity(self, workflow, lifecycle):
        """A blocked signal clears the heartbeat so the highlight drops."""
        lifecycle.get_or_create("60", "kanboard")
        lifecycle.transition("60", "kanboard", TicketState.READY)
        lifecycle.transition("60", "kanboard", TicketState.IN_PROGRESS)
        workflow._mark_progress_activity("60")
        assert "60" in workflow.get_working_ticket_ids()

        await workflow.set_blocked("60", blocked_by="dep #61")

        assert "60" not in workflow.get_working_ticket_ids()


class TestClassifyReportIntentLenient:
    """Report classification tolerates common phrasings, guards negation."""

    def test_explicit_prefixes(self, workflow):
        assert workflow._classify_report_intent("DONE - shipped") == "done"
        assert workflow._classify_report_intent("BLOCKED - need key") == "blocked"
        assert workflow._classify_report_intent("waiting on API") == "waiting"

    def test_lenient_completion_phrasings(self, workflow):
        for r in (
            "Finished implementing all the acceptance criteria",
            "All acceptance criteria met and tested",
            "The implementation is complete",
            "I'm done with the feature",
            "everything is done",
        ):
            assert workflow._classify_report_intent(r) == "done", r

    def test_negated_completion_is_progress(self, workflow):
        for r in (
            "not done yet, still writing tests",
            "the implementation isn't complete",
            "still working on the acceptance criteria",
        ):
            assert workflow._classify_report_intent(r) == "progress", r

    def test_plain_progress_stays_progress(self, workflow):
        assert workflow._classify_report_intent("wrote the login form") == "progress"

    def test_lenient_blocked_and_waiting(self, workflow):
        assert workflow._classify_report_intent("I'm blocked on the DB schema") == "blocked"
        assert workflow._classify_report_intent("I need human input on the design") == "waiting"


class TestHandleBranchPush:
    """A Gitea push to a ticket branch posts a 'commits pushed' comment and
    keeps the ticket's liveness heartbeat lit."""

    @pytest.mark.asyncio
    async def test_posts_comment_and_marks_activity(self, workflow, lifecycle, mock_kanban):
        rec = lifecycle.get_or_create("5", "kanboard")
        rec.branch_name = "ticket/kanboard/5"

        ok = await workflow.handle_branch_push(
            "ticket/kanboard/5", ["add login form", "wire up validation"]
        )

        assert ok is True
        assert mock_kanban.add_comment.await_count >= 1
        assert "5" in workflow.get_working_ticket_ids()

    @pytest.mark.asyncio
    async def test_empty_commits_is_noop(self, workflow, lifecycle):
        rec = lifecycle.get_or_create("6", "kanboard")
        setattr(rec, "branch_name", "ticket/kanboard/6")
        assert await workflow.handle_branch_push("ticket/kanboard/6", []) is False

    @pytest.mark.asyncio
    async def test_no_matching_ticket_is_noop(self, workflow):
        assert await workflow.handle_branch_push("ticket/kanboard/999", ["x"]) is False

    @pytest.mark.asyncio
    async def test_done_ticket_is_skipped(self, workflow, lifecycle):
        rec = lifecycle.get_or_create("8", "kanboard")
        setattr(rec, "branch_name", "ticket/kanboard/8")
        lifecycle.transition("8", "kanboard", TicketState.READY)
        lifecycle.transition("8", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("8", "kanboard", TicketState.DONE)
        assert await workflow.handle_branch_push("ticket/kanboard/8", ["x"]) is False

    @pytest.mark.asyncio
    async def test_syncs_marcus_local_clone_before_the_webhook_refresh(
        self, workflow, lifecycle, mock_branch
    ):
        """A push must pull the new commits into Marcus's OWN local clone,
        not just post a comment — GiteaWebhookReceiver calls this handler
        (on_commits) BEFORE DevEnvironmentManager.refresh_by_branch(), and
        that refresh's container-side `git fetch origin` reaches Marcus's
        local clone (a bind-mount), not Gitea directly. Skipping this sync
        left an already-open preview's hot reload resetting to the same
        stale commit it already had — the change a human just requested
        via a "waiting for human" comment never appeared on reload."""
        rec = lifecycle.get_or_create("9", "kanboard")
        rec.branch_name = "ticket/kanboard/9"

        await workflow.handle_branch_push("ticket/kanboard/9", ["fix the button copy"])

        mock_branch.sync_branch.assert_awaited_once_with("ticket/kanboard/9")


class TestAgentPresenceAndUsage:
    """Connected (polling) vs active (working) agent presence, and the
    account-level subscription usage agents self-report via marcus_work."""

    def test_connected_includes_polling_agent(self, workflow):
        workflow._mark_agent_seen("worker-A")
        assert "worker-A" in workflow.get_connected_agent_ids()

    def test_connected_prunes_stale(self, workflow):
        with patch(
            "src.workflows.human_gated_workflow.time.monotonic", return_value=1000.0
        ):
            workflow._mark_agent_seen("worker-A")
        with patch(
            "src.workflows.human_gated_workflow.time.monotonic", return_value=1100.0
        ):
            assert workflow.get_connected_agent_ids() == []
        assert workflow._agent_seen == {}

    @pytest.mark.asyncio
    async def test_poll_counts_as_connected_even_with_no_work(self, workflow):
        """An agent that polls but gets no ticket is still 'connected'."""
        res = await workflow.orchestrate_work(agent_id="worker-A")
        assert res["status"] == "no_work"
        assert "worker-A" in workflow.get_connected_agent_ids()

    def test_active_agents_are_connected_workers_of_live_tickets(
        self, workflow, lifecycle
    ):
        rec = lifecycle.get_or_create("5", "kanboard")
        rec.ai_agent_id = "worker-A"          # claimed
        workflow._mark_agent_seen("worker-A")  # connected (polling)
        workflow._mark_progress_activity("5")  # live progress heartbeat → working
        assert workflow.get_active_agent_ids() == ["worker-A"]

    def test_working_but_not_connected_is_not_active(self, workflow, lifecycle):
        """A claimer working a live ticket but NOT polling marcus_work (e.g. an
        internal marcus-<slot> reservation) is not counted as an active agent —
        active must be a connected agent."""
        rec = lifecycle.get_or_create("7", "kanboard")
        rec.ai_agent_id = "marcus-slot0"      # internal claim, never polls
        workflow._mark_progress_activity("7")  # ticket has a live heartbeat
        assert workflow.get_active_agent_ids() == []

    def test_progress_activity_marks_real_agent_connected(
        self, workflow, lifecycle
    ):
        """A real agent whose ticket is kept live only by progress/push
        activity (it never separately polled marcus_work) still counts as
        connected AND active.

        Regression: the golden 'working' ring is refreshed by branch pushes
        (via the Gitea webhook) and progress reports, but the connected/
        working counts used to come only from marcus_work polls — so an
        agent that committed and pushed lit the ring yet showed 0 connected
        / 0 working. Marking progress must also mark the owning real agent
        connected.
        """
        rec = lifecycle.get_or_create("8", "kanboard")
        rec.ai_agent_id = "worker-C"           # a real agent, claimed
        # ONLY progress activity — no explicit _mark_agent_seen("worker-C").
        workflow._mark_progress_activity("8")
        assert "worker-C" in workflow.get_connected_agent_ids()
        assert workflow.get_active_agent_ids() == ["worker-C"]

    def test_claimed_but_idle_ticket_is_not_active(self, workflow, lifecycle):
        rec = lifecycle.get_or_create("6", "kanboard")
        rec.ai_agent_id = "worker-B"          # claimed but NO progress heartbeat
        workflow._mark_agent_seen("worker-B")  # connected, but not working
        assert workflow.get_active_agent_ids() == []

    def test_usage_shared_across_one_account(self, workflow):
        workflow._record_agent_usage(
            "worker-A", {"account": "team@x", "used": 10, "limit": 50, "unit": "M"}
        )
        workflow._record_agent_usage(
            "worker-B", {"account": "team@x", "used": 12, "limit": 50}
        )
        ua = workflow.usage_for_agent("worker-A")
        ub = workflow.usage_for_agent("worker-B")
        assert ua == ub                       # same account → same figure
        assert ua["used"] == 12 and ua["limit"] == 50

    def test_different_accounts_stay_separate(self, workflow):
        """Agents from DIFFERENT subscription accounts keep separate figures —
        each ticket shows only its own agent's account usage, never mixed."""
        workflow._record_agent_usage(
            "worker-A", {"account": "acct-1", "used": 10, "limit": 50}
        )
        workflow._record_agent_usage(
            "worker-B", {"account": "acct-2", "used": 30, "limit": 100}
        )
        ua = workflow.usage_for_agent("worker-A")
        ub = workflow.usage_for_agent("worker-B")
        assert ua["used"] == 10 and ua["limit"] == 50
        assert ub["used"] == 30 and ub["limit"] == 100
        assert ua != ub

    def test_usage_unlimited_keeps_none_limit(self, workflow):
        workflow._record_agent_usage(
            "worker-A", {"account": "local", "used": 0, "limit": None}
        )
        assert workflow.usage_for_agent("worker-A")["limit"] is None

    def test_usage_none_for_unknown_agent(self, workflow):
        assert workflow.usage_for_agent("nobody") is None

    def test_malformed_usage_is_ignored(self, workflow):
        workflow._record_agent_usage("worker-A", "not a dict")
        assert workflow.usage_for_agent("worker-A") is None

    @pytest.mark.asyncio
    async def test_orchestrate_records_reported_usage(self, workflow):
        await workflow.orchestrate_work(
            agent_id="worker-A",
            usage={"account": "team@x", "used": 5, "limit": 50},
        )
        assert workflow.usage_for_agent("worker-A")["used"] == 5


class TestUsageSanitization:
    """The agent-supplied `usage` payload is untrusted — sanitize it on ingest
    (safe JSON scalars, capped lengths, bounded account count) so a crafted or
    buggy agent can't crash Marcus, bloat the API, or exhaust memory."""

    def test_scalar_accepts_numbers_and_strings(self):
        from src.workflows.human_gated_workflow import _safe_usage_scalar
        assert _safe_usage_scalar(12) == 12
        assert _safe_usage_scalar(1.5) == 1.5
        assert _safe_usage_scalar("45%") == "45%"

    def test_scalar_rejects_unsafe_values(self):
        from src.workflows.human_gated_workflow import _safe_usage_scalar
        assert _safe_usage_scalar(True) is None          # bool
        assert _safe_usage_scalar(float("inf")) is None   # not finite
        assert _safe_usage_scalar(float("nan")) is None
        assert _safe_usage_scalar({"x": 1}) is None        # dict
        assert _safe_usage_scalar([1, 2]) is None          # list
        assert _safe_usage_scalar(None) is None

    def test_scalar_caps_string_length(self):
        from src.workflows.human_gated_workflow import (
            _safe_usage_scalar, _USAGE_SCALAR_MAX,
        )
        assert len(_safe_usage_scalar("x" * 5000)) == _USAGE_SCALAR_MAX

    def test_record_sanitizes_stored_values(self, workflow):
        workflow._record_agent_usage("worker-A", {
            "account": "a",
            "used": float("inf"),                       # → None
            "limit": True,                              # → None
            "unit": "<script>alert(1)</script>" * 20,   # → capped string
        })
        u = workflow.usage_for_agent("worker-A")
        assert u["used"] is None
        assert u["limit"] is None
        assert isinstance(u["unit"], str) and len(u["unit"]) <= 64

    def test_record_caps_account_id_length(self, workflow):
        from src.workflows.human_gated_workflow import _ACCOUNT_ID_MAX
        workflow._record_agent_usage("worker-A", {"account": "z" * 5000, "used": 1})
        assert all(len(k) <= _ACCOUNT_ID_MAX for k in workflow._account_usage)

    def test_record_bounds_number_of_accounts(self, workflow):
        from src.workflows.human_gated_workflow import _MAX_TRACKED_ACCOUNTS
        for i in range(_MAX_TRACKED_ACCOUNTS + 25):
            workflow._record_agent_usage(
                "agent-%d" % i, {"account": "acct-%d" % i, "used": i}
            )
        assert len(workflow._account_usage) <= _MAX_TRACKED_ACCOUNTS

    def test_non_dict_usage_is_ignored(self, workflow):
        workflow._record_agent_usage("worker-A", ["not", "a", "dict"])
        assert workflow.usage_for_agent("worker-A") is None

    @pytest.mark.asyncio
    async def test_orchestrate_caps_agent_id_length(self, workflow):
        from src.workflows.human_gated_workflow import _ACCOUNT_ID_MAX
        res = await workflow.orchestrate_work(agent_id="A" * 5000)
        assert len(res["agent_id"]) <= _ACCOUNT_ID_MAX


class TestProjectAccessGate:
    """A ticket's Kanboard project must be explicitly enabled for Marcus (via
    ProjectAccessSettingManager) before Marcus — or any AI agent — claims or
    starts work on it. Default is OFF: an unconfigured project is refused.
    """

    def _wire_task_project(self, mock_kanban, project_id):
        """Make get_task_by_id resolve to a task in *project_id*."""
        task = MagicMock()
        task.source_context = {"kanboard_task": {"project_id": project_id}}
        mock_kanban.get_task_by_id = AsyncMock(return_value=task)

    @pytest.mark.asyncio
    async def test_disabled_project_refuses_to_start(
        self, workflow, lifecycle, mock_kanban, mock_branch, mock_project_access
    ):
        """A ticket in a project that is NOT enabled never gets a branch,
        a claim, or a 'Started' comment."""
        self._wire_task_project(mock_kanban, 9)
        mock_project_access.is_enabled = MagicMock(return_value=False)
        lifecycle.get_or_create("30", "kanboard")
        lifecycle.transition("30", "kanboard", TicketState.READY)
        lifecycle.set_assignee("30", "kanboard", "alice")

        await workflow._start_ai_work("30", lifecycle.get("30", "kanboard"))

        mock_branch.create_branch.assert_not_called()
        rec = lifecycle.get("30", "kanboard")
        assert rec.ai_agent_id is None
        assert rec.state != TicketState.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_enabled_project_starts_normally(
        self, workflow, lifecycle, mock_kanban, mock_branch, mock_project_access
    ):
        """An explicitly enabled project's ticket starts exactly as before."""
        self._wire_task_project(mock_kanban, 9)
        mock_project_access.is_enabled = MagicMock(return_value=True)
        lifecycle.get_or_create("31", "kanboard")
        lifecycle.transition("31", "kanboard", TicketState.READY)
        lifecycle.set_assignee("31", "kanboard", "alice")

        # Unrelated downstream gate (does the project have a known tech
        # stack?) — satisfy it so the test can observe MY gate specifically,
        # not get stopped one step further down for a different reason.
        with patch(
            "src.core.project_description.ProjectDescriptionManager.get_stack",
            return_value={"language": "python"},
        ):
            await workflow._start_ai_work("31", lifecycle.get("31", "kanboard"))

        mock_branch.create_branch.assert_called_once()
        rec = lifecycle.get("31", "kanboard")
        assert rec.state == TicketState.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_unresolvable_project_does_not_block(
        self, workflow, lifecycle, mock_kanban, mock_branch, mock_project_access
    ):
        """When the ticket's project can't be determined (non-Kanboard
        provider, RPC failure — get_task_by_id returns None), the access
        gate does not apply; existing single-project/non-Kanboard
        deployments are unaffected."""
        mock_kanban.get_task_by_id = AsyncMock(return_value=None)
        mock_project_access.is_enabled = MagicMock(return_value=False)
        lifecycle.get_or_create("32", "kanboard")
        lifecycle.transition("32", "kanboard", TicketState.READY)
        lifecycle.set_assignee("32", "kanboard", "alice")

        await workflow._start_ai_work("32", lifecycle.get("32", "kanboard"))

        mock_branch.create_branch.assert_called_once()

    @pytest.mark.asyncio
    async def test_orchestrate_skips_disabled_project_ticket(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """_next_worker_ticket (via orchestrate_work) skips a disabled-
        project ticket that sorts first and hands out the next eligible
        one instead of starving the agent forever."""

        async def fake_get_task(ticket_id):
            task = MagicMock()
            pid = 9 if ticket_id == "40" else 11
            task.source_context = {"kanboard_task": {"project_id": pid}}
            return task

        mock_kanban.get_task_by_id = AsyncMock(side_effect=fake_get_task)
        mock_project_access.is_enabled = MagicMock(side_effect=lambda pid: pid == 11)

        lifecycle.get_or_create("40", "kanboard")  # project 9 — disabled
        lifecycle.transition("40", "kanboard", TicketState.READY)
        lifecycle.set_assignee("40", "kanboard", "alice")

        lifecycle.get_or_create("41", "kanboard")  # project 11 — enabled
        lifecycle.transition("41", "kanboard", TicketState.READY)
        lifecycle.set_assignee("41", "kanboard", "bob")

        with patch(
            "src.core.project_description.ProjectDescriptionManager.get_stack",
            return_value={"language": "python"},
        ):
            result = await workflow.orchestrate_work(agent_id="worker-Z")

        assert result["ticket_id"] == "41"
        assert lifecycle.get("40", "kanboard").ai_agent_id is None

    @pytest.mark.asyncio
    async def test_orchestrate_reports_no_work_when_all_disabled(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """Every candidate disabled → orchestrate_work reports no_work
        rather than getting stuck retrying a ticket it will never start."""
        self._wire_task_project(mock_kanban, 9)
        mock_project_access.is_enabled = MagicMock(return_value=False)
        lifecycle.get_or_create("42", "kanboard")
        lifecycle.transition("42", "kanboard", TicketState.READY)
        lifecycle.set_assignee("42", "kanboard", "alice")

        result = await workflow.orchestrate_work(agent_id="worker-Y")

        assert result["status"] == "no_work"

    @pytest.mark.asyncio
    async def test_marcus_work_rescans_the_board_before_handing_out(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """Each marcus_work poll re-reads the enabled boards first.

        _next_worker_ticket selects from lifecycle records, which are only
        populated by BoardWatcher's own timer (30s by default) and by
        webhooks. An agent polling every ~10s would otherwise wait up to a
        full watcher interval before a ticket a human just moved to Ready
        became visible — and if webhooks aren't reaching Marcus, that delay
        is the only thing standing between "assigned and Ready" and "handed
        to an agent".
        """
        workflow._watcher = MagicMock()
        workflow._watcher.poll_once = AsyncMock()

        await workflow.orchestrate_work(agent_id="worker-scan")

        workflow._watcher.poll_once.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_board_rescan_failure_does_not_break_handout(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """A board read that fails must not stop Marcus handing out work it
        already knows about — the rescan is an optimisation, not a
        precondition."""
        self._wire_task_project(mock_kanban, 9)
        mock_project_access.is_enabled = MagicMock(return_value=True)
        workflow._watcher = MagicMock()
        workflow._watcher.poll_once = AsyncMock(
            side_effect=RuntimeError("kanboard unreachable")
        )
        lifecycle.get_or_create("70", "kanboard")
        lifecycle.transition("70", "kanboard", TicketState.READY)
        lifecycle.set_assignee("70", "kanboard", "alice")

        with patch(
            "src.core.project_description.ProjectDescriptionManager.get_stack",
            return_value={"language": "python"},
        ):
            result = await workflow.orchestrate_work(agent_id="worker-scan2")

        assert result["status"] == "assigned"
        assert result["ticket_id"] == "70"

    @pytest.mark.asyncio
    async def test_withheld_message_names_the_project_not_just_its_id(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """A bare numeric project id is unactionable.

        Kanboard shows humans project NAMES, never ids, so "enable Kanboard
        project 7" cannot be mapped to a board — a human who has already
        enabled a different project reasonably reads it as Marcus being
        wrong rather than as a second, different project needing the
        toggle. Name the project.
        """
        self._wire_task_project(mock_kanban, 7)
        mock_kanban.get_project_name = AsyncMock(return_value="Website Rebuild")
        mock_project_access.is_enabled = MagicMock(return_value=False)
        lifecycle.get_or_create("21", "kanboard")
        lifecycle.transition("21", "kanboard", TicketState.READY)
        lifecycle.set_assignee("21", "kanboard", "alice")

        result = await workflow.orchestrate_work(agent_id="worker-C")

        msg = result["message"]
        assert "Website Rebuild" in msg
        assert "7" in msg

    @pytest.mark.asyncio
    async def test_withheld_tickets_are_grouped_per_project(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """Eight tickets blocked on ONE toggle must read as one instruction,
        not eight repetitions of the same sentence."""
        self._wire_task_project(mock_kanban, 7)
        mock_kanban.get_project_name = AsyncMock(return_value="Website Rebuild")
        mock_project_access.is_enabled = MagicMock(return_value=False)
        for tid in [str(n) for n in range(21, 29)]:
            lifecycle.get_or_create(tid, "kanboard")
            lifecycle.transition(tid, "kanboard", TicketState.READY)
            lifecycle.set_assignee(tid, "kanboard", "alice")

        result = await workflow.orchestrate_work(agent_id="worker-D")

        msg = result["message"]
        # One line for the project, listing the tickets — not one per ticket.
        assert msg.count("is not enabled for Marcus") == 1
        for tid in ("21", "28"):
            assert f"#{tid}" in msg

    @pytest.mark.asyncio
    async def test_withheld_reasons_surface_unassigned_ready_tickets(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """A Ready ticket nobody has assigned yet must be named in the
        no_work message — not silently omitted.

        Without this, an agent polling marcus_work sees only "no tickets
        are ready right now" even though a real, ready ticket exists —
        the ONLY reason it isn't handed out is that no human has claimed
        it. An agent with no visibility into that has to go dig through
        other tool calls to find the ticket and guess why, which in
        practice led to a wrong diagnosis (confusing the unrelated
        per-ticket gate_mode setting for the real cause)."""
        self._wire_task_project(mock_kanban, 7)
        lifecycle.get_or_create("39", "kanboard")
        lifecycle.transition("39", "kanboard", TicketState.READY)
        # No set_assignee() call — genuinely unassigned.

        result = await workflow.orchestrate_work(agent_id="worker-F")

        msg = result["message"]
        assert "#39" in msg
        assert "no assignee" in msg.lower() or "unassigned" in msg.lower()

    @pytest.mark.asyncio
    async def test_withheld_reasons_ignore_unassigned_ticket_in_disabled_project(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """An unassigned Ready ticket in a project Marcus isn't even
        enabled for must not be surfaced — the actionable instruction
        there is "enable the project", not "assign the ticket", and the
        disabled-project reason already covers assigned tickets in that
        same project."""
        self._wire_task_project(mock_kanban, 7)
        mock_project_access.is_enabled = MagicMock(return_value=False)
        lifecycle.get_or_create("40", "kanboard")
        lifecycle.transition("40", "kanboard", TicketState.READY)

        result = await workflow.orchestrate_work(agent_id="worker-G")

        assert "#40" not in result["message"]

    @pytest.mark.asyncio
    async def test_project_id_lookup_is_cached_per_ticket(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """A ticket's project is resolved once, not on every poll.

        _withheld_ticket_reasons and _next_worker_ticket each resolve every
        assigned ticket's project. With a handful of blocked tickets and an
        agent polling every ~10s that is a steady stream of getTask calls
        against Kanboard's SQLite backend — the same write contention that
        surfaces as "database is locked". A ticket does not change project
        in practice, so resolve it once and remember it.
        """
        self._wire_task_project(mock_kanban, 7)
        mock_kanban.get_project_name = AsyncMock(return_value="Website Rebuild")
        mock_project_access.is_enabled = MagicMock(return_value=False)
        lifecycle.get_or_create("21", "kanboard")
        lifecycle.transition("21", "kanboard", TicketState.READY)
        lifecycle.set_assignee("21", "kanboard", "alice")

        await workflow.orchestrate_work(agent_id="worker-E")
        first = mock_kanban.get_task_by_id.await_count
        await workflow.orchestrate_work(agent_id="worker-E")
        second = mock_kanban.get_task_by_id.await_count

        # The second poll must not re-resolve the same ticket's project.
        assert second == first

    @pytest.mark.asyncio
    async def test_refused_ticket_still_mirrors_the_board_state(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """A ticket refused for a disabled project must still have its
        lifecycle record synced to the board's column.

        _next_worker_ticket only ever considers READY/IN_PROGRESS records.
        Leaving the record at TODO while the board says Ready makes the
        ticket invisible to every worker FOREVER — including after a human
        toggles the project on, because nothing re-checks it: the board
        column does not change again, so no further status event fires.
        Mirroring the column keeps the ticket a valid candidate the moment
        the block is lifted.
        """
        self._wire_task_project(mock_kanban, 9)
        mock_project_access.is_enabled = MagicMock(return_value=False)
        lifecycle.get_or_create("60", "kanboard")
        lifecycle.set_assignee("60", "kanboard", "alice")

        await workflow._on_status_changed(
            _make_event(
                {
                    "ticket_id": "60",
                    "provider": "kanboard",
                    "old_status": "todo",
                    "new_status": "ready",
                }
            )
        )

        rec = lifecycle.get("60", "kanboard")
        assert rec.state == TicketState.READY  # mirrors the board
        assert rec.ai_agent_id is None  # but was NOT claimed

    @pytest.mark.asyncio
    async def test_ticket_is_picked_up_once_the_project_is_enabled(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """The whole point of mirroring: after the human toggles Marcus on,
        the next marcus_work poll hands the ticket out with no further
        board activity needed."""
        self._wire_task_project(mock_kanban, 9)
        mock_project_access.is_enabled = MagicMock(return_value=False)
        lifecycle.get_or_create("61", "kanboard")
        lifecycle.set_assignee("61", "kanboard", "alice")

        await workflow._on_status_changed(
            _make_event(
                {
                    "ticket_id": "61",
                    "provider": "kanboard",
                    "old_status": "todo",
                    "new_status": "ready",
                }
            )
        )
        first = await workflow.orchestrate_work(agent_id="worker-A")
        assert first["status"] == "no_work"

        # Human flips the toggle on. No board event follows.
        mock_project_access.is_enabled = MagicMock(return_value=True)
        with patch(
            "src.core.project_description.ProjectDescriptionManager.get_stack",
            return_value={"language": "python"},
        ):
            second = await workflow.orchestrate_work(agent_id="worker-A")

        assert second["status"] == "assigned"
        assert second["ticket_id"] == "61"

    @pytest.mark.asyncio
    async def test_no_work_message_explains_a_disabled_project(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """'No tickets are ready right now' is actively misleading when a
        ticket IS ready and Marcus is simply not allowed to touch its
        project. The agent's reply must name the ticket and the reason, or
        the human has nothing to act on — the refusal is otherwise only an
        INFO log line inside the container."""
        self._wire_task_project(mock_kanban, 9)
        mock_project_access.is_enabled = MagicMock(return_value=False)
        lifecycle.get_or_create("62", "kanboard")
        lifecycle.transition("62", "kanboard", TicketState.READY)
        lifecycle.set_assignee("62", "kanboard", "alice")

        result = await workflow.orchestrate_work(agent_id="worker-B")

        assert result["status"] == "no_work"
        msg = result["message"]
        assert "62" in msg
        assert "not enabled" in msg.lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,payload",
        [
            ("_on_comment_added", {"comment_body": "please change it"}),
            ("_on_ticket_closed", {}),
            ("_on_ticket_reopened", {}),
            ("_on_ac_changed", {"new_ac_text": "- [ ] x"}),
        ],
    )
    async def test_disabled_project_tickets_are_seen_but_never_touched(
        self, workflow, lifecycle, mock_kanban, mock_project_access,
        handler, payload,
    ):
        """Marcus reads every board, so events arrive for projects it is
        NOT allowed to act on — and these handlers all write to the board
        (comments, column moves, merges to main). Reading a disabled
        project must never turn into touching it.
        """
        self._wire_task_project(mock_kanban, 9)
        mock_project_access.is_enabled = MagicMock(return_value=False)
        lifecycle.get_or_create("80", "kanboard")
        lifecycle.transition("80", "kanboard", TicketState.READY)
        lifecycle.transition("80", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("80", "kanboard", "alice")
        mock_kanban.add_comment.reset_mock()
        mock_kanban.move_task_to_column.reset_mock()

        event = _make_event(
            {"ticket_id": "80", "provider": "kanboard", **payload}
        )
        await getattr(workflow, handler)(event)

        mock_kanban.add_comment.assert_not_called()
        mock_kanban.move_task_to_column.assert_not_called()

    @pytest.mark.asyncio
    async def test_enabled_project_comment_still_handled(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """Sanity check: the gate must not deafen an ENABLED project."""
        self._wire_task_project(mock_kanban, 9)
        mock_project_access.is_enabled = MagicMock(return_value=True)
        lifecycle.get_or_create("81", "kanboard")
        lifecycle.transition("81", "kanboard", TicketState.READY)
        lifecycle.transition("81", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("81", "kanboard", TicketState.WAITING_FOR_HUMAN)
        lifecycle.set_assignee("81", "kanboard", "alice")
        mock_kanban.add_comment.reset_mock()

        await workflow._on_comment_added(
            _make_event({
                "ticket_id": "81", "provider": "kanboard",
                "comment_body": "please change it", "comment_author": "alice",
            })
        )

        mock_kanban.add_comment.assert_called()

    @pytest.mark.asyncio
    async def test_disabled_project_does_not_generate_ac_on_new_ticket(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """A brand-new ticket in a disabled project must not get an
        AI-generated acceptance-criteria comment or description edit —
        _on_ticket_new's AC-generation path is a Kanboard write (add_comment
        + update_task) just like _start_ai_work's branch/claim, and must be
        gated the same way."""
        self._wire_task_project(mock_kanban, 9)
        mock_project_access.is_enabled = MagicMock(return_value=False)
        workflow._generate_and_post_ac = AsyncMock()
        event = _make_event(
            {
                "ticket_id": "50",
                "provider": "kanboard",
                "task": {
                    "id": "50",
                    "title": "New ticket, disabled project",
                    "description": "",
                    "status": "todo",
                },
            }
        )

        await workflow._on_ticket_new(event)

        workflow._generate_and_post_ac.assert_not_called()

    @pytest.mark.asyncio
    async def test_enabled_project_generates_ac_on_new_ticket(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """Sanity check: an enabled project's new ticket still gets AC
        generated as before (the gate must not block the normal path)."""
        self._wire_task_project(mock_kanban, 11)
        mock_project_access.is_enabled = MagicMock(return_value=True)
        workflow._generate_and_post_ac = AsyncMock()
        event = _make_event(
            {
                "ticket_id": "51",
                "provider": "kanboard",
                "task": {
                    "id": "51",
                    "title": "New ticket, enabled project",
                    "description": "",
                    "status": "todo",
                },
            }
        )

        await workflow._on_ticket_new(event)

        workflow._generate_and_post_ac.assert_called_once()

    @pytest.mark.asyncio
    async def test_orchestrate_never_hands_out_a_disabled_project_ticket(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """Belt-and-suspenders: orchestrate_work itself must refuse to hand
        out a disabled-project ticket, no matter how ``next_id`` was
        produced — not just rely on ``_next_worker_ticket``'s own filter.

        The concrete gap this closes: ``_next_worker_ticket`` checks
        is_enabled per candidate and returns a ticket only once that check
        passes, but orchestrate_work does not act on it immediately.
        ``decompose_ticket`` runs first (an LLM call — seconds, not
        microseconds) and, if the ticket turns out already IN_PROGRESS
        under an internal 'marcus-' slot claim, the hand-out used to skip
        straight to ``claim_ticket`` — bypassing ``_start_ai_work``'s own
        re-check entirely. A human has a real window to disable the
        project in between.
        """
        self._wire_task_project(mock_kanban, 9)
        mock_project_access.is_enabled = MagicMock(return_value=False)
        lifecycle.get_or_create("60", "kanboard")
        lifecycle.transition("60", "kanboard", TicketState.READY)
        lifecycle.set_assignee("60", "kanboard", "alice")
        # Simulate _next_worker_ticket having selected this ticket before
        # the project was disabled — the invariant must hold regardless.
        workflow._next_worker_ticket = AsyncMock(return_value="60")

        result = await workflow.orchestrate_work(agent_id="worker-Z")

        assert result["status"] != "working"
        assert result.get("ticket_id") != "60"
        rec = lifecycle.get("60", "kanboard")
        assert rec.ai_agent_id is None

    @pytest.mark.asyncio
    async def test_reclaim_branch_refuses_a_disabled_project_ticket(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """The reclaim branch specifically (ticket already IN_PROGRESS
        under an internal 'marcus-' slot claim from the human-gated
        auto-start path) must not hand the claim to a worker once the
        project has been disabled."""
        self._wire_task_project(mock_kanban, 9)
        mock_project_access.is_enabled = MagicMock(return_value=False)
        lifecycle.get_or_create("61", "kanboard")
        lifecycle.transition("61", "kanboard", TicketState.READY)
        lifecycle.transition("61", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("61", "kanboard", "alice")
        lifecycle.claim_ticket("61", "kanboard", "marcus-slot-0")
        workflow._next_worker_ticket = AsyncMock(return_value="61")

        result = await workflow.orchestrate_work(agent_id="worker-Z")

        assert result.get("ticket_id") != "61"
        rec = lifecycle.get("61", "kanboard")
        assert rec.ai_agent_id != "worker-Z"

    @pytest.mark.asyncio
    async def test_reclaim_branch_refuses_when_disabled_during_decompose(
        self, workflow, lifecycle, mock_kanban, mock_project_access
    ):
        """The residual gap: decompose_ticket's LLM call can take seconds,
        and the project can be disabled DURING it — after the ONE gate
        check orchestrate_work does before attempting decompose, but
        before the reclaim branch's claim_ticket call. If the LLM declines
        to split the ticket (returns no subtasks), execution falls through
        to the reclaim branch using the ORIGINAL, now-stale gate result.
        There must be a second, fresh check after decompose returns.
        """
        self._wire_task_project(mock_kanban, 9)
        lifecycle.get_or_create("62", "kanboard")
        lifecycle.transition("62", "kanboard", TicketState.READY)
        lifecycle.transition("62", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("62", "kanboard", "alice")
        lifecycle.claim_ticket("62", "kanboard", "marcus-slot-0")
        # 4+ AC items so _should_attempt_decompose fires.
        lifecycle.update_acceptance_criteria(
            "62", "kanboard", "- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d", "hash"
        )
        workflow._next_worker_ticket = AsyncMock(return_value="62")

        # Enabled through orchestrate_work's pre-decompose gate check AND
        # decompose_ticket's own internal gate (so the LLM is actually
        # called, matching the real scenario) — then disabled by the time
        # decompose returns having declined to split the ticket, so
        # execution falls through to the reclaim branch for ticket 62
        # itself using what would otherwise be a stale "enabled" result.
        calls = {"n": 0}

        def is_enabled(pid):
            calls["n"] += 1
            return calls["n"] <= 2

        mock_project_access.is_enabled = MagicMock(side_effect=is_enabled)

        llm_called = {"yes": False}

        async def fake_llm(prompt):
            llm_called["yes"] = True
            return '{"subtasks": []}'

        workflow._llm_generate = fake_llm

        result = await workflow.orchestrate_work(agent_id="worker-Z")

        assert llm_called["yes"] is True  # the real, slow path was taken
        assert result.get("ticket_id") != "62"
        rec = lifecycle.get("62", "kanboard")
        assert rec.ai_agent_id != "worker-Z"
