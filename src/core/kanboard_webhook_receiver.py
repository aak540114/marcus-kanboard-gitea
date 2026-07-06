"""
Kanboard push-webhook receiver.

Kanboard can POST a JSON payload to a configured URL whenever a task changes.
This module parses those payloads and re-emits them as Marcus internal events
(the same event names that ``BoardWatcher`` produces from polling), so that
``HumanGatedWorkflow`` and other subscribers react instantly rather than
waiting for the next poll cycle.

Event mapping
-------------
Kanboard event          → Marcus event
``task.move.column``    → ``ticket.status_changed``
``task.update``         → ``ticket.status_changed`` (if column changed)
``task.assignee.change``→ ``ticket.assigned`` / ``ticket.unassigned``
``task.close``          → ``ticket.closed``
``task.open``           → ``ticket.reopened``
``task.create``         → ``ticket.new``
``comment.create``      → ``ticket.comment_added``
``comment.update``      → ``ticket.comment_added``

Usage
-----
Instantiate one ``KanboardWebhookReceiver`` per server and call
``handle_request(request_body_bytes, secret_token)`` from the HTTP route
handler.  The receiver requires access to the shared ``Events`` bus.

The Kanboard webhook URL must be configured manually in the Kanboard UI:
    Settings → Integrations → Webhook URL
    → ``http://host.docker.internal:4298/webhooks/kanboard``

Set ``KANBOARD_WEBHOOK_TOKEN`` env var and the same value in Kanboard's
"Webhook Token" field; the receiver will validate the token query-param that
Kanboard appends (``?token=<value>``).  Leave both unset to skip validation
(development only).
"""

import json
import logging
import os
from typing import Any, Dict, Optional

from src.core.events import Events

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column-name → TaskStatus name mapping (kept in sync with KanboardKanban)
# ---------------------------------------------------------------------------
_COLUMN_STATUS_MAP: Dict[str, str] = {
    # TODO-family
    "backlog": "todo",
    "todo": "todo",
    "to do": "todo",
    "open": "todo",
    "new": "todo",
    "queue": "todo",
    # READY-family
    "ready": "ready",
    # IN_PROGRESS-family
    "in progress": "in_progress",
    "in development": "in_progress",
    "wip": "in_progress",
    "work in progress": "in_progress",
    "doing": "in_progress",
    "active": "in_progress",
    "development": "in_progress",
    "review": "in_progress",
    "in review": "in_progress",
    "testing": "in_progress",
    # WAITING_FOR_HUMAN-family
    "waiting for human": "waiting_for_human",
    "waiting": "waiting_for_human",
    "pending review": "waiting_for_human",
    # BLOCKED-family
    "blocked": "blocked",
    "block": "blocked",
    "impediment": "blocked",
    "on hold": "blocked",
    "hold": "blocked",
    # DONE-family
    "done": "done",
    "closed": "done",
    "complete": "done",
    "completed": "done",
    "finished": "done",
    "resolved": "done",
    "archive": "done",
    "archived": "done",
}

# Kanboard event names
_EV_MOVE = "task.move.column"
_EV_UPDATE = "task.update"
_EV_ASSIGN = "task.assignee.change"
_EV_CLOSE = "task.close"
_EV_OPEN = "task.open"
_EV_CREATE = "task.create"
_EV_COMMENT_CREATE = "comment.create"
_EV_COMMENT_UPDATE = "comment.update"


