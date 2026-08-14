"""
Unit tests for src/core/board_watcher.py
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import asyncio

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
    async def test_status_changed_task_payload_includes_project_id(
        self, watcher, events, mock_kanban
    ):
        """The emitted task dict carries project_id (from
        source_context["kanboard_task"]["project_id"], the shape
        KanboardKanban._to_task always sets) — needed by project-stats
        tracking, which has no other way to resolve a project id from
        the poll path's event payload alone."""
        task_todo = _make_task(
            "T-4b",
            status=TaskStatus.TODO,
            source_context={"kanboard_task": {"project_id": 7}},
        )
        task_done = _make_task(
            "T-4b",
            status=TaskStatus.DONE,
            source_context={"kanboard_task": {"project_id": 7}},
        )

        mock_kanban.get_all_tasks = AsyncMock(return_value=[task_todo])
        await watcher.poll_once()

        status_events: List[Any] = []
        events.subscribe("ticket.status_changed", lambda e: status_events.append(e))

        mock_kanban.get_all_tasks = AsyncMock(return_value=[task_done])
        await watcher.poll_once()

        assert len(status_events) == 1
        assert status_events[0].data["task"]["project_id"] == 7

    @pytest.mark.asyncio
    async def test_task_payload_project_id_none_when_no_source_context(
        self, watcher, events, mock_kanban
    ):
        """A task with no source_context (non-Kanboard provider) must not
        crash — project_id is simply absent/None."""
        task_todo = _make_task("T-4c", status=TaskStatus.TODO)
        task_done = _make_task("T-4c", status=TaskStatus.DONE)

        mock_kanban.get_all_tasks = AsyncMock(return_value=[task_todo])
        await watcher.poll_once()

        status_events: List[Any] = []
        events.subscribe("ticket.status_changed", lambda e: status_events.append(e))

        mock_kanban.get_all_tasks = AsyncMock(return_value=[task_done])
        await watcher.poll_once()

        assert len(status_events) == 1
        assert status_events[0].data["task"].get("project_id") is None

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


class TestPollOnceConcurrencyAndFreshness:
    """poll_once() is called on demand (every marcus_work poll) as well as by
    the background loop, so it has to be safe to overlap and cheap to repeat.
    """

    @pytest.mark.asyncio
    async def test_concurrent_polls_do_not_double_emit(
        self, watcher, events, mock_kanban
    ):
        """Two overlapping polls must not both report the same ticket as new.

        The background loop and an on-demand poll from marcus_work can land
        together. Without serialisation both diff against the same empty
        snapshot and both emit ticket.new for the same card — which
        downstream means two claims and two contradictory "Started"
        comments on one ticket.
        """
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_get_all_tasks():
            started.set()
            await release.wait()
            return [_make_task("T-1")]

        mock_kanban.get_all_tasks = AsyncMock(side_effect=slow_get_all_tasks)

        received: List[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        events.subscribe("ticket.new", handler)

        first = asyncio.create_task(watcher.poll_once())
        await started.wait()
        second = asyncio.create_task(watcher.poll_once())
        await asyncio.sleep(0)  # let the second call reach the lock
        release.set()
        await asyncio.gather(first, second)

        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_max_age_skips_a_just_completed_poll(self, watcher, mock_kanban):
        """A max_age window coalesces near-simultaneous on-demand polls.

        Several agents each polling every ~10s would otherwise each trigger
        a full board read per poll — two RPCs per enabled project, straight
        into Kanboard's SQLite backend.
        """
        mock_kanban.get_all_tasks = AsyncMock(return_value=[])

        await watcher.poll_once()
        assert mock_kanban.get_all_tasks.await_count == 1

        await watcher.poll_once(max_age=60.0)
        assert mock_kanban.get_all_tasks.await_count == 1  # skipped

    @pytest.mark.asyncio
    async def test_max_age_zero_always_polls(self, watcher, mock_kanban):
        """The default (no window) still reads the board every time."""
        mock_kanban.get_all_tasks = AsyncMock(return_value=[])

        await watcher.poll_once()
        await watcher.poll_once()

        assert mock_kanban.get_all_tasks.await_count == 2


class TestDeletedTicketDetection:
    """A ticket that vanishes from the board must be reported as deleted —
    but only once it is confirmed gone.

    Marcus selects work from lifecycle records, not from the board, so a
    record left behind after a human deletes a ticket is still handed to an
    agent. The watcher previously just dropped its snapshot at debug level
    and emitted nothing, so nothing downstream ever learned.

    Disappearing from get_all_tasks() is NOT proof of deletion, though:
    Marcus only reads the projects it is enabled for, so every ticket on a
    project a human just switched OFF also vanishes from that call. Purging
    those would destroy live state — including claims on tickets an agent is
    actively working.
    """

    @pytest.mark.asyncio
    async def test_emits_ticket_deleted_when_confirmed_gone(
        self, watcher, events, mock_kanban
    ):
        """Gone from the board AND gone on direct lookup → deleted."""
        mock_kanban.get_all_tasks = AsyncMock(return_value=[_make_task("T-1")])
        await watcher.poll_once()

        received: List[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        events.subscribe("ticket.deleted", handler)
        mock_kanban.get_all_tasks = AsyncMock(return_value=[])
        mock_kanban.get_task_by_id = AsyncMock(return_value=None)

        await watcher.poll_once()

        assert len(received) == 1
        assert received[0].data["ticket_id"] == "T-1"
        assert "T-1" not in watcher._snapshots

    @pytest.mark.asyncio
    async def test_out_of_scope_ticket_is_not_reported_deleted(
        self, watcher, events, mock_kanban
    ):
        """Gone from the board but still resolvable → out of scope, not
        deleted. Its snapshot is kept so re-enabling the project doesn't
        replay it as a brand-new ticket."""
        mock_kanban.get_all_tasks = AsyncMock(return_value=[_make_task("T-1")])
        await watcher.poll_once()

        received: List[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        events.subscribe("ticket.deleted", handler)
        mock_kanban.get_all_tasks = AsyncMock(return_value=[])
        mock_kanban.get_task_by_id = AsyncMock(return_value=_make_task("T-1"))

        await watcher.poll_once()

        assert received == []
        assert "T-1" in watcher._snapshots

    @pytest.mark.asyncio
    async def test_lookup_failure_is_not_treated_as_deletion(
        self, watcher, events, mock_kanban
    ):
        """A failed confirmation lookup must never purge anything — a
        transient Kanboard error would otherwise wipe live tickets."""
        mock_kanban.get_all_tasks = AsyncMock(return_value=[_make_task("T-1")])
        await watcher.poll_once()

        received: List[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        events.subscribe("ticket.deleted", handler)
        mock_kanban.get_all_tasks = AsyncMock(return_value=[])
        mock_kanban.get_task_by_id = AsyncMock(
            side_effect=RuntimeError("kanboard unreachable")
        )

        await watcher.poll_once()

        assert received == []
        assert "T-1" in watcher._snapshots
