"""
Unit tests for multi-round AI verification in HumanGatedWorkflow._autocomplete_ticket.

These tests exercise the round-tracking state machine in isolation without
hitting any real kanban or git services.  Every external dependency is mocked.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, call, patch

from src.ai.verification.ai_verifier import VerificationResult
from src.core.gate_settings import GateSettingManager
from src.core.project_access_settings import ProjectAccessSettingManager
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
    kanban.set_merge_conflict_flag = AsyncMock(return_value=True)
    kanban.set_verify_round_tag = AsyncMock(return_value=True)

    events = MagicMock()
    events.subscribe = MagicMock()

    gate_settings = GateSettingManager(data_dir=tmp_path)
    project_access = ProjectAccessSettingManager(data_dir=tmp_path)

    # Isolated per-test: TicketLifecycleManager() with no arg defaults to
    # the real ./data/ticket_lifecycle.json — every prior test in this
    # file worked around that by writing directly into ._records instead
    # of going through get_or_create()'s persisted-load path, but that's
    # incidental, not a real guarantee. Point it at tmp_path so it's
    # actually isolated (unit tests must mock/isolate ALL external state).
    lifecycle = TicketLifecycleManager(
        state_file=str(tmp_path / "ticket_lifecycle.json")
    )

    branch_mgr = MagicMock()
    branch_mgr.config = MagicMock()
    branch_mgr.config.main_branch = "main"
    branch_mgr.create_branch = AsyncMock(return_value=True)
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
        project_access=project_access,
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
    async def test_second_call_after_fail_reverifies_before_merging(self, workflow):
        """After a round-1 failure, the next signal_ready re-verifies the
        fix rather than merging blindly.

        Regression: this used to merge with NO re-verification the moment
        rounds_done reached verify_count, regardless of whether that last
        round actually passed — with verify_count=1 (a common config),
        that meant AI-verify effectively only ever ran ONCE per ticket:
        the agent's follow-up fix after a failure was never checked at
        all. Fixed by tracking whether the last completed round passed
        (self._ticket_verify_last_passed) alongside the round count."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 1)
        workflow._verifier.verify = AsyncMock(
            side_effect=[_fail_result(), _pass_result()]
        )

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        # First call: verification fails
        await workflow._autocomplete_ticket("42", record)
        assert workflow._ticket_verify_rounds.get("42") == 1

        workflow._branch.merge_to_main.reset_mock()
        workflow._kanban.add_comment.reset_mock()

        # Simulate agent picked up, fixed issues, calls signal_ready again
        result = await workflow._autocomplete_ticket("42", record)

        assert result is True
        # The fix WAS re-verified (round 2), not blindly merged.
        assert workflow._verifier.verify.await_count == 2
        workflow._branch.merge_to_main.assert_called_once()
        assert "42" not in workflow._ticket_verify_rounds
        assert "42" not in workflow._ticket_verify_last_passed

    @pytest.mark.asyncio
    async def test_second_call_after_fail_still_fails_releases_again(
        self, workflow
    ):
        """If the resubmitted "fix" ALSO fails verification, it must be
        released again for another attempt — not merged."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 1)
        workflow._verifier.verify = AsyncMock(return_value=_fail_result(["Still broken"]))

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        await workflow._autocomplete_ticket("42", record)
        result = await workflow._autocomplete_ticket("42", record)

        assert result is False
        assert workflow._verifier.verify.await_count == 2
        workflow._branch.merge_to_main.assert_not_called()
        assert workflow._ticket_verify_last_passed.get("42") is False


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
    async def test_last_round_fails_final_fix_is_still_reverified(self, workflow):
        """Last round (N) fails → ticket released; the next call re-verifies
        the fix (round N+1, exceeding the configured count) rather than
        merging blindly. Regression: this used to skip straight to merge
        with NO re-verification the moment rounds_done reached
        verify_count, even though the round that got it there had FAILED
        — silently merging code whose most recent verification attempt
        found "Final bug" and was never actually fixed-and-confirmed."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 2)
        workflow._verifier.verify = AsyncMock(
            side_effect=[_pass_result(), _fail_result(["Final bug"]), _pass_result()]
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
        workflow._branch.merge_to_main.reset_mock()

        # Agent fixes, calls signal_ready again → round 3 actually runs
        # and confirms the fix before merging.
        result3 = await workflow._autocomplete_ticket("42", record)
        assert result3 is True
        assert workflow._verifier.verify.await_count == 3
        workflow._branch.merge_to_main.assert_called_once()
        assert "42" not in workflow._ticket_verify_rounds
        assert "42" not in workflow._ticket_verify_last_passed


class TestFinalRoundPassComment:
    """The last passing round posts a 'Merging now' comment before the merge."""

    @pytest.mark.asyncio
    async def test_final_round_pass_posts_round_comment(self, workflow):
        """verify_count=2, both rounds pass → round-2 comment announces the merge."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 2)
        workflow._verifier.verify = AsyncMock(return_value=_pass_result())

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        # Round 1: passes, not last → released for re-signal
        await workflow._autocomplete_ticket("42", record)
        workflow._kanban.add_comment.reset_mock()

        # Round 2 (last): passes → posts PASSED comment, then merges
        result = await workflow._autocomplete_ticket("42", record)

        assert result is True
        workflow._branch.merge_to_main.assert_called_once()
        bodies = [c.args[1] for c in workflow._kanban.add_comment.call_args_list]
        round_comments = [b for b in bodies if "Round 2 of 2: PASSED" in b]
        assert len(round_comments) == 1
        assert "Merging now" in round_comments[0]


class TestMergeFailureClearsCounter:
    """A failed merge leaves no stale round counter behind."""

    @pytest.mark.asyncio
    async def test_merge_failure_clears_round_counter(self, workflow):
        """verify_count=1, round passes but merge fails → counter is cleared."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 1)
        workflow._verifier.verify = AsyncMock(return_value=_pass_result())
        workflow._branch.merge_to_main = AsyncMock(return_value=False)

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        result = await workflow._autocomplete_ticket("42", record)

        assert result is False
        assert "42" not in workflow._ticket_verify_rounds

    @pytest.mark.asyncio
    async def test_merge_failure_sends_ticket_back_for_rebase_not_a_human(
        self, workflow
    ):
        """A failed auto-merge must not leak the slot: the ticket goes back
        to READY/unclaimed (or is immediately re-picked-up under an
        internal, worker-adoptable slot) for an AI agent to rebase, and
        the posted comment asks for a rebase, not human intervention.
        Mirrors the equivalent fix in _merge_ticket_to_main.
        """
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 1)
        workflow._verifier.verify = AsyncMock(return_value=_pass_result())
        workflow._branch.merge_to_main = AsyncMock(return_value=False)

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        result = await workflow._autocomplete_ticket("42", record)

        assert result is False
        rec = workflow._lifecycle.get("42", "kanboard")
        assert rec.state in (TicketState.READY, TicketState.IN_PROGRESS)
        assert rec.ai_agent_id is None or str(rec.ai_agent_id).startswith("marcus-")
        workflow._kanban.move_task_to_column.assert_any_call("42", "ready")
        posted_bodies = [
            c.args[-1] for c in workflow._kanban.add_comment.call_args_list
        ]
        assert any("rebase" in body.lower() for body in posted_bodies)


