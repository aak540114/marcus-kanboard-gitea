"""
Unit tests for multi-round AI verification in HumanGatedWorkflow._autocomplete_ticket.

These tests exercise the round-tracking state machine in isolation without
hitting any real kanban or git services.  Every external dependency is mocked.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.ai.verification.ai_verifier import VerificationResult
from src.core.gate_settings import GateSettingManager
from src.core.ticket_lifecycle import TicketLifecycleManager, TicketRecord, TicketState
from src.workflows.human_gated_workflow import HumanGatedWorkflow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(ticket_id: str = "42", branch_name: str = "ticket/kb/42") -> TicketRecord:
    """Return a minimal TicketRecord in IN_PROGRESS state."""
    record = TicketRecord(
        ticket_id=ticket_id,
        provider="kanboard",
        state=TicketState.IN_PROGRESS,
        branch_name=branch_name,
        assignee="alice",
    )
    return record


def _pass_result(**kwargs) -> VerificationResult:
    return VerificationResult(passed=True, findings=[], **kwargs)


def _fail_result(findings=None) -> VerificationResult:
    return VerificationResult(passed=False, findings=findings or ["Bug found"])


# ---------------------------------------------------------------------------
# Fixture: a fully-mocked HumanGatedWorkflow
# ---------------------------------------------------------------------------

@pytest.fixture()
def workflow(tmp_path):
    """Return a HumanGatedWorkflow with all external collaborators mocked."""
    kanban = MagicMock()
    # get_task_by_id returns a task with project_id so _get_effective_verify_count works
    task_mock = MagicMock()
    task_mock.name = "Test ticket"
    task_mock.source_context = {"kanboard_task": {"project_id": "1"}}
    kanban.get_task_by_id = AsyncMock(return_value=task_mock)
    kanban.add_comment = AsyncMock(return_value=True)
    kanban.move_task_to_column = AsyncMock(return_value=True)

    events = MagicMock()
    events.subscribe = MagicMock()

    gate_settings = GateSettingManager(data_dir=tmp_path)

    lifecycle = TicketLifecycleManager()

    branch_mgr = MagicMock()
    branch_mgr.config = MagicMock()
    branch_mgr.config.main_branch = "main"
    branch_mgr.get_branch_diff = AsyncMock(return_value="diff content")
    branch_mgr.merge_to_main = AsyncMock(return_value=True)
    branch_mgr.get_branch_commits = AsyncMock(return_value=[])

    verifier = MagicMock()

    wf = HumanGatedWorkflow(
        kanban=kanban,
        events=events,
        provider_name="kanboard",
        lifecycle=lifecycle,
        gate_settings=gate_settings,
        ai_verifier=verifier,
    )
    wf._branch = branch_mgr
    wf._verifier = verifier
    # Prevent _pickup_next_ticket from doing real work
    wf._pickup_next_ticket = AsyncMock()

    return wf


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestVerifyCountZeroSkipsVerification:
    """verify_count=0 → no verification; branch merges immediately."""

    @pytest.mark.asyncio
    async def test_merges_without_calling_verifier(self, workflow):
        """When verify_count is 0, the verifier is never called."""
        workflow._gate.set_project_gate(1, "ai")
        # verify_count defaults to 0

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        result = await workflow._autocomplete_ticket("42", record)

        assert result is True
        workflow._verifier.verify.assert_not_called()
        workflow._branch.merge_to_main.assert_called_once()


class TestVerifyCountOne:
    """verify_count=1 → exactly one LLM round."""

    @pytest.mark.asyncio
    async def test_passes_on_first_call_merges_immediately(self, workflow):
        """Single round passes → merge happens on the same signal_ready call."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 1)
        workflow._verifier.verify = AsyncMock(return_value=_pass_result())

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        result = await workflow._autocomplete_ticket("42", record)

        assert result is True
        workflow._verifier.verify.assert_called_once()
        workflow._branch.merge_to_main.assert_called_once()
        # Round counter is cleared
        assert "42" not in workflow._ticket_verify_rounds

    @pytest.mark.asyncio
    async def test_fails_on_first_call_releases_ticket(self, workflow):
        """Single round fails → comment posted, ticket released, returns False."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 1)
        workflow._verifier.verify = AsyncMock(return_value=_fail_result(["Missing test"]))

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        result = await workflow._autocomplete_ticket("42", record)

        assert result is False
        workflow._branch.merge_to_main.assert_not_called()
        workflow._kanban.add_comment.assert_called_once()
        # Round counter reflects 1 round done
        assert workflow._ticket_verify_rounds.get("42") == 1

    @pytest.mark.asyncio
    async def test_second_call_after_fail_merges_without_verify(self, workflow):
        """After a round-1 failure, the next signal_ready merges (no re-verify)."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 1)
        workflow._verifier.verify = AsyncMock(return_value=_fail_result())

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        # First call: verification fails
        await workflow._autocomplete_ticket("42", record)
        assert workflow._ticket_verify_rounds.get("42") == 1

        # Reset mock counts for second call
        workflow._verifier.verify.reset_mock()
        workflow._branch.merge_to_main.reset_mock()
        workflow._kanban.add_comment.reset_mock()

        # Simulate agent picked up, fixed issues, calls signal_ready again
        result = await workflow._autocomplete_ticket("42", record)

        assert result is True
        # rounds_done (1) >= verify_count (1) → no re-verify
        workflow._verifier.verify.assert_not_called()
        workflow._branch.merge_to_main.assert_called_once()
        assert "42" not in workflow._ticket_verify_rounds