class KanboardWebhookReceiver:
    """
    Translates raw Kanboard webhook payloads into Marcus ``Events`` bus events.

    Parameters
    ----------
    events : Events
        Shared Marcus event bus.
    provider : str
        Provider name string to stamp on emitted events (default ``"kanboard"``).
    secret_token : Optional[str]
        If set, incoming requests must carry ``?token=<secret_token>`` or a
        ``X-Kanboard-Token`` header matching this value.  Mismatches are
        rejected (logged and not emitted).
    """

    def __init__(
        self,
        events: Events,
        provider: str = "kanboard",
        secret_token: Optional[str] = None,
    ) -> None:
        self._events = events
        self._provider = provider
        self._secret_token = secret_token or os.getenv("KANBOARD_WEBHOOK_TOKEN")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def handle_request(
        self,
        body: bytes,
        *,
        token: Optional[str] = None,
        header_token: Optional[str] = None,
    ) -> bool:
        """
        Parse a raw Kanboard webhook request body and emit Marcus events.

        Parameters
        ----------
        body : bytes
            Raw HTTP request body (JSON).
        token : Optional[str]
            Value of the ``?token=`` query parameter (Kanboard appends this).
        header_token : Optional[str]
            Value of the ``X-Kanboard-Token`` HTTP header, if present.

        Returns
        -------
        bool
            ``True`` if the payload was accepted and processed;
            ``False`` if rejected (bad token or malformed body).
        """
        if not self._validate_token(token, header_token):
            logger.warning("Kanboard webhook rejected: invalid token")
            return False

        try:
            payload: Dict[str, Any] = json.loads(body)
        except json.JSONDecodeError as exc:
            logger.warning("Kanboard webhook: malformed JSON — %s", exc)
            return False

        event_name = payload.get("event_name", "")
        event_data = payload.get("event_data", {})

        logger.debug("Kanboard webhook received: %s", event_name)

        try:
            await self._dispatch(event_name, event_data)
        except Exception:
            logger.exception("Error processing Kanboard webhook event %s", event_name)
            return False

        return True

    # ------------------------------------------------------------------
    # Token validation
    # ------------------------------------------------------------------

    def _validate_token(
        self, query_token: Optional[str], header_token: Optional[str]
    ) -> bool:
        """Return True if the request token matches the configured secret."""
        if not self._secret_token:
            return True  # validation disabled
        provided = query_token or header_token
        return provided == self._secret_token

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, event_name: str, event_data: Dict[str, Any]) -> None:
        """Route a single Kanboard event to the appropriate Marcus event."""
        task = event_data.get("task", {})
        ticket_id = str(task.get("id", event_data.get("task_id", "")))

        if event_name == _EV_MOVE:
            await self._handle_move_column(ticket_id, event_data, task)

        elif event_name == _EV_UPDATE:
            # task.update fires for many fields; only care if column changed
            changes = event_data.get("changes", {})
            if "column_id" in changes or "column_name" in changes:
                await self._handle_move_column(ticket_id, event_data, task)

        elif event_name == _EV_ASSIGN:
            await self._handle_assignee_change(ticket_id, event_data, task)

        elif event_name == _EV_CLOSE:
            await self._events.publish(
                "ticket.closed",
                source="kanboard_webhook",
                data={
                    "ticket_id": ticket_id,
                    "provider": self._provider,
                    "task": task,
                },
            )
            logger.info("Webhook → ticket.closed  (ticket=%s)", ticket_id)

        elif event_name == _EV_OPEN:
            await self._events.publish(
                "ticket.reopened",
                source="kanboard_webhook",
                data={
                    "ticket_id": ticket_id,
                    "provider": self._provider,
                    "task": task,
                },
            )
            logger.info("Webhook → ticket.reopened  (ticket=%s)", ticket_id)

        elif event_name == _EV_CREATE:
            await self._events.publish(
                "ticket.new",
                source="kanboard_webhook",
                data={
                    "ticket_id": ticket_id,
                    "provider": self._provider,
                    "task": task,
                },
            )
            logger.info("Webhook → ticket.new  (ticket=%s)", ticket_id)

        elif event_name in (_EV_COMMENT_CREATE, _EV_COMMENT_UPDATE):
            await self._handle_comment(ticket_id, event_data, task)

        else:
            logger.debug("Kanboard webhook: unhandled event '%s'", event_name)

    # ------------------------------------------------------------------
    # Specific event handlers
    # ------------------------------------------------------------------

    async def _handle_move_column(
        self,
        ticket_id: str,
        event_data: Dict[str, Any],
        task: Dict[str, Any],
    ) -> None:
        """Emit ``ticket.status_changed`` from a column-move event."""
        task_data = event_data.get("task", task)
        column_name: str = (task_data.get("column_name") or "").lower().strip()

        changes = event_data.get("changes", {})
        old_column: str = str(
            changes.get("old_column_name", changes.get("column_name", ""))
        ).lower().strip()

        new_status = _COLUMN_STATUS_MAP.get(column_name, "todo")
        old_status = _COLUMN_STATUS_MAP.get(old_column, "todo")

        await self._events.publish(
            "ticket.status_changed",
            source="kanboard_webhook",
            data={
                "ticket_id": ticket_id,
                "provider": self._provider,
                "old_status": old_status,
                "new_status": new_status,
                "task": task_data,
            },
        )
        logger.info(
            "Webhook → ticket.status_changed  (ticket=%s, %s→%s)",
            ticket_id,
            old_status,
            new_status,
        )

    async def _handle_assignee_change(
        self,
        ticket_id: str,
        event_data: Dict[str, Any],
        task: Dict[str, Any],
    ) -> None:
        """Emit ``ticket.assigned`` or ``ticket.unassigned`` from an assignee event."""
        task_data = event_data.get("task", task)
        owner_id = str(task_data.get("owner_id", "0"))

        if owner_id and owner_id != "0":
            assignee = task_data.get("assignee_username") or owner_id
            await self._events.publish(
                "ticket.assigned",
                source="kanboard_webhook",
                data={
                    "ticket_id": ticket_id,
                    "provider": self._provider,
                    "assignee": assignee,
                    "task": task_data,
                },
            )
            logger.info(
                "Webhook → ticket.assigned  (ticket=%s, assignee=%s)",
                ticket_id,
                assignee,
            )
        else:
            await self._events.publish(
                "ticket.unassigned",
                source="kanboard_webhook",
                data={
                    "ticket_id": ticket_id,
                    "provider": self._provider,
                    "task": task_data,
                },
            )
            logger.info("Webhook → ticket.unassigned  (ticket=%s)", ticket_id)

    async def _handle_comment(
        self,
        ticket_id: str,
        event_data: Dict[str, Any],
        task: Dict[str, Any],
    ) -> None:
        """Emit ``ticket.comment_added`` from a comment event."""
        comment = event_data.get("comment", {})
        await self._events.publish(
            "ticket.comment_added",
            source="kanboard_webhook",
            data={
                "ticket_id": ticket_id,
                "provider": self._provider,
                "comment_body": comment.get("comment", ""),
                "comment_author": comment.get("username", "unknown"),
                "comment_id": str(comment.get("id", "")),
                "task": task,
            },
        )
        logger.info("Webhook → ticket.comment_added  (ticket=%s)", ticket_id)