class TestFindingsSummaryIncludedInComment:
    """The whole point of AI Verify posting anything at all: whatever
    gaps/bugs the verifier actually found must be summarized, verbatim,
    in the ticket comment — not just a generic "issues found" header.
    Every prior test in this file asserts the header text is present;
    none of them assert the FINDING CONTENT itself made it into the
    comment body, which is the part a human/agent actually needs to act
    on."""

    @pytest.mark.asyncio
    async def test_single_finding_text_appears_verbatim_in_comment(self, workflow):
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 1)
        workflow._verifier.verify = AsyncMock(
            return_value=_fail_result(
                ["Off-by-one error in the pagination loop at line 42"]
            )
        )

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        await workflow._autocomplete_ticket("42", record)

        comment_body = workflow._kanban.add_comment.call_args[0][1]
        assert (
            "Off-by-one error in the pagination loop at line 42" in comment_body
        )

    @pytest.mark.asyncio
    async def test_multiple_findings_all_appear_in_comment(self, workflow):
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 1)
        findings = [
            "Missing null check on the response payload",
            "SQL query is vulnerable to injection via the search param",
            "New endpoint has no test coverage",
        ]
        workflow._verifier.verify = AsyncMock(
            return_value=_fail_result(list(findings))
        )

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        await workflow._autocomplete_ticket("42", record)

        comment_body = workflow._kanban.add_comment.call_args[0][1]
        for finding in findings:
            assert finding in comment_body

    @pytest.mark.asyncio
    async def test_findings_appear_on_the_final_round_failure_too(self, workflow):
        """The last-round-failed comment variant (different wording/
        action text) must still include the actual findings, not just
        the "final round" framing."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 2)
        workflow._verifier.verify = AsyncMock(
            side_effect=[
                _pass_result(),
                _fail_result(["Race condition when two agents claim the same slot"]),
            ]
        )

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        await workflow._autocomplete_ticket("42", record)  # round 1: passes
        workflow._kanban.add_comment.reset_mock()

        await workflow._autocomplete_ticket("42", record)  # round 2 (last): fails

        comment_body = workflow._kanban.add_comment.call_args[0][1]
        assert (
            "Race condition when two agents claim the same slot" in comment_body
        )

    @pytest.mark.asyncio
    async def test_findings_survive_across_a_multi_round_fix_cycle(self, workflow):
        """Round 1's findings must be distinguishable from round 2's —
        each round's own comment carries only that round's findings, not
        a stale carryover from a previous round."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 3)
        workflow._verifier.verify = AsyncMock(
            side_effect=[
                _fail_result(["Round-one-only issue: unhandled exception"]),
                _fail_result(["Round-two-only issue: leaked file handle"]),
                _pass_result(),
            ]
        )

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        await workflow._autocomplete_ticket("42", record)  # round 1: fails
        round1_body = workflow._kanban.add_comment.call_args[0][1]
        assert "Round-one-only issue: unhandled exception" in round1_body
        assert "Round-two-only issue" not in round1_body
        workflow._kanban.add_comment.reset_mock()

        await workflow._autocomplete_ticket("42", record)  # round 2: fails
        round2_body = workflow._kanban.add_comment.call_args[0][1]
        assert "Round-two-only issue: leaked file handle" in round2_body
        assert "Round-one-only issue" not in round2_body


