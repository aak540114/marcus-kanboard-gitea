"""
Unit tests for src/core/ticket_lifecycle.py
"""

from datetime import datetime, timezone

import pytest

from src.core.ticket_lifecycle import (
    InvalidTransitionError,
    TicketLifecycleManager,
    TicketRecord,
    TicketState,
)


@pytest.fixture
def state_file(tmp_path):
    """Temporary state file path."""
    return str(tmp_path / "lifecycle.json")


@pytest.fixture
def manager(state_file):
    """Fresh TicketLifecycleManager backed by a temp file."""
    return TicketLifecycleManager(state_file=state_file)


class TestTicketRecord:
    """Tests for TicketRecord serialisation."""

    def test_to_dict_round_trips(self):
        """Serialise and deserialise a record without data loss."""
        rec = TicketRecord(
            ticket_id="PROJ-42",
            provider="jira",
            state=TicketState.IN_PROGRESS,
            branch_name="ticket/jira/proj-42",
            assignee="alice",
            acceptance_criteria="- [ ] Deploy",
            ac_hash="abc123",
        )
        d = rec.to_dict()
        restored = TicketRecord.from_dict(d)

        assert restored.ticket_id == "PROJ-42"
        assert restored.provider == "jira"
        assert restored.state == TicketState.IN_PROGRESS
        assert restored.assignee == "alice"

    def test_key_property_format(self):
        """Key is provider:ticket_id."""
        rec = TicketRecord(ticket_id="123", provider="github")
        assert rec.key == "github:123"

    def test_from_dict_with_merged_at(self):
        """Records with merged_at deserialise correctly."""
        rec = TicketRecord(
            ticket_id="X-1",
            provider="jira",
            merged_at=datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        )
        d = rec.to_dict()
        restored = TicketRecord.from_dict(d)
        assert restored.merged_at is not None
        assert restored.merged_at.year == 2024


