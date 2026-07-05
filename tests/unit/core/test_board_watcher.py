"""
Unit tests for src/core/board_watcher.py
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.board_watcher import BoardWatcher, TicketSnapshot
from src.core.events import Events
from src.core.models import Priority, Task, TaskStatus


def _make_task(
    task_id: str,
    title: str = "Test task",
    status: TaskStatus = TaskStatus.TODO,
    description: str = "",
    assignee: Optional[str] = None,
    source_context: Optional[Dict[str, Any]] = None,
) -> Task:
    """Helper: build a minimal Task for testing."""
    ctx = source_context or {}
    if assignee:
        ctx["assignee"] = assignee
    return Task(
        id=task_id,
        name=title,
        description=description,
        status=status,
        priority=Priority.MEDIUM,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        assigned_to=assignee,
        due_date=None,
        estimated_hours=0.0,
        source_context=ctx or None,
    )


@pytest.fixture
def events():
    """Real Events bus for testing."""
    return Events(store_history=True)


@pytest.fixture
def mock_kanban():
    """Mock KanbanInterface."""
    kanban = MagicMock()
    kanban.get_all_tasks = AsyncMock(return_value=[])
    kanban.get_attachments = AsyncMock(return_value={"success": True, "data": []})
    return kanban


@pytest.fixture
def watcher(mock_kanban, events):
    """BoardWatcher with mock kanban and real events."""
    return BoardWatcher(
        kanban=mock_kanban,
        events=events,
        provider_name="jira",
        poll_interval=60.0,
    )


class TestTicketSnapshot:
    """Tests for TicketSnapshot dataclass."""

    def test_defaults(self):
        """TicketSnapshot initialises with sensible defaults."""
        snap = TicketSnapshot(ticket_id="T-1")
        assert snap.assignee is None
        assert snap.status == TaskStatus.TODO
        assert snap.is_closed is False
        assert snap.comment_ids == set()


class TestBoardWatcherInit:
    """Tests for BoardWatcher initialisation."""

    def test_not_running_initially(self, watcher):
        """Watcher is not running after construction."""
        assert watcher._running is False

    def test_snapshots_empty_initially(self, watcher):
        """No snapshots exist before first poll."""
        assert watcher._snapshots == {}


class TestBoardWatcherPollOnce:
    """Tests for poll_once() behaviour."""

    @pytest.mark.asyncio
    async def test_emits_ticket_new_on_first_sight(self, watcher, events, mock_kanban):
        """First time a ticket is seen, ticket.new is emitted."""
        task = _make_task("T-1")
        mock_kanban.get_all_tasks = AsyncMock(return_value=[task])

        received: List[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        events.subscribe("ticket.new", handler)
        await watcher.poll_once()

        assert len(received) == 1
        assert received[0].data["ticket_id"] == "T-1"

    @pytest.mark.asyncio
    async def test_no_event_when_nothing_changes(self, watcher, events, mock_kanban):
        """Second poll with unchanged data emits no events."""
        task = _make_task("T-1")
        mock_kanban.get_all_tasks = AsyncMock(return_value=[task])

        new_events: List[Any] = []
        status_events: List[Any] = []
        events.subscribe("ticket.new", lambda e: new_events.append(e))
        events.subscribe("ticket.status_changed", lambda e: status_events.append(e))

        await watcher.poll_once()  # first poll → ticket.new
        await watcher.poll_once()  # second poll → nothing new

        assert len(new_events) == 1
        assert len(status_events) == 0

    @pytest.mark.asyncio
    async def test_emits_assigned_when_assignee_appears(
        self, watcher, events, mock_kanban
    ):
        """ticket.assigned is emitted when a ticket gains an assignee."""
        task_unassigned = _make_task("T-2")
        task_assigned = _make_task("T-2", assignee="alice")

        mock_kanban.get_all_tasks = AsyncMock(return_value=[task_unassigned])
        await watcher.poll_once()

        assigned_events: List[Any] = []
        events.subscribe("ticket.assigned", lambda e: assigned_events.append(e))

        mock_kanban.get_all_tasks = AsyncMock(return_value=[task_assigned])
        await watcher.poll_once()

        assert len(assigned_events) == 1
        assert assigned_events[0].data["assignee"] == "alice"

    @pytest.mark.asyncio
    async def test_emits_unassigned_when_assignee_removed(
        self, watcher, events, mock_kanban
    ):
        """ticket.unassigned is emitted when assignee is removed."""
        task_assigned = _make_task("T-3", assignee="alice")
        task_unassigned = _make_task("T-3")

        mock_kanban.get_all_tasks = AsyncMock(return_value=[task_assigned])
        await watcher.poll_once()

        unassigned_events: List[Any] = []
        events.subscribe("ticket.unassigned", lambda e: unassigned_events.append(e))

        mock_kanban.get_all_tasks = AsyncMock(return_value=[task_unassigned])
        await watcher.poll_once()

        assert len(unassigned_events) == 1

    @pytest.mark.asyncio
    async def test_emits_status_changed(self, watcher, events, mock_kanban):
        """ticket.status_changed is emitted on status change."""
        task_todo = _make_task("T-4", status=TaskStatus.TODO)
        task_in_progress = _make_task("T-4", status=TaskStatus.IN_PROGRESS)

        mock_kanban.get_all_tasks = AsyncMock(return_value=[task_todo])
        await watcher.poll_once()

        status_events: List[Any] = []
        events.subscribe("ticket.status_changed", lambda e: status_events.append(e))

        mock_kanban.get_all_tasks = AsyncMock(return_value=[task_in_progress])
        await watcher.poll_once()

        assert len(status_events) == 1
        assert status_events[0].data["old_status"] == "todo"
        assert status_events[0].data["new_status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_emits_closed_when_status_becomes_done(
        self, watcher, events, mock_kanban
    ):
        """ticket.closed is emitted when status transitions to DONE."""
        task_open = _make_task("T-5", status=TaskStatus.IN_PROGRESS)
        task_closed = _make_task("T-5", status=TaskStatus.DONE)

        mock_kanban.get_all_tasks = AsyncMock(return_value=[task_open])
        await watcher.poll_once()

        closed_events: List[Any] = []
        events.subscribe("ticket.closed", lambda e: closed_events.append(e))

        mock_kanban.get_all_tasks = AsyncMock(return_value=[task_closed])
        await watcher.poll_once()

        assert len(closed_events) == 1

    @pytest.mark.asyncio
    async def test_emits_reopened_when_done_becomes_active(
        self, watcher, events, mock_kanban
    ):
        """ticket.reopened is emitted when DONE reverts to an active status."""
        task_done = _make_task("T-6", status=TaskStatus.DONE)
        task_todo = _make_task("T-6", status=TaskStatus.TODO)

        mock_kanban.get_all_tasks = AsyncMock(return_value=[task_done])
        await watcher.poll_once()

        reopened_events: List[Any] = []
        events.subscribe("ticket.reopened", lambda e: reopened_events.append(e))

        mock_kanban.get_all_tasks = AsyncMock(return_value=[task_todo])
        await watcher.poll_once()

        assert len(reopened_events) == 1

    @pytest.mark.asyncio
    async def test_emits_ac_changed_on_description_edit(
        self, watcher, events, mock_kanban
    ):
        """ticket.ac_changed is emitted when the AC block changes."""
        desc_v1 = (
            "<!-- MARCUS_AC_START -->\n## Acceptance Criteria\n\n"
            "- [ ] Deploy service\n<!-- MARCUS_AC_END -->"
        )
        desc_v2 = (
            "<!-- MARCUS_AC_START -->\n## Acceptance Criteria\n\n"
            "- [ ] Deploy service\n- [ ] Added new criterion\n<!-- MARCUS_AC_END -->"
        )
        task_v1 = _make_task("T-7", description=desc_v1)
        task_v2 = _make_task("T-7", description=desc_v2)

        mock_kanban.get_all_tasks = AsyncMock(return_value=[task_v1])
        await watcher.poll_once()

        ac_events: List[Any] = []
        events.subscribe("ticket.ac_changed", lambda e: ac_events.append(e))

        mock_kanban.get_all_tasks = AsyncMock(return_value=[task_v2])
        await watcher.poll_once()

        assert len(ac_events) == 1
        assert "Added new criterion" in ac_events[0].data["new_ac_text"]

    @pytest.mark.asyncio
    async def test_multiple_tickets_handled_independently(
        self, watcher, events, mock_kanban
    ):
        """Events for multiple concurrent tickets are emitted independently."""
        tasks = [_make_task(f"T-{i}") for i in range(5)]
        mock_kanban.get_all_tasks = AsyncMock(return_value=tasks)

        new_events: List[Any] = []
        events.subscribe("ticket.new", lambda e: new_events.append(e))

        await watcher.poll_once()
        assert len(new_events) == 5

    @pytest.mark.asyncio
    async def test_get_all_tasks_error_is_propagated(self, watcher, mock_kanban):
        """poll_once raises when get_all_tasks fails."""
        mock_kanban.get_all_tasks = AsyncMock(side_effect=RuntimeError("API down"))
        with pytest.raises(RuntimeError, match="API down"):
            await watcher.poll_once()

    @pytest.mark.asyncio
    async def test_provider_name_included_in_events(self, watcher, events, mock_kanban):
        """Events include the provider name."""
        task = _make_task("T-9")
        mock_kanban.get_all_tasks = AsyncMock(return_value=[task])

        received: List[Any] = []
        events.subscribe("ticket.new", lambda e: received.append(e))
        await watcher.poll_once()

        assert received[0].data["provider"] == "jira"


class TestBoardWatcherStartStop:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_sets_running_true(self, watcher, mock_kanban):
        """start() sets _running = True."""
        mock_kanban.get_all_tasks = AsyncMock(return_value=[])
        await watcher.start()
        assert watcher._running is True
        await watcher.stop()

    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self, watcher, mock_kanban):
        """stop() sets _running = False."""
        mock_kanban.get_all_tasks = AsyncMock(return_value=[])
        await watcher.start()
        await watcher.stop()
        assert watcher._running is False

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, watcher, mock_kanban):
        """Calling start() twice does not spawn duplicate tasks."""
        mock_kanban.get_all_tasks = AsyncMock(return_value=[])
        await watcher.start()
        task1 = watcher._task
        await watcher.start()
        assert watcher._task is task1  # same task object
        await watcher.stop()