class TestVerifyCountThree:
    """verify_count=3 → three sequential LLM rounds with fix cycles between."""

    @pytest.mark.asyncio
    async def test_all_three_rounds_pass_cleanly(self, workflow):
        """Rounds 1 and 2 pass → comment posted asking for next call.
        Round 3 passes → immediate merge."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 3)
        workflow._verifier.verify = AsyncMock(return_value=_pass_result())

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        # Round 1: passes but not last → comment, no merge
        result1 = await workflow._autocomplete_ticket("42", record)
        assert result1 is False
        assert workflow._ticket_verify_rounds["42"] == 1
        assert workflow._branch.merge_to_main.call_count == 0

        comment_body_1 = workflow._kanban.add_comment.call_args[0][1]
        assert "Round 1 of 3: PASSED" in comment_body_1
        assert "round 2 of 3" in comment_body_1.lower()

        workflow._kanban.add_comment.reset_mock()

        # Round 2: passes but not last → comment, no merge
        result2 = await workflow._autocomplete_ticket("42", record)
        assert result2 is False
        assert workflow._ticket_verify_rounds["42"] == 2
        assert workflow._branch.merge_to_main.call_count == 0

        comment_body_2 = workflow._kanban.add_comment.call_args[0][1]
        assert "Round 2 of 3: PASSED" in comment_body_2

        workflow._kanban.add_comment.reset_mock()

        # Round 3: passes and is last → merge immediately, no comment
        result3 = await workflow._autocomplete_ticket("42", record)
        assert result3 is True
        workflow._branch.merge_to_main.assert_called_once()
        assert "42" not in workflow._ticket_verify_rounds

    @pytest.mark.asyncio
    async def test_round_fails_then_agent_fixes_then_continues(self, workflow):
        """Round 1 fails → agent fixes → round 2 runs on next call."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 3)

        # Round 1: fail; rounds 2+: pass
        workflow._verifier.verify = AsyncMock(
            side_effect=[_fail_result(["Bug"]), _pass_result(), _pass_result()]
        )

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        # Round 1: fails → ticket released for fix
        result1 = await workflow._autocomplete_ticket("42", record)
        assert result1 is False
        assert workflow._ticket_verify_rounds["42"] == 1

        comment_body = workflow._kanban.add_comment.call_args[0][1]
        assert "Round 1 of 3: Issues Found" in comment_body
        assert "round 2 of 3" in comment_body.lower()

        workflow._kanban.add_comment.reset_mock()

        # Agent fixes issue, calls signal_ready again → round 2
        result2 = await workflow._autocomplete_ticket("42", record)
        assert result2 is False
        assert workflow._ticket_verify_rounds["42"] == 2

        # Round 3 passes → merge
        result3 = await workflow._autocomplete_ticket("42", record)
        assert result3 is True
        workflow._branch.merge_to_main.assert_called_once()

    @pytest.mark.asyncio
    async def test_last_round_fails_releases_for_final_fix(self, workflow):
        """Last round (N) fails → ticket released; next call merges with no verify."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 2)
        workflow._verifier.verify = AsyncMock(
            side_effect=[_pass_result(), _fail_result(["Final bug"])]
        )

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        # Round 1: passes
        await workflow._autocomplete_ticket("42", record)
        assert workflow._ticket_verify_rounds["42"] == 1

        workflow._kanban.add_comment.reset_mock()

        # Round 2 (last): fails → comment says "final round"
        result2 = await workflow._autocomplete_ticket("42", record)
        assert result2 is False
        assert workflow._ticket_verify_rounds["42"] == 2

        comment_body = workflow._kanban.add_comment.call_args[0][1]
        assert "Round 2 of 2: Issues Found" in comment_body
        assert "final verification round" in comment_body.lower()

        workflow._kanban.add_comment.reset_mock()
        workflow._verifier.verify.reset_mock()
        workflow._branch.merge_to_main.reset_mock()

        # Agent fixes, calls signal_ready one last time → merge with no verify
        result3 = await workflow._autocomplete_ticket("42", record)
        assert result3 is True
        workflow._verifier.verify.assert_not_called()
        workflow._branch.merge_to_main.assert_called_once()
        assert "42" not in workflow._ticket_verify_rounds


class TestVerifyCountRoundTrackerIsolation:
    """Round counters for different tickets are independent."""

    @pytest.mark.asyncio
    async def test_separate_tickets_have_independent_counters(self, workflow):
        """Ticket A and ticket B track rounds independently."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 2)
        workflow._verifier.verify = AsyncMock(return_value=_pass_result())

        def make_rec(tid):
            r = _make_record(ticket_id=tid, branch_name=f"ticket/kb/{tid}")
            workflow._lifecycle.get_or_create(tid, "kanboard")
            workflow._lifecycle._records[(tid, "kanboard")] = r
            return r

        rec_a = make_rec("10")
        rec_b = make_rec("20")

        # Ticket A: round 1
        await workflow._autocomplete_ticket("10", rec_a)
        assert workflow._ticket_verify_rounds.get("10") == 1
        assert workflow._ticket_verify_rounds.get("20") is None

        # Ticket B: round 1
        await workflow._autocomplete_ticket("20", rec_b)
        assert workflow._ticket_verify_rounds.get("10") == 1
        assert workflow._ticket_verify_rounds.get("20") == 1

        # Ticket A: round 2 → merge
        result = await workflow._autocomplete_ticket("10", rec_a)
        assert result is True
        assert "10" not in workflow._ticket_verify_rounds
        assert workflow._ticket_verify_rounds.get("20") == 1