class TestTicketLifecycleManager:
    """Tests for TicketLifecycleManager."""

    def test_get_or_create_returns_new_todo_record(self, manager):
        """get_or_create creates a new TODO record (not UNASSIGNED)."""
        rec = manager.get_or_create("PROJ-1", "jira")
        assert rec.ticket_id == "PROJ-1"
        assert rec.provider == "jira"
        assert rec.state == TicketState.TODO

    def test_get_or_create_is_idempotent(self, manager):
        """Calling get_or_create twice returns the same record."""
        rec1 = manager.get_or_create("PROJ-1", "jira")
        rec2 = manager.get_or_create("PROJ-1", "jira")
        assert rec1.key == rec2.key

    def test_get_returns_none_for_unknown(self, manager):
        """get() returns None for untracked tickets."""
        assert manager.get("unknown-99", "jira") is None

    def test_default_branch_name_generated(self, manager):
        """Branch name is auto-generated from ticket_id."""
        rec = manager.get_or_create("PROJ-42", "jira")
        assert rec.branch_name == "ticket/jira/proj-42"

    def test_explicit_branch_name_preserved(self, manager):
        """Explicitly provided branch name is not overridden."""
        rec = manager.get_or_create("99", "github", branch_name="custom/branch")
        assert rec.branch_name == "custom/branch"

    def test_valid_transition_todo_to_ready(self, manager):
        """TODO → READY is a valid transition."""
        manager.get_or_create("T-1", "jira")
        rec = manager.transition(
            "T-1", "jira", TicketState.READY, assignee="bob"
        )
        assert rec.state == TicketState.READY
        assert rec.assignee == "bob"

    def test_valid_transition_chain_happy_path(self, manager):
        """Full happy-path: TODO → READY → IN_PROGRESS → WAITING_FOR_HUMAN → DONE."""
        manager.get_or_create("T-2", "jira")
        manager.transition("T-2", "jira", TicketState.READY)
        manager.transition("T-2", "jira", TicketState.IN_PROGRESS)
        manager.transition("T-2", "jira", TicketState.WAITING_FOR_HUMAN)
        rec = manager.transition("T-2", "jira", TicketState.DONE)
        assert rec.state == TicketState.DONE

    def test_invalid_transition_todo_to_in_progress_raises(self, manager):
        """Jumping from TODO straight to IN_PROGRESS raises InvalidTransitionError."""
        manager.get_or_create("T-3", "jira")
        with pytest.raises(InvalidTransitionError):
            manager.transition("T-3", "jira", TicketState.IN_PROGRESS)

    def test_transition_records_history(self, manager):
        """Each transition is appended to history."""
        manager.get_or_create("T-4", "jira")
        manager.transition("T-4", "jira", TicketState.READY, reason="human readied")
        rec = manager.get("T-4", "jira")
        assert len(rec.history) == 1
        assert rec.history[0]["from"] == "todo"
        assert rec.history[0]["to"] == "ready"
        assert rec.history[0]["reason"] == "human readied"

    def test_transition_missing_ticket_raises(self, manager):
        """Transitioning an untracked ticket raises KeyError."""
        with pytest.raises(KeyError):
            manager.transition("missing", "jira", TicketState.READY)

    def test_reopen_flow(self, manager):
        """DONE → REOPENED → IN_PROGRESS is valid (reopen scenario)."""
        manager.get_or_create("T-5", "jira")
        manager.transition("T-5", "jira", TicketState.READY)
        manager.transition("T-5", "jira", TicketState.IN_PROGRESS)
        manager.transition("T-5", "jira", TicketState.DONE)
        manager.transition("T-5", "jira", TicketState.REOPENED)
        rec = manager.transition("T-5", "jira", TicketState.IN_PROGRESS)
        assert rec.state == TicketState.IN_PROGRESS

    def test_waiting_for_human_flow(self, manager):
        """IN_PROGRESS → WAITING_FOR_HUMAN → IN_PROGRESS (human responds)."""
        manager.get_or_create("T-6", "jira")
        manager.transition("T-6", "jira", TicketState.READY)
        manager.transition("T-6", "jira", TicketState.IN_PROGRESS)
        manager.transition("T-6", "jira", TicketState.WAITING_FOR_HUMAN)
        rec = manager.transition("T-6", "jira", TicketState.IN_PROGRESS)
        assert rec.state == TicketState.IN_PROGRESS

    def test_blocked_flow(self, manager):
        """IN_PROGRESS → BLOCKED → IN_PROGRESS (dependency resolved)."""
        manager.get_or_create("T-7", "jira")
        manager.transition("T-7", "jira", TicketState.READY)
        manager.transition("T-7", "jira", TicketState.IN_PROGRESS)
        manager.transition("T-7", "jira", TicketState.BLOCKED)
        rec = manager.transition("T-7", "jira", TicketState.IN_PROGRESS)
        assert rec.state == TicketState.IN_PROGRESS

    def test_unassign_from_ready_returns_to_todo(self, manager):
        """READY → TODO is valid (ticket unassigned after being readied)."""
        manager.get_or_create("T-8", "jira")
        manager.transition("T-8", "jira", TicketState.READY)
        rec = manager.transition("T-8", "jira", TicketState.TODO)
        assert rec.state == TicketState.TODO

    def test_update_acceptance_criteria(self, manager):
        """update_acceptance_criteria stores new text and hash."""
        manager.get_or_create("T-9", "jira")
        manager.update_acceptance_criteria("T-9", "jira", "- [ ] Test", "hash1")
        rec = manager.get("T-9", "jira")
        assert rec.acceptance_criteria == "- [ ] Test"
        assert rec.ac_hash == "hash1"

    def test_set_merged(self, manager):
        """set_merged records the merge timestamp."""
        manager.get_or_create("T-10", "jira")
        now = datetime.now(timezone.utc)
        manager.set_merged("T-10", "jira", merged_at=now)
        rec = manager.get("T-10", "jira")
        assert rec.merged_at == now

    def test_set_dev_env_port(self, manager):
        """set_dev_env_port stores the port number."""
        manager.get_or_create("T-11", "jira")
        manager.set_dev_env_port("T-11", "jira", 9200)
        rec = manager.get("T-11", "jira")
        assert rec.dev_env_port == 9200

    def test_set_assignee(self, manager):
        """set_assignee stores the assignee without a state transition."""
        manager.get_or_create("T-12", "jira")
        rec = manager.set_assignee("T-12", "jira", "alice")
        assert rec.assignee == "alice"
        assert rec.state == TicketState.TODO  # state unchanged

    def test_set_assignee_missing_ticket_raises(self, manager):
        """set_assignee on an untracked ticket raises KeyError."""
        with pytest.raises(KeyError):
            manager.set_assignee("missing", "jira", "bob")

    def test_in_state_filter(self, manager):
        """in_state returns only records in the given state."""
        manager.get_or_create("A", "jira")
        manager.get_or_create("B", "jira")
        manager.transition("A", "jira", TicketState.READY)
        in_ready = manager.in_state(TicketState.READY)
        in_todo = manager.in_state(TicketState.TODO)
        assert len(in_ready) == 1
        assert in_ready[0].ticket_id == "A"
        assert len(in_todo) == 1
        assert in_todo[0].ticket_id == "B"

    def test_all_records(self, manager):
        """all_records returns every tracked ticket."""
        manager.get_or_create("X", "jira")
        manager.get_or_create("Y", "github")
        assert len(manager.all_records()) == 2

    def test_persistence_survives_restart(self, state_file):
        """Records are reloaded correctly after a manager restart."""
        m1 = TicketLifecycleManager(state_file=state_file)
        m1.get_or_create("PERSIST-1", "jira")
        m1.transition("PERSIST-1", "jira", TicketState.READY, assignee="alice")

        m2 = TicketLifecycleManager(state_file=state_file)
        rec = m2.get("PERSIST-1", "jira")
        assert rec is not None
        assert rec.state == TicketState.READY
        assert rec.assignee == "alice"

    def test_branch_name_with_special_chars(self, manager):
        """Ticket IDs with slashes/spaces produce safe branch names."""
        rec = manager.get_or_create("PROJ/TASK 42", "jira")
        assert "/" not in rec.branch_name.split("ticket/jira/")[1]
        assert " " not in rec.branch_name

    def test_done_from_in_progress_directly(self, manager):
        """Human can mark ticket DONE directly from IN_PROGRESS."""
        manager.get_or_create("T-13", "jira")
        manager.transition("T-13", "jira", TicketState.READY)
        manager.transition("T-13", "jira", TicketState.IN_PROGRESS)
        rec = manager.transition("T-13", "jira", TicketState.DONE)
        assert rec.state == TicketState.DONE

    def test_done_from_waiting_for_human(self, manager):
        """Human can mark ticket DONE from WAITING_FOR_HUMAN state."""
        manager.get_or_create("T-14", "jira")
        manager.transition("T-14", "jira", TicketState.READY)
        manager.transition("T-14", "jira", TicketState.IN_PROGRESS)
        manager.transition("T-14", "jira", TicketState.WAITING_FOR_HUMAN)
        rec = manager.transition("T-14", "jira", TicketState.DONE)
        assert rec.state == TicketState.DONE
