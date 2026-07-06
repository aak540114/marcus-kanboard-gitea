"""
Human-gated AI workflow orchestrator.

This module ties together the board watcher, ticket lifecycle manager,
acceptance criteria engine, git branch manager, comment protocol, and
dev environment manager into the end-to-end workflow described below.

Full lifecycle
--------------
1. ``BoardWatcher`` detects a new (or existing) ticket.
2. If no Marcus AC block exists, ``ACGenerator`` produces one and posts
   it as a comment.  The AC is also embedded in the ticket description.
3. The board watcher polls until a human **both** assigns the ticket to
   themselves **and** moves it to the ``ready`` kanban column.
4. On the ready trigger, a ``ticket/{provider}/{id}`` branch is created,
   the kanban column is set to ``in progress``, and the AI agent is
   notified via a Marcus comment on the ticket.
5. The AI agent works, posting periodic progress comments.
6. When the AI agent signals completion (or needs human input), the
   ticket moves to ``waiting for human`` and a matching comment is posted.
7. If the human responds, the AI re-reads the comments and continues on
   the same branch; the kanban column returns to ``in progress``.
8. When the human marks the ticket ``done``, the branch is merged to
   main, a "Merged" comment is posted, and the lifecycle state is ``DONE``.
9. If the human later reopens the ticket, the branch is rebased on main
   and work resumes from step 5.

Hot-reload preview
------------------
At any point a human can comment ``@marcus start-dev-env`` on the ticket
(or click a button in a future UI) to spin up a hot-reload dev
environment on the ticket branch.  The URL is posted back as a comment.

Status model
------------
The six kanban column names that Marcus understands are::

    todo  →  ready  →  in progress  ⇄  waiting for human
                            │
                        blocked (dependency)
                            │
                           done  →  (branch merged, REOPENED if reopened)

Classes
-------
HumanGatedWorkflow
    Central orchestrator.  Subscribe to the Marcus ``Events`` bus to
    receive board events, then call :meth:`handle_event` to route them.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from src.core.acceptance_criteria import ACChangeDetector, ACGenerator, ACParser
from src.core.board_watcher import BoardWatcher
from src.core.comment_protocol import CommentFormatter, CommentParser
from src.core.dev_environment import DevEnvironmentManager
from src.core.events import Events
from src.core.git_branch_manager import BranchManager
from src.core.models import TaskStatus
from src.core.ticket_lifecycle import (
    InvalidTransitionError,
    TicketLifecycleManager,
    TicketRecord,
    TicketState,
)
from src.integrations.kanban_interface import KanbanInterface

logger = logging.getLogger(__name__)


def _ticket_priority_key(record: TicketRecord) -> Tuple[int, int]:
    """Sort key for selecting the next ticket in dependency order.

    Tickets in ``READY`` state come before ``IN_PROGRESS`` (they haven't been
    touched yet).  Within each group, tickets with a lower numeric ID are
    assumed to have been created earlier and are more likely to be
    prerequisites for later work — so they get priority.
    """
    state_order = 0 if record.state == TicketState.READY else 1
    try:
        numeric_id = int(record.ticket_id)
    except ValueError:
        numeric_id = abs(hash(record.ticket_id))
    return (state_order, numeric_id)


class HumanGatedWorkflow:
    """Orchestrates the human-approval workflow for every ticket.

    Parameters
    ----------
    kanban : KanbanInterface
        Connected kanban provider.
    events : Events
        Shared Marcus event bus.
    provider_name : str
        Short label for the provider (``"github"``, ``"jira"``, etc.).
    lifecycle : Optional[TicketLifecycleManager]
        Lifecycle state store.  Created with defaults if not provided.
    branch_manager : Optional[BranchManager]
        Git branch manager.  Created with defaults if not provided.
    dev_env_manager : Optional[DevEnvironmentManager]
        Dev environment manager.  Created with defaults if not provided.
    ac_generator : Optional[ACGenerator]
        AC generator (may have an injected LLM callable).
    poll_interval : float
        Seconds between board polls for the ``BoardWatcher``.
    """

    def __init__(
        self,
        kanban: KanbanInterface,
        events: Events,
        provider_name: str,
        lifecycle: Optional[TicketLifecycleManager] = None,
        project_sync: Optional[Any] = None,
        branch_manager: Optional[BranchManager] = None,
        dev_env_manager: Optional[DevEnvironmentManager] = None,
        ac_generator: Optional[ACGenerator] = None,
        poll_interval: float = 30.0,
    ) -> None:
        """Initialise the workflow."""
        self._kanban = kanban
        self._events = events
        self._provider = provider_name
        self._lifecycle = lifecycle or TicketLifecycleManager()
        self._branch = branch_manager or BranchManager()
        self._dev_env = dev_env_manager or DevEnvironmentManager()
        self._ac_gen = ac_generator or ACGenerator()
        self._project_sync = project_sync  # Optional ProjectSyncWorkflow
        self._watcher = BoardWatcher(
            kanban=kanban,
            events=events,
            provider_name=provider_name,
            poll_interval=poll_interval,
            on_error=self._on_watcher_error,
        )
        self._subscribed = False
        # Unique identifier for this Marcus workflow instance — used as the
        # agent_id when claiming tickets to prevent duplicate AI work.
        self._agent_id = f"marcus-{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to events and start polling."""
        if not self._subscribed:
            self._subscribe_events()
            self._subscribed = True
        await self._watcher.start()
        logger.info("HumanGatedWorkflow started for provider=%s", self._provider)

    async def stop(self) -> None:
        """Stop polling and shut down all dev environments."""
        await self._watcher.stop()
        await self._dev_env.stop_all()
        logger.info("HumanGatedWorkflow stopped for provider=%s", self._provider)

    # ------------------------------------------------------------------
    # Event subscriptions
    # ------------------------------------------------------------------

    def _subscribe_events(self) -> None:
        """Wire board watcher events to handler methods."""
        self._events.subscribe("ticket.new", self._on_ticket_new)
        self._events.subscribe("ticket.assigned", self._on_ticket_assigned)
        self._events.subscribe("ticket.unassigned", self._on_ticket_unassigned)
        self._events.subscribe("ticket.status_changed", self._on_status_changed)
        self._events.subscribe("ticket.closed", self._on_ticket_closed)
        self._events.subscribe("ticket.reopened", self._on_ticket_reopened)
        self._events.subscribe("ticket.comment_added", self._on_comment_added)
        self._events.subscribe("ticket.ac_changed", self._on_ac_changed)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_ticket_new(self, event: Any) -> None:
        """Handle a ticket seen for the first time."""
        data = event.data
        ticket_id = data["ticket_id"]
        task = data.get("task", {})
        description = task.get("description", "")
        title = task.get("title", ticket_id)

        record = self._lifecycle.get_or_create(ticket_id, self._provider)

        # If there's no Marcus AC block yet, generate one.
        existing_ac = ACParser.extract(description)
        if existing_ac is None:
            await self._generate_and_post_ac(
                ticket_id=ticket_id,
                title=title,
                description=description,
                was_human_created=True,
                record=record,
            )
        else:
            # AC already present (AI-created ticket) — just store the hash.
            if not record.ac_hash:
                new_hash = ACChangeDetector.hash_ac(existing_ac.raw_text)
                self._lifecycle.update_acceptance_criteria(
                    ticket_id, self._provider, existing_ac.raw_text, new_hash
                )

    async def _on_ticket_assigned(self, event: Any) -> None:
        """Handle a ticket being assigned to a human.

        The human assigning themselves is the signal for AI to start work.
        If the ticket is already in a non-todo state (column has been moved
        past ``todo``), AI claims the ticket and begins immediately.
        """
        data = event.data
        ticket_id = data["ticket_id"]
        assignee = data.get("assignee", "unknown")

        record = self._lifecycle.get_or_create(ticket_id, self._provider)

        # Record the human assignee.
        try:
            self._lifecycle.set_assignee(ticket_id, self._provider, assignee)
        except KeyError:
            pass

        # Re-fetch so record reflects the stored assignee before the check.
        record = self._lifecycle.get(ticket_id, self._provider) or record

        # If the kanban column is already past todo, start AI work now.
        if record.state != TicketState.TODO:
            await self._start_ai_work(ticket_id, record)

    async def _on_ticket_unassigned(self, event: Any) -> None:
        """Handle a ticket being unassigned by a human.

        Without a human owner, AI has no one to report to — the claim is
        released and AI stops until a human re-assigns the ticket.
        """
        data = event.data
        ticket_id = data["ticket_id"]
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return

        # Clear the stored assignee.
        try:
            self._lifecycle.set_assignee(ticket_id, self._provider, "")
        except KeyError:
            pass

        # Release the AI claim — no human owner means AI should not work.
        try:
            self._lifecycle.release_ticket(ticket_id, self._provider)
        except KeyError:
            pass

    async def _on_status_changed(self, event: Any) -> None:
        """Handle a kanban status/column change.

        Triggers
        --------
        * ``ready`` or ``in_progress``, ticket has a human owner → AI starts.
        * ``in_progress`` while WAITING_FOR_HUMAN, has human owner → AI
          resumes (human moved card back after reviewing).
        * ``waiting_for_human`` moved by human → rejected with a warning
          (that state is AI-only; only AI may set it).
        * ``todo`` / ``blocked`` → update lifecycle state accordingly.
        * ``done`` is handled by the ``ticket.closed`` event; no action here.
        """
        data = event.data
        ticket_id = data["ticket_id"]
        new_status = data.get("new_status", "")

        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            record = self._lifecycle.get_or_create(ticket_id, self._provider)

        # Block human attempts to set the AI-only state.
        if new_status == TaskStatus.WAITING_FOR_HUMAN.value:
            logger.warning(
                "Ticket %s: human moved card to waiting_for_human; "
                "that state is reserved for AI — ignoring",
                ticket_id,
            )
            return

        if new_status in (TaskStatus.READY.value, TaskStatus.IN_PROGRESS.value):
            if not self._is_unassigned(record):
                # Ticket has a human owner → AI should work.
                if (
                    new_status == TaskStatus.IN_PROGRESS.value
                    and record.state == TicketState.WAITING_FOR_HUMAN
                ):
                    # Human moved card from waiting_for_human → in_progress.
                    # AI resumes work on the existing branch without re-claiming.
                    try:
                        self._lifecycle.transition(
                            ticket_id,
                            self._provider,
                            TicketState.IN_PROGRESS,
                            reason="Human moved ticket back to in_progress; AI resuming",
                        )
                    except InvalidTransitionError:
                        pass
                else:
                    # Status changed to a workable state with a human owner → start.
                    await self._start_ai_work(ticket_id, record)
            # else: no human owner → AI does not work on unassigned tickets.

        elif new_status == TaskStatus.TODO.value:
            # Human reset the ticket to todo.
            try:
                self._lifecycle.human_transition(
                    ticket_id,
                    self._provider,
                    TicketState.TODO,
                    reason="Human moved ticket to todo",
                )
            except (InvalidTransitionError, KeyError):
                pass

        elif new_status == TaskStatus.BLOCKED.value:
            # Human marked the ticket blocked.
            try:
                self._lifecycle.human_transition(
                    ticket_id,
                    self._provider,
                    TicketState.BLOCKED,
                    reason="Human marked ticket as blocked",
                )
            except (InvalidTransitionError, KeyError):
                pass

    async def _on_ticket_closed(self, event: Any) -> None:
        """Handle a ticket marked done — merge branch to main."""
        data = event.data
        ticket_id = data["ticket_id"]
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return

        if record.state not in (
            TicketState.IN_PROGRESS,
            TicketState.WAITING_FOR_HUMAN,
            TicketState.BLOCKED,
        ):
            return

        branch_name = record.branch_name
        main_branch = self._branch.config.main_branch

        merge_msg = (
            f"merge: ticket/{self._provider}/{ticket_id}"
            f" (accepted by {record.assignee})"
        )
        merged = await self._branch.merge_to_main(
            branch_name,
            commit_message=merge_msg,
        )

        if merged:
            try:
                self._lifecycle.transition(
                    ticket_id,
                    self._provider,
                    TicketState.DONE,
                    reason="Human marked done; branch merged to main",
                )
            except InvalidTransitionError as exc:
                logger.warning(
                    "Ticket %s: unexpected state when closing — forcing DONE: %s",
                    ticket_id,
                    exc,
                )
                # Force state to DONE via human_transition so the claim is
                # still released below even if _AI_TRANSITIONS blocks the path.
                try:
                    self._lifecycle.human_transition(
                        ticket_id,
                        self._provider,
                        TicketState.DONE,
                        reason="Forced DONE after merge (state machine override)",
                    )
                except (InvalidTransitionError, KeyError):
                    pass
            try:
                self._lifecycle.set_merged(ticket_id, self._provider)
            except KeyError:
                pass
            try:
                self._lifecycle.release_ticket(ticket_id, self._provider)
            except KeyError:
                pass

            # Stop dev env if running.
            await self._dev_env.stop(ticket_id, self._provider)

            comment = CommentFormatter.merged(
                ticket_id=ticket_id,
                branch_name=branch_name,
                main_branch=main_branch,
            )
            await self._post_comment(ticket_id, comment)
            logger.info("Ticket %s done and merged to %s", ticket_id, main_branch)

            # Agent is now free — pick up the next ticket in dependency order.
            await self._pickup_next_ticket()
        else:
            await self._post_error(
                ticket_id,
                f"Merge of `{branch_name}` to `{main_branch}` failed — "
                "there may be conflicts.  Please merge manually or rebase the branch.",
            )

    async def _on_ticket_reopened(self, event: Any) -> None:
        """Handle a ticket being reopened — rebase branch on main and resume."""
        data = event.data
        ticket_id = data["ticket_id"]
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return

        branch_name = record.branch_name

        rebased = await self._branch.rebase_on_main(branch_name)
        if not rebased:
            await self._post_error(
                ticket_id,
                f"Rebase of `{branch_name}` on `{self._branch.config.main_branch}` "
                "failed — please resolve conflicts manually.",
            )
            return

        # Clear any stale claim so AI can reclaim after reopen.
        try:
            self._lifecycle.release_ticket(ticket_id, self._provider)
        except KeyError:
            pass

        try:
            self._lifecycle.transition(
                ticket_id,
                self._provider,
                TicketState.REOPENED,
                reason="Human reopened ticket",
            )
            self._lifecycle.transition(
                ticket_id,
                self._provider,
                TicketState.IN_PROGRESS,
                reason="Branch rebased on main; AI resuming work",
            )
        except InvalidTransitionError as exc:
            logger.debug("State transition on reopen failed: %s", exc)

        # Move kanban column back to in progress.
        try:
            await self._kanban.move_task_to_column(ticket_id, "in progress")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not reset kanban column after reopen: %s", exc)

        logger.info(
            "Ticket %s reopened; branch %s rebased on main", ticket_id, branch_name
        )

        # Agent is now free — pick up the next ticket (or re-claim this one).
        await self._pickup_next_ticket()

    async def _on_comment_added(self, event: Any) -> None:
        """Handle a new human comment on a ticket."""
        data = event.data
        ticket_id = data["ticket_id"]
        body = data.get("comment_body", "")
        author = data.get("comment_author", "")

        # Ignore Marcus's own comments.
        if CommentParser.is_marcus_comment(body):
            return

        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None or record.state == TicketState.TODO:
            return

        # Check for @marcus commands.
        if CommentParser.contains_command(body, "start-dev-env"):
            await self._handle_start_dev_env_command(ticket_id, record)
            return

        # If AI is waiting for human, treat any comment as a continuation
        # signal: acknowledge the input and transition back to IN_PROGRESS.
        if record.state == TicketState.WAITING_FOR_HUMAN:
            try:
                self._lifecycle.transition(
                    ticket_id,
                    self._provider,
                    TicketState.IN_PROGRESS,
                    reason=f"Human {author!r} provided input; AI continuing",
                )
            except InvalidTransitionError:
                pass

            # Move kanban column back to in progress.
            try:
                await self._kanban.move_task_to_column(ticket_id, "in progress")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not update kanban column on comment: %s", exc)

            comment = CommentFormatter.revision_requested(
                ticket_id=ticket_id,
                human_comment=body,
                ai_understanding=(
                    "Thanks for the input.  I'll apply the requested changes "
                    "and post an update when complete."
                ),
            )
            await self._post_comment(ticket_id, comment)

        elif record.state == TicketState.IN_PROGRESS:
            # Human commenting while AI is already working — log it.
            logger.debug(
                "Human comment on %s while AI in progress: %s", ticket_id, body[:100]
            )

    async def _on_ac_changed(self, event: Any) -> None:
        """Handle human edits to the acceptance criteria."""
        data = event.data
        ticket_id = data["ticket_id"]
        new_ac = data.get("new_ac_text", "")
        new_hash = data.get("new_hash", "")

        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return

        self._lifecycle.update_acceptance_criteria(
            ticket_id, self._provider, new_ac, new_hash
        )

        if record.state in (TicketState.IN_PROGRESS, TicketState.WAITING_FOR_HUMAN):
            try:
                self._lifecycle.transition(
                    ticket_id,
                    self._provider,
                    TicketState.IN_PROGRESS
                    if record.state == TicketState.WAITING_FOR_HUMAN
                    else TicketState.WAITING_FOR_HUMAN,
                    reason="Acceptance criteria edited by human",
                )
            except InvalidTransitionError:
                pass

            comment = CommentFormatter.revision_requested(
                ticket_id=ticket_id,
                human_comment="*(acceptance criteria edited in ticket description)*",
                ai_understanding=(
                    "The acceptance criteria have been updated.  I'll re-read "
                    "them now and adjust the implementation accordingly."
                ),
            )
            await self._post_comment(ticket_id, comment)
            logger.info("AC change detected on ticket %s — notified agent", ticket_id)

    # ------------------------------------------------------------------
    # Agent-facing helpers (called by MCP tools)
    # ------------------------------------------------------------------

    async def report_progress(
        self,
        ticket_id: str,
        percentage: int,
        message: str,
    ) -> bool:
        """Post a progress comment on behalf of the AI agent.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        percentage : int
            Completion percentage (0–100).
        message : str
            Progress description.

        Returns
        -------
        bool
            ``True`` if the comment was posted successfully.
        """
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return False

        commits = await self._branch.get_branch_commits(record.branch_name)
        comment = CommentFormatter.progress(
            ticket_id=ticket_id,
            branch_name=record.branch_name,
            percentage=percentage,
            message=message,
            commits=commits,
        )
        return await self._post_comment(ticket_id, comment)

    async def signal_ready_for_review(self, ticket_id: str) -> bool:
        """Signal that the AI agent is done and awaiting human acceptance.

        Transitions the ticket to ``WAITING_FOR_HUMAN``, moves the kanban
        column to ``waiting for human``, and posts a "Ready for Review"
        comment asking the human to review the work and mark the ticket
        ``done`` when satisfied.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.

        Returns
        -------
        bool
            ``True`` on success.
        """
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return False

        try:
            self._lifecycle.transition(
                ticket_id,
                self._provider,
                TicketState.WAITING_FOR_HUMAN,
                reason="AI agent signalled implementation complete",
            )
        except InvalidTransitionError as exc:
            logger.error(
                "Cannot move %s to WAITING_FOR_HUMAN: %s", ticket_id, exc
            )
            return False

        # Move the kanban card to "waiting for human".
        try:
            await self._kanban.move_task_to_column(ticket_id, "waiting for human")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not update kanban column: %s", exc)

        dev_info = self._dev_env.get_info(ticket_id, self._provider)
        dev_url = dev_info.url if dev_info else None

        commits = await self._branch.get_branch_commits(record.branch_name)
        ac_items = self._get_ac_items(record)
        comment = CommentFormatter.ready_for_review(
            ticket_id=ticket_id,
            branch_name=record.branch_name,
            ac_items=ac_items,
            dev_env_url=dev_url,
            commit_count=len(commits),
        )
        posted = await self._post_comment(ticket_id, comment)

        # Release the claim so this agent is free for other work, then pick
        # up the next available ticket in dependency order.
        try:
            self._lifecycle.release_ticket(ticket_id, self._provider)
        except KeyError:
            pass
        await self._pickup_next_ticket()

        return posted

    async def set_waiting_for_human(
        self,
        ticket_id: str,
        reason: str = "AI agent requires human input to continue.",
    ) -> bool:
        """Signal that the AI needs external human input.

        Transitions to ``WAITING_FOR_HUMAN`` and moves the kanban card to
        the ``waiting for human`` column.  Use this when the AI is blocked
        by something outside its control (e.g. missing credentials, unclear
        requirement) rather than having finished the work.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        reason : str
            Human-readable explanation of what input is needed.

        Returns
        -------
        bool
            ``True`` on success.
        """
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return False

        if record.state != TicketState.IN_PROGRESS:
            return False

        try:
            self._lifecycle.transition(
                ticket_id,
                self._provider,
                TicketState.WAITING_FOR_HUMAN,
                reason=f"AI waiting for human: {reason}",
            )
        except InvalidTransitionError as exc:
            logger.error("Cannot set %s to WAITING_FOR_HUMAN: %s", ticket_id, exc)
            return False

        try:
            await self._kanban.move_task_to_column(ticket_id, "waiting for human")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not update kanban column: %s", exc)

        comment = CommentFormatter.revision_requested(
            ticket_id=ticket_id,
            human_comment="",
            ai_understanding=reason,
        )
        posted = await self._post_comment(ticket_id, comment)

        # Release claim so agent can work on the next available ticket.
        try:
            self._lifecycle.release_ticket(ticket_id, self._provider)
        except KeyError:
            pass
        await self._pickup_next_ticket()

        return posted

    async def set_blocked(
        self,
        ticket_id: str,
        blocked_by: str,
    ) -> bool:
        """Mark the ticket as blocked by an unresolved dependency.

        Transitions to ``BLOCKED`` and moves the kanban card to the
        ``blocked`` column.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        blocked_by : str
            Description of the blocking dependency (e.g. ticket ID or
            resource name).

        Returns
        -------
        bool
            ``True`` on success.
        """
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return False

        if record.state != TicketState.IN_PROGRESS:
            return False

        try:
            self._lifecycle.transition(
                ticket_id,
                self._provider,
                TicketState.BLOCKED,
                reason=f"Blocked by: {blocked_by}",
            )
        except InvalidTransitionError as exc:
            logger.error("Cannot set %s to BLOCKED: %s", ticket_id, exc)
            return False

        try:
            await self._kanban.move_task_to_column(ticket_id, "blocked")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not update kanban column: %s", exc)

        # Release claim so this agent can pick up the next available ticket.
        try:
            self._lifecycle.release_ticket(ticket_id, self._provider)
        except KeyError:
            pass
        await self._pickup_next_ticket()

        return True

    async def start_dev_environment(self, ticket_id: str) -> Optional[str]:
        """Spin up the hot-reload dev environment for a ticket branch.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.

        Returns
        -------
        Optional[str]
            URL of the running environment, or ``None`` on failure.
        """
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return None

        try:
            info = await self._dev_env.start(
                ticket_id=ticket_id,
                provider=self._provider,
                branch_name=record.branch_name,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to start dev env for %s: %s", ticket_id, exc)
            return None

        # Store the port in lifecycle record.
        self._lifecycle.set_dev_env_port(ticket_id, self._provider, info.port)

        # Post a comment with the URL.
        comment = CommentFormatter.dev_env_started(
            ticket_id=ticket_id,
            branch_name=record.branch_name,
            url=info.url,
            port=info.port,
        )
        await self._post_comment(ticket_id, comment)
        return info.url

    async def get_work_context(
        self,
        ticket_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return everything an AI agent needs to start working on a ticket.

        This is the single entry-point for any new AI agent connecting to
        the Marcus–Kanboard–GitLab system.  A single call gives the agent:

        - Ticket title and description (from Kanboard)
        - Acceptance criteria checklist (from Marcus lifecycle store)
        - Git branch name to check out
        - Local repository path on disk
        - GitLab remote URL
        - Current lifecycle state
        - MCP server URL for reporting back

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID.

        Returns
        -------
        Optional[Dict[str, Any]]
            Context dict, or ``None`` if the ticket is not tracked.
        """
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return None

        # Fetch live ticket details from Kanboard.
        title = ticket_id
        description = ""
        kanboard_project_id: Optional[int] = None
        try:
            task = await self._kanban.get_task_by_id(ticket_id)
            if task:
                title = task.name
                description = task.description
                src_ctx = task.source_context or {}
                raw = src_ctx.get("kanboard_task", {})
                project_id_raw = raw.get("project_id")
                if project_id_raw:
                    kanboard_project_id = int(project_id_raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch task %s from kanban: %s", ticket_id, exc)

        # Repo info from ProjectSyncWorkflow (if wired up).
        local_repo_path: Optional[str] = None
        gitlab_repo_url: Optional[str] = None
        if self._project_sync and kanboard_project_id is not None:
            mapping = self._project_sync.get_repo_for_project(kanboard_project_id)
            if mapping:
                local_repo_path = mapping.get("local_repo_path")
                gitlab_repo_url = mapping.get("gitlab_repo_url")

        return {
            "ticket_id": ticket_id,
            "provider": self._provider,
            "title": title,
            "description": description,
            "acceptance_criteria": record.acceptance_criteria or "",
            "branch_name": record.branch_name,
            "local_repo_path": local_repo_path,
            "gitlab_repo_url": gitlab_repo_url,
            "state": record.state.value,
            "assignee": record.assignee,
            "already_claimed_by": record.ai_agent_id,
            "mcp_server_url": "http://localhost:4298/mcp",
            "instructions": (
                "1. cd into local_repo_path\n"
                "2. git checkout branch_name\n"
                "3. Read the description and acceptance_criteria\n"
                "4. Implement the work; commit and push to the branch\n"
                "5. Call signal_ready_for_review when done, "
                "or signal_waiting_for_human / signal_blocked if stuck"
            ),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _start_ai_work(
        self,
        ticket_id: str,
        record: TicketRecord,
    ) -> None:
        """Claim the ticket, create branch, set in-progress, and notify AI.

        Called whenever both conditions are met: the ticket is unassigned
        (``owner_id == 0`` on Kanboard) **and** the lifecycle state is
        ``READY`` or ``IN_PROGRESS`` (or the kanban column just changed
        to one of those states).

        The claim gate ensures at most one Marcus instance starts work on
        the same ticket concurrently.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        record : TicketRecord
            Current lifecycle record.
        """
        # One-ticket-per-agent: refuse if this agent is already working on
        # a different ticket.
        current_ticket = self._lifecycle.get_agent_ticket(self._agent_id)
        if current_ticket is not None and current_ticket != ticket_id:
            logger.info(
                "Agent %s already working on ticket %s; skipping %s",
                self._agent_id,
                current_ticket,
                ticket_id,
            )
            return

        # Atomically claim the ticket; abort if another agent already has it.
        claimed = self._lifecycle.claim_ticket(
            ticket_id, self._provider, self._agent_id
        )
        if not claimed:
            current = self._lifecycle.get(ticket_id, self._provider)
            logger.info(
                "Ticket %s already claimed by %s; skipping",
                ticket_id,
                current.ai_agent_id if current else "unknown",
            )
            return

        # Advance the lifecycle state to IN_PROGRESS via READY if needed.
        if record.state == TicketState.TODO:
            try:
                self._lifecycle.transition(
                    ticket_id,
                    self._provider,
                    TicketState.READY,
                    reason="AI agent starting: ticket unassigned and workable",
                )
            except InvalidTransitionError as exc:
                logger.debug("Cannot transition to READY: %s", exc)
                self._lifecycle.release_ticket(ticket_id, self._provider)
                return

        if record.state in (TicketState.TODO, TicketState.READY):
            try:
                self._lifecycle.transition(
                    ticket_id,
                    self._provider,
                    TicketState.IN_PROGRESS,
                    reason="Branch created; AI agent beginning work",
                )
            except InvalidTransitionError as exc:
                logger.error("Cannot transition to IN_PROGRESS: %s", exc)
                self._lifecycle.release_ticket(ticket_id, self._provider)
                return

        # Re-fetch after transitions so branch_name is current.
        record = self._lifecycle.get(ticket_id, self._provider) or record

        # Create the ticket branch.
        branch_name = record.branch_name or BranchManager.make_branch_name(
            self._provider, ticket_id
        )
        created = await self._branch.create_branch(branch_name)
        if not created:
            logger.error(
                "Failed to create branch %s for ticket %s", branch_name, ticket_id
            )
            await self._post_error(
                ticket_id,
                f"Failed to create git branch `{branch_name}`. "
                "Please check repository permissions.",
            )
            self._lifecycle.release_ticket(ticket_id, self._provider)
            return

        # Move kanban card to "in progress".
        try:
            await self._kanban.move_task_to_column(ticket_id, "in progress")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not update kanban column to in_progress: %s", exc)

        # Post "started" comment.
        ac_items = self._get_ac_items(record)
        comment = CommentFormatter.started(
            ticket_id=ticket_id,
            branch_name=branch_name,
            assignee=record.assignee or "AI agent",
            ac_items=ac_items,
        )
        await self._post_comment(ticket_id, comment)
        logger.info("AI work started for ticket %s (branch %s)", ticket_id, branch_name)

    async def _generate_and_post_ac(
        self,
        ticket_id: str,
        title: str,
        description: str,
        was_human_created: bool,
        record: TicketRecord,
    ) -> None:
        """Generate AC via LLM/heuristic and post it on the ticket."""
        ac_markdown = await self._ac_gen.generate(
            title=title,
            description=description,
        )
        comment = CommentFormatter.ac_generated(
            ticket_id=ticket_id,
            ac_markdown=ac_markdown,
            was_human_created=was_human_created,
        )
        await self._post_comment(ticket_id, comment)

        # Embed the AC block in the ticket description.
        new_desc = ACParser.embed(description, ac_markdown)
        try:
            await self._kanban.update_task(ticket_id, {"description": new_desc})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not embed AC in ticket description: %s", exc)

        # Store hash in lifecycle record.
        import hashlib

        new_hash = hashlib.sha256(ac_markdown.encode()).hexdigest()
        self._lifecycle.update_acceptance_criteria(
            ticket_id, self._provider, ac_markdown, new_hash
        )

    async def _handle_start_dev_env_command(
        self, ticket_id: str, record: TicketRecord
    ) -> None:
        """Handle the ``@marcus start-dev-env`` comment command."""
        url = await self.start_dev_environment(ticket_id)
        if url is None:
            await self._post_error(
                ticket_id,
                "Failed to start dev environment.  "
                "Check that Docker is running and the repository is accessible.",
            )

    def _get_ac_items(self, record: TicketRecord) -> List[str]:
        """Return the list of AC item texts from the stored AC markdown."""
        if not record.acceptance_criteria:
            return []
        ac = ACParser.extract(
            f"<!-- MARCUS_AC_START -->\n## Acceptance Criteria\n\n"
            f"{record.acceptance_criteria}\n<!-- MARCUS_AC_END -->"
        )
        if ac is None:
            # The stored text might not have sentinels — try parsing directly.
            import re

            items = re.findall(
                r"^- \[[ xX]\] (.+)$", record.acceptance_criteria, re.MULTILINE
            )
            return items
        return [item.text for item in ac.items]

    async def _pickup_next_ticket(self) -> None:
        """After releasing the current ticket, start the next available one.

        Called whenever this agent's ticket moves to ``WAITING_FOR_HUMAN``,
        ``BLOCKED``, or ``DONE`` so the agent does not sit idle while other
        work is ready.

        Selection order (dependency approximation):

        1. ``READY`` tickets before ``IN_PROGRESS`` ones.
        2. Lower numeric ticket ID first (earlier-created tickets are more
           likely to be prerequisites for later work).
        """
        candidates = self._lifecycle.get_available_tickets()
        if not candidates:
            logger.debug("Agent %s has no next ticket to pick up", self._agent_id)
            return

        candidates.sort(key=_ticket_priority_key)
        next_rec = candidates[0]
        logger.info(
            "Agent %s picking up next ticket: %s (state=%s)",
            self._agent_id,
            next_rec.ticket_id,
            next_rec.state.value,
        )
        await self._start_ai_work(next_rec.ticket_id, next_rec)

    def _is_unassigned(self, record: TicketRecord) -> bool:
        """Return ``True`` if no human is assigned to *record*.

        Treats ``None``, empty string, and ``"0"`` (Kanboard's ``owner_id``
        sentinel for "no owner") as unassigned.  AI only works when this
        returns ``False`` — i.e., when a human has taken ownership.

        Parameters
        ----------
        record : TicketRecord
            Lifecycle record to check.

        Returns
        -------
        bool
            ``True`` if the ticket has no human assignee.
        """
        return record.assignee in (None, "", "0")

    async def _post_comment(self, ticket_id: str, body: str) -> bool:
        """Post a comment via the kanban provider (best-effort)."""
        try:
            result = await self._kanban.add_comment(ticket_id, body)
            return bool(result)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to post comment on %s: %s", ticket_id, exc)
            return False

    async def _post_error(self, ticket_id: str, error_summary: str) -> None:
        """Post an error comment on a ticket."""
        comment = CommentFormatter.error(
            ticket_id=ticket_id, error_summary=error_summary
        )
        await self._post_comment(ticket_id, comment)

    async def _on_watcher_error(self, exc: Exception) -> None:
        """Handle a poll cycle failure reported by the BoardWatcher."""
        logger.error("Board watcher error in HumanGatedWorkflow: %s", exc)