class TestVerifierErrorFailsOpen:
    """An exception from the LLM verifier must not leave the ticket stuck."""

    @pytest.mark.asyncio
    async def test_verifier_exception_treated_as_pass(self, workflow):
        """verify_count=1, verifier raises → fail-open, branch merges."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 1)
        workflow._verifier.verify = AsyncMock(side_effect=RuntimeError("LLM down"))

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        result = await workflow._autocomplete_ticket("42", record)

        assert result is True
        workflow._branch.merge_to_main.assert_called_once()
        assert "42" not in workflow._ticket_verify_rounds


class TestVerifyRoundCardTag:
    """The "Verify N" board-card tag (KanboardKanban.set_verify_round_tag)
    must track the round counter exactly: set to the round about to run,
    left in place while the ticket sits in "in progress" awaiting a fix,
    and cleared the moment verification is fully done (all rounds passed,
    or a merge failure resets it for a rebase retry)."""

    @pytest.mark.asyncio
    async def test_verify_count_zero_never_touches_the_tag(self, workflow):
        workflow._gate.set_project_gate(1, "ai")
        # verify_count defaults to 0

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        await workflow._autocomplete_ticket("42", record)

        workflow._kanban.set_verify_round_tag.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_round_tags_verify_1_before_running_verification(
        self, workflow
    ):
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 3)
        workflow._verifier.verify = AsyncMock(return_value=_pass_result())

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        await workflow._autocomplete_ticket("42", record)  # round 1, not last

        workflow._kanban.set_verify_round_tag.assert_any_call("42", 1)
        # Verification hasn't finished (2 more rounds remain) — the tag
        # must NOT have been cleared.
        assert (
            call("42", None)
            not in workflow._kanban.set_verify_round_tag.call_args_list
        )

    @pytest.mark.asyncio
    async def test_tag_advances_to_2_on_the_second_round(self, workflow):
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 3)
        workflow._verifier.verify = AsyncMock(
            side_effect=[_fail_result(["Bug"]), _pass_result(), _pass_result()]
        )

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        await workflow._autocomplete_ticket("42", record)  # round 1: fails
        workflow._kanban.set_verify_round_tag.reset_mock()

        await workflow._autocomplete_ticket("42", record)  # round 2

        workflow._kanban.set_verify_round_tag.assert_any_call("42", 2)

    @pytest.mark.asyncio
    async def test_final_passing_round_clears_the_tag_before_merging(
        self, workflow
    ):
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 1)
        workflow._verifier.verify = AsyncMock(return_value=_pass_result())

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        result = await workflow._autocomplete_ticket("42", record)

        assert result is True
        workflow._kanban.set_verify_round_tag.assert_any_call("42", 1)
        # The clear (None) must be the LAST tag call — set to round 1,
        # then cleared once that round passes and is the final one.
        calls = [c.args for c in workflow._kanban.set_verify_round_tag.call_args_list]
        assert calls[-1] == ("42", None)
        workflow._branch.merge_to_main.assert_called_once()

    @pytest.mark.asyncio
    async def test_failed_round_leaves_the_tag_in_place_not_cleared(self, workflow):
        """Issues found → ticket bounces back to "in progress" — the tag
        must keep showing the round just attempted, not disappear."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 1)
        workflow._verifier.verify = AsyncMock(return_value=_fail_result(["Bug"]))

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        result = await workflow._autocomplete_ticket("42", record)

        assert result is False
        workflow._kanban.set_verify_round_tag.assert_any_call("42", 1)
        assert (
            call("42", None)
            not in workflow._kanban.set_verify_round_tag.call_args_list
        )

    @pytest.mark.asyncio
    async def test_merge_failure_does_not_re_clear_an_already_cleared_tag(
        self, workflow
    ):
        """By the time a merge is even attempted, the tag has always
        already been cleared (either by the final-round-passed branch, or
        the defensive rounds_done>=verify_count branch — verify_count=0
        never sets it at all) — a merge failure needs no clear of its
        own. Regression guard against re-adding one: that would cost an
        extra Kanboard RPC on every merge conflict, including ordinary
        verify_count=0 tickets that never had a tag to begin with."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 1)
        workflow._verifier.verify = AsyncMock(return_value=_pass_result())
        workflow._branch.merge_to_main = AsyncMock(return_value=False)

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        result = await workflow._autocomplete_ticket("42", record)

        assert result is False
        # Set for round 1, then cleared once (final round passed) — never
        # called a third time after the merge fails.
        assert workflow._kanban.set_verify_round_tag.call_args_list == [
            call("42", 1),
            call("42", None),
        ]


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


class TestVerifyCountLoweredMidFlight:
    """A human can lower a ticket's/project's AI-verify round count live,
    via the gate-setting API, while a ticket sits IN_PROGRESS between
    rounds. Lowering it must never let an attempt that already FAILED
    slip through unverified just because the new, lower count happens to
    already be "met" by the failed attempt's round number."""

    @pytest.mark.asyncio
    async def test_lowered_count_after_a_failure_still_reverifies(self, workflow):
        """verify_count starts at 3, round 1 fails (rounds_done=1), then
        a human lowers verify_count to 1 before the agent resubmits — the
        resubmission must still be re-verified, not merged blindly just
        because rounds_done(1) >= the new verify_count(1)."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 3)
        workflow._verifier.verify = AsyncMock(
            side_effect=[_fail_result(["Bug"]), _pass_result()]
        )

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        # Round 1 (of the originally-configured 3): fails.
        await workflow._autocomplete_ticket("42", record)
        assert workflow._ticket_verify_rounds["42"] == 1
        assert workflow._ticket_verify_last_passed["42"] is False

        # Human lowers verify_count to 1 before the agent resubmits.
        workflow._gate.set_project_verify_count(1, 1)

        result = await workflow._autocomplete_ticket("42", record)

        assert result is True
        assert workflow._verifier.verify.await_count == 2  # re-verified
        workflow._branch.merge_to_main.assert_called_once()
        assert "42" not in workflow._ticket_verify_rounds
        assert "42" not in workflow._ticket_verify_last_passed

    @pytest.mark.asyncio
    async def test_lowered_count_after_a_pass_merges_without_extra_verify(
        self, workflow
    ):
        """The inverse: if the LAST completed round genuinely passed,
        lowering verify_count to that same round number is safe to trust
        — merges immediately without wasting another verification call."""
        workflow._gate.set_project_gate(1, "ai")
        workflow._gate.set_project_verify_count(1, 3)
        workflow._verifier.verify = AsyncMock(return_value=_pass_result())

        record = _make_record()
        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle._records[("42", "kanboard")] = record

        # Round 1 (of 3): passes, not final.
        await workflow._autocomplete_ticket("42", record)
        assert workflow._ticket_verify_rounds["42"] == 1
        assert workflow._ticket_verify_last_passed["42"] is True

        workflow._gate.set_project_verify_count(1, 1)
        workflow._verifier.verify.reset_mock()
        workflow._branch.merge_to_main.reset_mock()

        result = await workflow._autocomplete_ticket("42", record)

        assert result is True
        workflow._verifier.verify.assert_not_called()
        workflow._branch.merge_to_main.assert_called_once()


class TestStaleVerifyRoundsClearedOnFreshStart:
    """self._ticket_verify_rounds/_ticket_verify_last_passed must not
    survive a ticket LEAVING and later RE-ENTERING an AI-gate verify
    episode via a different route (gate flip to human then back, a
    reopen, a stuck-ticket re-assignment) — but must NOT be touched by an
    ordinary same-episode resume (an agent picking a still-IN_PROGRESS
    ticket back up to fix issues from the last verify round)."""

    async def _start(self, workflow, ticket_id, record):
        with patch(
            "src.core.project_description.ProjectDescriptionManager.get_stack",
            return_value={"language": "python"},
        ):
            await workflow._start_ai_work(ticket_id, record)

    @pytest.mark.asyncio
    async def test_resume_while_already_in_progress_preserves_round_data(
        self, workflow
    ):
        """The ordinary multi-round-verify resume path: ticket stays
        IN_PROGRESS the whole time between rounds — _start_ai_work must
        NOT clear the round counter here, or multi-round verify would
        never get past round 1 (see _pickup_next_ticket, which calls
        _start_ai_work for a released-but-still-IN_PROGRESS ticket)."""
        workflow._project_access.set_project_enabled(1, True)
        workflow._ticket_verify_rounds["42"] = 1
        workflow._ticket_verify_last_passed["42"] = False

        workflow._lifecycle.get_or_create("42", "kanboard")
        workflow._lifecycle.transition("42", "kanboard", TicketState.READY)
        workflow._lifecycle.transition("42", "kanboard", TicketState.IN_PROGRESS)
        workflow._lifecycle.set_assignee("42", "kanboard", "alice")
        record = workflow._lifecycle.get("42", "kanboard")

        await self._start(workflow, "42", record)

        assert workflow._ticket_verify_rounds.get("42") == 1
        assert workflow._ticket_verify_last_passed.get("42") is False

    @pytest.mark.asyncio
    async def test_fresh_start_from_ready_clears_stale_round_data(self, workflow):
        """A ticket starting fresh from READY must never inherit leftover
        round bookkeeping from some earlier, unrelated episode under the
        same ticket id."""
        workflow._project_access.set_project_enabled(1, True)
        workflow._ticket_verify_rounds["43"] = 2
        workflow._ticket_verify_last_passed["43"] = False

        workflow._lifecycle.get_or_create("43", "kanboard")
        workflow._lifecycle.transition("43", "kanboard", TicketState.READY)
        workflow._lifecycle.set_assignee("43", "kanboard", "alice")
        record = workflow._lifecycle.get("43", "kanboard")

        await self._start(workflow, "43", record)

        assert "43" not in workflow._ticket_verify_rounds
        assert "43" not in workflow._ticket_verify_last_passed

    @pytest.mark.asyncio
    async def test_resume_from_waiting_for_human_clears_stale_round_data(
        self, workflow
    ):
        """The exact scenario this fix targets: gate flipped to human
        mid-round (leaving stale round data from the AI path), ticket
        went through human review to Waiting for Human, and is now being
        resumed (e.g. re-assigned) with the gate back on AI — the stale
        count from the ABANDONED episode must not be reused."""
        workflow._project_access.set_project_enabled(1, True)
        workflow._ticket_verify_rounds["44"] = 1
        workflow._ticket_verify_last_passed["44"] = True

        workflow._lifecycle.get_or_create("44", "kanboard")
        workflow._lifecycle.transition("44", "kanboard", TicketState.READY)
        workflow._lifecycle.transition("44", "kanboard", TicketState.IN_PROGRESS)
        workflow._lifecycle.transition("44", "kanboard", TicketState.WAITING_FOR_HUMAN)
        workflow._lifecycle.set_assignee("44", "kanboard", "alice")
        record = workflow._lifecycle.get("44", "kanboard")

        await self._start(workflow, "44", record)

        assert "44" not in workflow._ticket_verify_rounds
        assert "44" not in workflow._ticket_verify_last_passed
        workflow._kanban.set_verify_round_tag.assert_any_call("44", None)
