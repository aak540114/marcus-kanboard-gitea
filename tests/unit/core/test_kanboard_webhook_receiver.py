"""
Unit tests for KanboardWebhookReceiver.

Verifies that raw Kanboard webhook payloads are correctly translated into
Marcus internal events and that token validation works correctly.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.kanboard_webhook_receiver import KanboardWebhookReceiver


@pytest.fixture
def mock_events():
    """Create a mock Marcus Events bus."""
    events = MagicMock()
    events.publish = AsyncMock()
    return events


@pytest.fixture
def receiver(mock_events):
    """Create a KanboardWebhookReceiver with no secret token (validation off)."""
    return KanboardWebhookReceiver(events=mock_events, provider="kanboard")


@pytest.fixture
def secured_receiver(mock_events):
    """Create a KanboardWebhookReceiver with a required secret token."""
    return KanboardWebhookReceiver(
        events=mock_events,
        provider="kanboard",
        secret_token="secret123",
    )


def _body(event_name: str, event_data: dict) -> bytes:
    """Build a Kanboard webhook body as raw bytes."""
    return json.dumps({"event_name": event_name, "event_data": event_data}).encode()


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------


class TestTokenValidation:
    """Test token-validation logic."""

    @pytest.mark.asyncio
    async def test_no_secret_accepts_any_request(self, receiver, mock_events):
        """Test that requests are accepted when no secret is configured."""
        body = _body("task.create", {"task": {"id": 1, "title": "test"}})
        result = await receiver.handle_request(body)
        assert result is True

    @pytest.mark.asyncio
    async def test_correct_query_token_accepted(self, secured_receiver):
        """Test that correct query-param token is accepted."""
        body = _body("task.create", {"task": {"id": 1, "title": "test"}})
        result = await secured_receiver.handle_request(body, token="secret123")
        assert result is True

    @pytest.mark.asyncio
    async def test_correct_header_token_accepted(self, secured_receiver):
        """Test that correct header token is accepted."""
        body = _body("task.create", {"task": {"id": 1, "title": "test"}})
        result = await secured_receiver.handle_request(
            body, header_token="secret123"
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_wrong_token_rejected(self, secured_receiver, mock_events):
        """Test that wrong token is rejected without emitting events."""
        body = _body("task.create", {"task": {"id": 1, "title": "test"}})
        result = await secured_receiver.handle_request(body, token="wrong")
        assert result is False
        mock_events.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_token_rejected(self, secured_receiver, mock_events):
        """Test that missing token is rejected when a secret is configured."""
        body = _body("task.create", {"task": {"id": 1, "title": "test"}})
        result = await secured_receiver.handle_request(body)
        assert result is False
        mock_events.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


class TestMalformedInput:
    """Test handling of bad payloads."""

    @pytest.mark.asyncio
    async def test_invalid_json_rejected(self, receiver, mock_events):
        """Test that non-JSON bodies are rejected gracefully."""
        result = await receiver.handle_request(b"not json at all")
        assert result is False
        mock_events.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_event_name_no_emit(self, receiver, mock_events):
        """Test that payloads without event_name emit nothing and return True."""
        body = json.dumps({"event_data": {}}).encode()
        result = await receiver.handle_request(body)
        assert result is True
        mock_events.publish.assert_not_called()


# ---------------------------------------------------------------------------
# task.move.column → ticket.status_changed
# ---------------------------------------------------------------------------


class TestMoveColumn:
    """Test column-move event translation.

    Kanboard's real webhook payload names the column ``column_title`` on
    the task hash (from ``TaskFinderModel::getDetails()`` — confirmed
    against Kanboard's own source), not ``column_name``. These fixtures
    use ``column_title`` to match what Kanboard actually sends; a prior
    version of this test suite used ``column_name``, which happened to
    match the receiver's (buggy) implementation but never matched a real
    webhook payload — every column move silently fell back to "todo" in
    production despite this test suite passing.
    """

    @pytest.mark.asyncio
    async def test_move_column_emits_status_changed(self, receiver, mock_events):
        """Test that task.move.column emits ticket.status_changed with the mapped status."""
        body = _body(
            "task.move.column",
            {
                "task": {"id": 42, "column_title": "In Progress"},
                "changes": {"column_id": "3", "src_column_id": "2", "dst_column_id": "3"},
            },
        )
        result = await receiver.handle_request(body)
        assert result is True
        mock_events.publish.assert_called_once()
        call_kwargs = mock_events.publish.call_args

        assert call_kwargs[0][0] == "ticket.status_changed"
        data = call_kwargs[1]["data"]
        assert data["ticket_id"] == "42"
        assert data["new_status"] == "in_progress"
        assert data["old_status"] is None
        assert data["provider"] == "kanboard"

    @pytest.mark.asyncio
    async def test_move_to_ready_column_emits_ready_status(self, receiver, mock_events):
        """Test that moving a task to the 'Ready' column emits new_status='ready'.

        Regression test: the receiver previously read a 'column_name' key
        that real Kanboard webhook payloads never populate, so this exact
        scenario (dragging a card to Ready) always emitted 'todo' instead.
        """
        body = _body(
            "task.move.column",
            {
                "task": {"id": 1, "column_title": "Ready"},
                "changes": {"column_id": "2", "src_column_id": "1", "dst_column_id": "2"},
            },
        )
        await receiver.handle_request(body)
        data = mock_events.publish.call_args[1]["data"]
        assert data["new_status"] == "ready"

    @pytest.mark.asyncio
    async def test_column_name_accepted_as_fallback(self, receiver, mock_events):
        """Test that 'column_name' is still accepted if a payload happens to carry it."""
        body = _body(
            "task.move.column",
            {
                "task": {"id": 43, "column_name": "Ready"},
                "changes": {},
            },
        )
        await receiver.handle_request(body)
        data = mock_events.publish.call_args[1]["data"]
        assert data["new_status"] == "ready"

    @pytest.mark.asyncio
    async def test_unknown_column_defaults_to_todo(self, receiver, mock_events):
        """Test that unrecognised column names default to 'todo' status."""
        body = _body(
            "task.move.column",
            {
                "task": {"id": 7, "column_title": "Someday/Maybe"},
                "changes": {},
            },
        )
        await receiver.handle_request(body)
        data = mock_events.publish.call_args[1]["data"]
        assert data["new_status"] == "todo"


# ---------------------------------------------------------------------------
# task.assignee_change → ticket.assigned / ticket.unassigned
# ---------------------------------------------------------------------------


class TestAssigneeChange:
    """Test assignee-change event translation."""

    @pytest.mark.asyncio
    async def test_assign_emits_ticket_assigned(self, receiver, mock_events):
        """Test that setting owner_id emits ticket.assigned."""
        body = _body(
            "task.assignee_change",
            {
                "task": {
                    "id": 10,
                    "owner_id": "5",
                    "assignee_username": "alice",
                }
            },
        )
        await receiver.handle_request(body)
        call = mock_events.publish.call_args
        assert call[0][0] == "ticket.assigned"
        assert call[1]["data"]["assignee"] == "alice"

    @pytest.mark.asyncio
    async def test_unassign_emits_ticket_unassigned(self, receiver, mock_events):
        """Test that clearing owner_id (to '0') emits ticket.unassigned."""
        body = _body(
            "task.assignee_change",
            {"task": {"id": 10, "owner_id": "0"}},
        )
        await receiver.handle_request(body)
        call = mock_events.publish.call_args
        assert call[0][0] == "ticket.unassigned"


# ---------------------------------------------------------------------------
# task.close / task.open → ticket.closed / ticket.reopened
# ---------------------------------------------------------------------------


class TestCloseOpen:
    """Test close/open event translation."""

    @pytest.mark.asyncio
    async def test_task_close_emits_ticket_closed(self, receiver, mock_events):
        """Test that task.close emits ticket.closed."""
        body = _body("task.close", {"task": {"id": 3}})
        await receiver.handle_request(body)
        assert mock_events.publish.call_args[0][0] == "ticket.closed"

    @pytest.mark.asyncio
    async def test_task_open_emits_ticket_reopened(self, receiver, mock_events):
        """Test that task.open emits ticket.reopened."""
        body = _body("task.open", {"task": {"id": 3}})
        await receiver.handle_request(body)
        assert mock_events.publish.call_args[0][0] == "ticket.reopened"


# ---------------------------------------------------------------------------
# task.create → ticket.new
# ---------------------------------------------------------------------------


class TestTaskCreate:
    """Test task creation event translation."""

    @pytest.mark.asyncio
    async def test_task_create_emits_ticket_new(self, receiver, mock_events):
        """Test that task.create emits ticket.new."""
        body = _body("task.create", {"task": {"id": 99, "title": "brand new"}})
        await receiver.handle_request(body)
        assert mock_events.publish.call_args[0][0] == "ticket.new"
        assert mock_events.publish.call_args[1]["data"]["ticket_id"] == "99"


# ---------------------------------------------------------------------------
# comment.create / comment.update → ticket.comment_added
# ---------------------------------------------------------------------------


class TestComment:
    """Test comment event translation."""

    @pytest.mark.asyncio
    async def test_comment_create_emits_comment_added(self, receiver, mock_events):
        """Test that comment.create emits ticket.comment_added with body and author."""
        body = _body(
            "comment.create",
            {
                "task": {"id": 5},
                "comment": {"id": 77, "comment": "Hello!", "username": "bob"},
            },
        )
        await receiver.handle_request(body)
        call = mock_events.publish.call_args
        assert call[0][0] == "ticket.comment_added"
        data = call[1]["data"]
        assert data["comment_body"] == "Hello!"
        assert data["comment_author"] == "bob"
        assert data["comment_id"] == "77"

    @pytest.mark.asyncio
    async def test_comment_update_also_emits_comment_added(self, receiver, mock_events):
        """Test that comment.update also emits ticket.comment_added."""
        body = _body(
            "comment.update",
            {
                "task": {"id": 5},
                "comment": {"id": 77, "comment": "Edited!", "username": "bob"},
            },
        )
        await receiver.handle_request(body)
        assert mock_events.publish.call_args[0][0] == "ticket.comment_added"


# ---------------------------------------------------------------------------
# task.update with column change
# ---------------------------------------------------------------------------


class TestTaskUpdate:
    """Test task.update event when it includes a column change."""

    @pytest.mark.asyncio
    async def test_task_update_with_column_change_emits_status_changed(
        self, receiver, mock_events
    ):
        """Test that task.update with column_id change emits ticket.status_changed."""
        body = _body(
            "task.update",
            {
                "task": {"id": 8, "column_title": "Done"},
                "changes": {"column_id": "5"},
            },
        )
        await receiver.handle_request(body)
        assert mock_events.publish.call_args[0][0] == "ticket.status_changed"

    @pytest.mark.asyncio
    async def test_task_update_without_column_change_no_emit(
        self, receiver, mock_events
    ):
        """Test that task.update without a column change does not emit events."""
        body = _body(
            "task.update",
            {
                "task": {"id": 8, "title": "New title"},
                "changes": {"title": "New title"},
            },
        )
        await receiver.handle_request(body)
        mock_events.publish.assert_not_called()


# ---------------------------------------------------------------------------
# KANBOARD_WEBHOOK_TOKEN env var
# ---------------------------------------------------------------------------


class TestEnvToken:
    """Test that KANBOARD_WEBHOOK_TOKEN env var is respected."""

    @pytest.mark.asyncio
    async def test_env_token_used_when_no_explicit_token(self, mock_events):
        """Test that KANBOARD_WEBHOOK_TOKEN env var is picked up by the receiver."""
        with patch.dict("os.environ", {"KANBOARD_WEBHOOK_TOKEN": "envtoken"}):
            rcv = KanboardWebhookReceiver(events=mock_events)
        body = _body("task.create", {"task": {"id": 1}})
        result = await rcv.handle_request(body, token="wrong")
        assert result is False

        result = await rcv.handle_request(body, token="envtoken")
        assert result is True


class TestTaskRemove:
    """task.remove → ticket.deleted.

    Kanboard itself never fires this: TaskModel::remove() deletes the row
    without dispatching anything, and there is no EVENT_REMOVE constant
    (checked against v1.2.53). The MarcusDevEnv plugin supplies it by
    overriding the model and POSTing this event name, so a deletion reaches
    Marcus instantly instead of waiting to be noticed on the next board
    read.
    """

    @pytest.mark.asyncio
    async def test_publishes_ticket_deleted(self, receiver, mock_events):
        """A task.remove payload becomes a ticket.deleted event."""
        body = _body("task.remove", {"task": {"id": 21, "project_id": 8}})

        assert await receiver.handle_request(body) is True

        published = [c for c in mock_events.publish.call_args_list]
        assert any(
            (c.args and c.args[0] == "ticket.deleted")
            or c.kwargs.get("event_type") == "ticket.deleted"
            for c in published
        )
        data = published[-1].kwargs["data"]
        assert data["ticket_id"] == "21"

    @pytest.mark.asyncio
    async def test_accepts_a_bare_task_id(self, receiver, mock_events):
        """The row is already gone when the plugin fires, so the payload may
        carry only the id."""
        body = _body("task.remove", {"task_id": 21})

        await receiver.handle_request(body)

        data = mock_events.publish.call_args_list[-1].kwargs["data"]
        assert data["ticket_id"] == "21"

    @pytest.mark.asyncio
    async def test_unknown_task_id_publishes_nothing(self, receiver, mock_events):
        """A payload with no resolvable id is ignored rather than emitting a
        deletion for the empty string."""
        body = _body("task.remove", {})

        await receiver.handle_request(body)

        mock_events.publish.assert_not_called()


class TestBackgroundDispatch:
    """``background=True`` acks Kanboard before processing the event.

    Kanboard's outbound webhook call is a synchronous, blocking HTTP
    request made from *within* the PHP request that changed the ticket
    (see Kanboard's ``WebhookNotification::sendMessage`` — a plain
    ``httpClient->postJson()`` call, no queue). If Marcus doesn't respond
    until it has finished handling the event — including any RPC calls
    Marcus itself makes back to Kanboard (e.g. moving a "waiting for
    human" ticket to "done" merges to main, then moves the column and
    posts a comment via the JSON-RPC API) — Kanboard's own request stays
    open the whole time. Those write-back calls are brand new HTTP
    requests hitting the very same SQLite-backed Kanboard instance while
    the original request is still in flight, which is exactly the
    "database is locked" collision Kanboard users can hit when moving a
    ticket to Done. ``background=True`` breaks that: Marcus acks as soon
    as the payload is validated and parsed, and does the actual dispatch
    (and any resulting writes back to Kanboard) after responding.
    """

    @pytest.mark.asyncio
    async def test_returns_before_dispatch_runs(self, receiver, mock_events):
        """The call returns without waiting for the event to be handled."""
        body = _body("task.close", {"task": {"id": "5"}})

        result = await receiver.handle_request(body, background=True)

        assert result is True
        mock_events.publish.assert_not_called()  # not yet — still pending

        await asyncio.sleep(0)  # let the scheduled task run

        mock_events.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_token_still_rejected_synchronously(
        self, secured_receiver, mock_events
    ):
        """A bad token is rejected immediately — nothing gets scheduled."""
        body = _body("task.close", {"task": {"id": "5"}})

        result = await secured_receiver.handle_request(
            body, token="wrong", background=True
        )

        assert result is False
        await asyncio.sleep(0)
        mock_events.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_malformed_json_still_rejected_synchronously(
        self, receiver, mock_events
    ):
        """Malformed JSON is rejected immediately — nothing gets scheduled."""
        result = await receiver.handle_request(b"not json", background=True)

        assert result is False
        await asyncio.sleep(0)
        mock_events.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_error_is_logged_not_raised(self, receiver, mock_events):
        """A handler exception during background dispatch must not escape —
        there is no caller left awaiting it by the time it happens."""
        mock_events.publish = AsyncMock(side_effect=RuntimeError("boom"))
        body = _body("task.close", {"task": {"id": "5"}})

        result = await receiver.handle_request(body, background=True)
        assert result is True

        await asyncio.sleep(0)  # the background task runs and raises —

        # ...and the test reaching this line at all is the assertion: a
        # raised exception inside the background task would otherwise
        # surface as "Task exception was never retrieved" at GC time, not
        # here, but it must not propagate through handle_request itself.
