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
    async def test_start_releases_ghost_claims(self, workflow, lifecycle):
        """workflow.start() releases claims persisted by a dead process."""
        lifecycle.get_or_create("53", "kanboard")
        lifecycle.transition("53", "kanboard", TicketState.READY)
        lifecycle.transition("53", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.claim_ticket("53", "kanboard", "marcus-deadbeef")

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
        task.priority = None
        task.labels = []
        task.due_date = None
        task.estimated_hours = None
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
    async def test_merge_failure_frees_slot_and_parks_waiting(
        self, workflow, lifecycle, mock_kanban, mock_branch
    ):
        """A failed merge parks the ticket in WFH and releases the slot.

        The old behavior left it IN_PROGRESS and claimed forever — a
        permanent slot leak (deadlock at cap=1).
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
        assert rec.state == TicketState.WAITING_FOR_HUMAN
        assert rec.ai_agent_id is None  # slot freed
        mock_kanban.move_task_to_column.assert_any_call("60", "waiting for human")

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
