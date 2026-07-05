"""
Kanban board polling and change-event detection.

The ``BoardWatcher`` polls a kanban provider on a configurable interval
and emits events whenever something relevant changes:

- A ticket is assigned to a human (AI should start work).
- A ticket's status changes (e.g. closed = human accepted).
- A new human comment is posted (AI may need to respond).
- The acceptance criteria block in a ticket description is edited.
- A ticket is reopened (AI should rebase and resume).

Events are emitted via the existing Marcus ``Events`` bus so that any
subscriber (the ``HumanGatedWorkflow``, analytics, logging, etc.) can
react without tight coupling.

Event names
-----------
``ticket.assigned``
    ``{ticket_id, provider, assignee, task}``
``ticket.unassigned``
    ``{ticket_id, provider, task}``
``ticket.status_changed``
    ``{ticket_id, provider, old_status, new_status, task}``
``ticket.closed``
    ``{ticket_id, provider, task}``
``ticket.reopened``
    ``{ticket_id, provider, task}``
``ticket.comment_added``
    ``{ticket_id, provider, comment_body, comment_author, comment_id, task}``
``ticket.ac_changed``
    ``{ticket_id, provider, old_hash, new_hash, new_ac_text, task}``
``ticket.new``
    ``{ticket_id, provider, task}`` — ticket seen for the first time

Classes
-------
TicketSnapshot
    Lightweight snapshot of a ticket used for diffing.
BoardWatcher
    Polls the kanban board and emits change events.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

from src.core.acceptance_criteria import ACChangeDetector, ACParser
from src.core.events import Events
from src.core.models import Task, TaskStatus
from src.integrations.kanban_interface import KanbanInterface

logger = logging.getLogger(__name__)


@dataclass
class TicketSnapshot:
    """Lightweight snapshot of ticket state for change diffing.

    Parameters
    ----------
    ticket_id : str
        Ticket identifier.
    assignee : Optional[str]
        Username of the current assignee (``None`` if unassigned).
    status : TaskStatus
        Current task status.
    ac_hash : str
        SHA-256 hash of the acceptance criteria block.  Empty string if
        the ticket has no Marcus AC block.
    comment_ids : Set[str]
        Set of known comment IDs.
    is_closed : bool
        ``True`` when the ticket has been transitioned to ``DONE``.
    last_seen : datetime
        When this snapshot was taken.
    """

    ticket_id: str
    assignee: Optional[str] = None
    status: TaskStatus = TaskStatus.TODO
    ac_hash: str = ""
    comment_ids: Set[str] = field(default_factory=set)
    is_closed: bool = False
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class BoardWatcher:
    """Polls a kanban provider and emits change events.

    Parameters
    ----------
    kanban : KanbanInterface
        Connected kanban provider.
    events : Events
        Marcus event bus to publish to.
    provider_name : str
        Short label for the kanban provider (e.g. ``"github"``, ``"jira"``).
    poll_interval : float
        Seconds between polls.  Default 30 s — enough for responsive
        human feedback without hammering the API rate-limits.
    on_error : Optional[Callable]
        Async callable invoked when a poll cycle raises an unhandled
        exception.  Signature: ``async (exc: Exception) -> None``.
    """

    def __init__(
        self,
        kanban: KanbanInterface,
        events: Events,
        provider_name: str,
        poll_interval: float = 30.0,
        on_error: Optional[Callable[..., Coroutine[Any, Any, None]]] = None,
    ) -> None:
        """Initialise the board watcher."""
        self._kanban = kanban
        self._events = events
        self._provider = provider_name
        self._poll_interval = poll_interval
        self._on_error = on_error

        # ticket_id → TicketSnapshot (persisted across poll cycles)
        self._snapshots: Dict[str, TicketSnapshot] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the polling loop in the background.

        Does nothing if already running.
        """
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="board-watcher")
        logger.info(
            "BoardWatcher started for provider=%s interval=%.0fs",
            self._provider,
            self._poll_interval,
        )

    async def stop(self) -> None:
        """Stop the polling loop and wait for the current cycle to finish."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("BoardWatcher stopped for provider=%s", self._provider)

    async def poll_once(self) -> None:
        """Run a single poll cycle (useful for testing / on-demand checks)."""
        await self._run_poll_cycle()

    # ------------------------------------------------------------------
    # Core polling logic
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Continuously poll the board until ``stop()`` is called."""
        while self._running:
            try:
                await self._run_poll_cycle()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("BoardWatcher poll error: %s", exc, exc_info=True)
                if self._on_error:
                    try:
                        await self._on_error(exc)
                    except Exception:  # noqa: BLE001
                        pass
            # Sleep in small chunks so cancellation is responsive.
            remaining = self._poll_interval
            while remaining > 0 and self._running:
                await asyncio.sleep(min(remaining, 5.0))
                remaining -= 5.0

    async def _run_poll_cycle(self) -> None:
        """Fetch all tasks and emit change events for anything that differs."""
        try:
            tasks: List[Task] = await self._kanban.get_all_tasks()
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_all_tasks() failed: %s", exc)
            raise

        # Also try to fetch comments for tickets we're tracking — not all
        # providers expose this through get_all_tasks, so we handle missing
        # data gracefully.
        seen_ids: Set[str] = set()

        for task in tasks:
            tid = task.id
            seen_ids.add(tid)
            await self._process_task(task)

        # Detect tickets that disappeared from the board (likely deleted).
        for tid in list(self._snapshots.keys()):
            if tid not in seen_ids:
                logger.debug("Ticket %s no longer visible on board", tid)
                del self._snapshots[tid]

    async def _process_task(self, task: Task) -> None:
        """Diff a single task against its stored snapshot and emit events."""
        tid = task.id
        prev = self._snapshots.get(tid)

        if prev is None:
            # First time we see this ticket.
            await self._emit("ticket.new", task, {"ticket_id": tid})
            snap = await self._build_snapshot(task)
            self._snapshots[tid] = snap
            return

        # ------ Assignment changes ------
        current_assignee = self._extract_assignee(task)
        if current_assignee != prev.assignee:
            if current_assignee and not prev.assignee:
                await self._emit(
                    "ticket.assigned",
                    task,
                    {"ticket_id": tid, "assignee": current_assignee},
                )
            elif not current_assignee and prev.assignee:
                await self._emit("ticket.unassigned", task, {"ticket_id": tid})
            else:
                # Re-assigned to a different person.
                await self._emit(
                    "ticket.assigned",
                    task,
                    {"ticket_id": tid, "assignee": current_assignee},
                )

        # ------ Status / closed changes ------
        is_now_closed = task.status == TaskStatus.DONE
        was_closed = prev.is_closed

        if task.status != prev.status:
            payload: Dict[str, Any] = {
                "ticket_id": tid,
                "old_status": prev.status.value,
                "new_status": task.status.value,
            }
            await self._emit("ticket.status_changed", task, payload)

            if is_now_closed and not was_closed:
                await self._emit("ticket.closed", task, {"ticket_id": tid})
            elif not is_now_closed and was_closed:
                # Status went from DONE to something else = reopened.
                await self._emit("ticket.reopened", task, {"ticket_id": tid})

        # ------ Acceptance criteria changes ------
        current_ac = self._extract_ac(task)
        if current_ac:
            changed, new_hash = ACChangeDetector.check(current_ac, prev.ac_hash)
            if changed:
                await self._emit(
                    "ticket.ac_changed",
                    task,
                    {
                        "ticket_id": tid,
                        "old_hash": prev.ac_hash,
                        "new_hash": new_hash,
                        "new_ac_text": current_ac,
                    },
                )
        else:
            new_hash = prev.ac_hash

        # ------ New comments ------
        await self._check_comments(task, prev)

        # Update snapshot.
        self._snapshots[tid] = TicketSnapshot(
            ticket_id=tid,
            assignee=current_assignee,
            status=task.status,
            ac_hash=new_hash,
            comment_ids=prev.comment_ids,  # updated inside _check_comments
            is_closed=is_now_closed,
        )

    async def _check_comments(self, task: Task, prev: TicketSnapshot) -> None:
        """Fetch comments and emit events for any new ones."""
        try:
            # Comments are surfaced via source_context["comments"] by providers
            # that support them.  get_attachments is not used here but kept as
            # a hook for future provider-specific comment fetching.
            comments: List[Dict[str, Any]] = (
                task.source_context.get("comments", []) if task.source_context else []
            )
        except Exception:  # noqa: BLE001
            comments = []

        for comment in comments:
            cid = str(comment.get("id", ""))
            if not cid or cid in prev.comment_ids:
                continue
            prev.comment_ids.add(cid)
            body = comment.get("body", comment.get("content", ""))
            author = comment.get(
                "author", comment.get("user", {}).get("name", "unknown")
            )
            await self._emit(
                "ticket.comment_added",
                task,
                {
                    "ticket_id": task.id,
                    "comment_body": body,
                    "comment_author": author,
                    "comment_id": cid,
                },
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _build_snapshot(self, task: Task) -> TicketSnapshot:
        """Build a fresh snapshot from *task*."""
        ac_text = self._extract_ac(task)
        ac_hash = ACChangeDetector.hash_ac(ac_text) if ac_text else ""
        comments: List[Dict[str, Any]] = (
            task.source_context.get("comments", []) if task.source_context else []
        )
        comment_ids = {str(c.get("id", "")) for c in comments if c.get("id")}
        return TicketSnapshot(
            ticket_id=task.id,
            assignee=self._extract_assignee(task),
            status=task.status,
            ac_hash=ac_hash,
            comment_ids=comment_ids,
            is_closed=task.status == TaskStatus.DONE,
        )

    @staticmethod
    def _extract_assignee(task: Task) -> Optional[str]:
        """Pull the assignee from a task, or ``None`` if unassigned."""
        return getattr(task, "assigned_to", None) or (
            task.source_context.get("assignee") if task.source_context else None
        )

    @staticmethod
    def _extract_ac(task: Task) -> Optional[str]:
        """Extract the Marcus AC text from task description, or ``None``."""
        desc = task.description or ""
        ac = ACParser.extract(desc)
        return ac.raw_text if ac else None

    async def _emit(
        self,
        event_type: str,
        task: Task,
        extra: Dict[str, Any],
    ) -> None:
        """Publish an event to the Marcus event bus."""
        data: Dict[str, Any] = {
            "provider": self._provider,
            "task": {
                "id": task.id,
                "title": getattr(task, "name", getattr(task, "title", task.id)),
                "status": task.status.value,
                "description": task.description or "",
            },
        }
        data.update(extra)
        try:
            await self._events.publish(
                event_type=event_type,
                source="board_watcher",
                data=data,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to publish event %s: %s", event_type, exc)
