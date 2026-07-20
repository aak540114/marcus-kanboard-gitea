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
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple, cast

from src.ai.verification.ai_verifier import AIVerifier, VerificationResult
from src.core.acceptance_criteria import ACChangeDetector, ACGenerator, ACParser
from src.core.board_watcher import BoardWatcher
from src.core.comment_protocol import CommentFormatter, CommentParser
from src.core.dev_environment import DevEnvironmentManager
from src.core.events import Events
from src.core.gate_settings import GateMode, GateSettingManager
from src.core.git_branch_manager import BranchManager, BranchManagerConfig
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
    max_parallel_agents : int
        How many tickets this workflow may keep *in progress* at once — the
        human-set "how many agents work in parallel" ceiling. Each
        concurrently in-progress ticket is held by a distinct AI *slot*;
        the first slot's id is :attr:`_agent_id` (kept for the single-agent
        callers). A slot frees only when its ticket naturally releases
        (waiting-for-human / blocked / done), so a busy slot is never
        preempted and in-flight work is never lost. Values below 1 are
        clamped to 1. Defaults to 1 (classic one-ticket-at-a-time behavior).
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
        gate_settings: Optional[GateSettingManager] = None,
        ai_verifier: Optional[AIVerifier] = None,
        desc_inferrer: Optional[Any] = None,
        llm_generate: Optional[Any] = None,
        max_parallel_agents: int = 1,
        poll_interval: float = 30.0,
    ) -> None:
        """Initialise the workflow."""
        self._kanban = kanban
        self._events = events
        self._provider = provider_name
        self._lifecycle = lifecycle or TicketLifecycleManager()
        self._branch = branch_manager or BranchManager()
        # Per-project BranchManagers keyed by local repo path — see
        # _branch_for_ticket. self._branch is only the fallback for
        # deployments with no project sync (and for tests that inject a
        # mock branch manager directly).
        self._branch_managers: Dict[str, BranchManager] = {}
        self._dev_env = dev_env_manager or DevEnvironmentManager()
        self._ac_gen = ac_generator or ACGenerator()
        self._gate = gate_settings or GateSettingManager()
        self._verifier = ai_verifier or AIVerifier()
        # Optional ProjectDescriptionInferrer — when set, a ticket whose
        # project has no usable tech stack gets one inferred from the ticket
        # instead of immediately pausing on the human (see
        # _infer_project_description).
        self._desc_inferrer = desc_inferrer
        # Optional async ``(prompt) -> str`` used to summarize a worker's raw
        # progress report into a one-line ticket comment (orchestrate mode).
        self._llm_generate = llm_generate
        self._project_sync = project_sync  # Optional ProjectSyncWorkflow
        self._watcher = BoardWatcher(
            kanban=kanban,
            events=events,
            provider_name=provider_name,
            poll_interval=poll_interval,
            on_error=self._on_watcher_error,
        )
        self._subscribed = False
        # How many tickets may be in progress at once (parallel-agent cap).
        self._max_parallel_agents = max(1, int(max_parallel_agents))
        # Unique identifier for this Marcus workflow instance. This is slot
        # 0's claim id; additional parallel slots derive from it (see
        # _slot_id). Kept as _agent_id for the single-agent callers/tests
        # that reference it directly.
        self._agent_id = f"marcus-{uuid.uuid4().hex[:8]}"
        # Tracks how many verification rounds have been completed per ticket.
        # Lost on Marcus restart, which is acceptable since verify cycles are
        # short-lived (minutes) and the round counter resets naturally.
        self._ticket_verify_rounds: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to events and start polling."""
        if not self._subscribed:
            self._subscribe_events()
            self._subscribed = True
        # Persisted claims are ghosts after a restart: this instance's
        # agent id is a fresh UUID, so no event could ever release a claim
        # held under the previous process's id — the ticket would sit
        # "in progress" on the board forever (first-sight recovery
        # deliberately skips claimed records). Release them all before the
        # watcher's first poll so recovery can re-claim and resume work.
        stale = self._lifecycle.release_stale_claims()
        if stale:
            logger.info(
                "Released %d stale AI claim(s) from a previous run: %s",
                len(stale),
                ", ".join(stale),
            )
        # Same restart hygiene for dev-env containers: the registry is
        # in-memory, so containers from a previous run are unreachable
        # orphans (held ports, docker name collisions on restart).
        reconcile = getattr(self._dev_env, "reconcile_orphans", None)
        if reconcile is not None:
            try:
                await reconcile()
            except Exception as exc:  # noqa: BLE001 - never block startup
                logger.warning("Dev-env orphan reconciliation failed: %s", exc)
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

        # First-sight recovery: BoardWatcher emits ONLY ticket.new for a
        # ticket it has never seen — including one that was assigned and
        # moved to Ready while Marcus was down (its assignment and column
        # state get absorbed into the watcher's baseline snapshot, so no
        # ticket.assigned / ticket.status_changed diff ever fires later).
        # Reconcile against the board state carried in the event itself:
        # already assigned + already in a workable column → start now.
        # The Kanboard task.create webhook payload has neither a "status"
        # nor an "assignee" key (raw Kanboard fields instead), so this
        # never triggers for genuinely fresh webhook tickets — they are in
        # the first column at creation anyway.
        board_status = task.get("status") or ""
        board_assignee = task.get("assignee") or ""
        if board_assignee and board_assignee != "0" and board_status in (
            TaskStatus.READY.value,
            TaskStatus.IN_PROGRESS.value,
        ):
            try:
                self._lifecycle.set_assignee(
                    ticket_id, self._provider, board_assignee
                )
            except KeyError:
                pass
            record = self._lifecycle.get(ticket_id, self._provider) or record
            if record.ai_agent_id is None:
                logger.info(
                    "Ticket %s first seen already assigned (%s) and %s — "
                    "starting AI work (restart recovery)",
                    ticket_id,
                    board_assignee,
                    board_status,
                )
                await self._start_ai_work(ticket_id, record)

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

        # Unassigning freed a slot; fill it with any waiting assigned work
        # so parallel capacity is not left idle until an unrelated event.
        await self._pickup_next_ticket()

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
        * ``done`` → drive :meth:`_on_ticket_closed` (merge to main). A
          Done-column move is a column move, not a Kanboard task-close, so
          the merge must be triggered here too.
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
                    # AI resumes work on the existing branch. Re-acquire the
                    # claim (released at review-signal time): an unclaimed
                    # IN_PROGRESS record would otherwise be "started" again
                    # by BoardWatcher's poll echo of this same column move
                    # — a duplicate claim plus a contradictory "Started"
                    # comment right after this resume.
                    try:
                        self._lifecycle.transition(
                            ticket_id,
                            self._provider,
                            TicketState.IN_PROGRESS,
                            reason="Human moved ticket back to in_progress; AI resuming",
                        )
                    except InvalidTransitionError:
                        pass
                    self._reclaim_for_resume(ticket_id)
                elif (
                    record.state == TicketState.IN_PROGRESS
                    and record.ai_agent_id is not None
                ):
                    # Work already in flight (e.g. the poll-path echo of a
                    # webhook-handled column move — BoardWatcher snapshots
                    # only update during polls, so every webhook-signalled
                    # change re-fires once on the next poll). Nothing to do.
                    logger.debug(
                        "Ticket %s already claimed and in progress — "
                        "ignoring redundant status event",
                        ticket_id,
                    )
                else:
                    # Status changed to a workable state with a human owner → start.
                    await self._start_ai_work(ticket_id, record)
            else:
                # No human owner → AI does not start work on unassigned
                # tickets — but the lifecycle record must still mirror the
                # board. _on_ticket_assigned gates its "start now" decision
                # on ``record.state != TODO``, so without this sync the
                # "move to Ready first, assign second" ordering never
                # starts AI work: the record silently stays TODO while the
                # board shows Ready, and the later assignment is ignored.
                if record.state == TicketState.TODO:
                    try:
                        self._lifecycle.human_transition(
                            ticket_id,
                            self._provider,
                            TicketState.READY,
                            reason=(
                                "Human moved unassigned ticket to a workable "
                                "column; AI waits for assignment"
                            ),
                        )
                    except (InvalidTransitionError, KeyError):
                        pass

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
            # Release any AI claim: a todo reset means "stop working on
            # this". Without this, the claim stayed held and the
            # one-ticket-per-agent gate then skipped EVERY future ticket
            # ("already working on ticket X") until this specific ticket
            # was unassigned — a full workflow deadlock.
            try:
                self._lifecycle.release_ticket(ticket_id, self._provider)
            except KeyError:
                pass
            # The todo reset freed a slot; fill it with waiting assigned work.
            await self._pickup_next_ticket()

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

        elif new_status == TaskStatus.DONE.value:
            # Human dragged the card to the Done column. Kanboard fires this
            # as a column move (task.move.column), NOT task.close — so the
            # ticket.closed handler that merges the branch never runs on its
            # own (moving to the last column does not close a Kanboard task
            # by default). Drive that handler here so "drag to Done" actually
            # merges. Idempotent: _on_ticket_closed's own state guard skips an
            # already-DONE record, so the poll echo of this same move is a
            # no-op.
            await self._on_ticket_closed(event)

    async def _on_ticket_closed(self, event: Any) -> None:
        """Handle a ticket marked done — merge branch to main."""
        data = event.data
        ticket_id = data["ticket_id"]
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return

        # Human closed a ticket that AI never actually started (no branch to
        # merge). Under the parallel-agent cap an assigned ticket can sit in
        # READY (or TODO) waiting for a free slot; if the human then drags it
        # to Done, it must be marked DONE and released — otherwise it stays
        # READY+assigned+unclaimed, i.e. still "available", and the next slot
        # to free re-picks it, dragging the card back out of Done and posting
        # a "Started" comment (AI resurrects a ticket the human closed).
        if record.state in (TicketState.READY, TicketState.TODO):
            try:
                self._lifecycle.human_transition(
                    ticket_id,
                    self._provider,
                    TicketState.DONE,
                    reason="Human closed ticket before AI work began",
                )
            except (InvalidTransitionError, KeyError):
                pass
            try:
                self._lifecycle.release_ticket(ticket_id, self._provider)
            except KeyError:
                pass
            logger.info(
                "Ticket %s closed by human before any AI work — marked DONE",
                ticket_id,
            )
            await self._resume_tickets_blocked_by(ticket_id)
            await self._pickup_next_ticket()
            return

        if record.state not in (
            TicketState.IN_PROGRESS,
            TicketState.WAITING_FOR_HUMAN,
            TicketState.BLOCKED,
        ):
            return

        await self._merge_ticket_to_main(ticket_id, record)

    async def _merge_ticket_to_main(
        self, ticket_id: str, record: TicketRecord
    ) -> bool:
        """Merge a ticket's branch to main and complete it.

        Shared by the "human dragged the card to Done" path
        (:meth:`_on_ticket_closed`) and the "``@marcus approve`` comment"
        path (:meth:`_on_comment_added`). On success: transitions the record
        to DONE, releases the claim, stops the dev env, posts a "Merged"
        comment, unblocks dependents, and picks up the next ticket. On merge
        failure: posts an error, parks the ticket in WAITING_FOR_HUMAN, and
        frees the slot.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        record : TicketRecord
            Current lifecycle record (for branch name / assignee).

        Returns
        -------
        bool
            ``True`` if the branch merged and the ticket completed.
        """
        branch_name = record.branch_name
        branch_mgr = await self._branch_for_ticket(ticket_id)
        main_branch = branch_mgr.config.main_branch

        merge_msg = (
            f"merge: ticket/{self._provider}/{ticket_id}"
            f" (accepted by {record.assignee})"
        )
        merged = await branch_mgr.merge_to_main(
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

            # This completion may unblock other tickets.
            await self._resume_tickets_blocked_by(ticket_id)

            # Agent is now free — pick up the next ticket in dependency order.
            await self._pickup_next_ticket()
            return True
        else:
            await self._post_error(
                ticket_id,
                f"Merge of `{branch_name}` to `{main_branch}` failed — "
                "there may be conflicts.  Please merge manually or rebase the branch.",
            )
            # Park the ticket in WAITING_FOR_HUMAN and free the slot. The old
            # behavior left it IN_PROGRESS and *claimed* — permanently leaking
            # one parallel slot (a full deadlock at cap=1), since no later
            # event ever released it. Parking removes it from the available
            # pool (no re-merge loop) and lets a human resolve the conflict.
            self._park_in_waiting_for_human(
                ticket_id,
                reason="Merge to main failed; awaiting human conflict resolution",
            )
            try:
                await self._kanban.move_task_to_column(
                    ticket_id, "waiting for human"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not move %s to waiting-for-human after merge fail: %s",
                    ticket_id,
                    exc,
                )
            await self._pickup_next_ticket()
            return False

    async def _on_ticket_reopened(self, event: Any) -> None:
        """Handle a ticket being reopened — rebase branch on main and resume."""
        data = event.data
        ticket_id = data["ticket_id"]
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return

        branch_name = record.branch_name

        branch_mgr = await self._branch_for_ticket(ticket_id)
        rebased = await branch_mgr.rebase_on_main(branch_name)
        if not rebased:
            await self._post_error(
                ticket_id,
                f"Rebase of `{branch_name}` on `{branch_mgr.config.main_branch}` "
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

        # Approval by comment: on a ticket awaiting review, "@marcus approve"
        # (or a plain "approve" / "lgtm" / "merge to main") means the same as
        # dragging the card to Done — merge the branch to main. This must be
        # checked BEFORE the generic "any comment = please make changes" path
        # below, or an approval would be misread as a revision request.
        if (
            record.state == TicketState.WAITING_FOR_HUMAN
            and self._is_approval_comment(body)
        ):
            logger.info(
                "Approval comment on ticket %s by %s — merging to main",
                ticket_id,
                author,
            )
            merged = await self._merge_ticket_to_main(ticket_id, record)
            if merged:
                try:
                    await self._kanban.move_task_to_column(ticket_id, "done")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Could not move %s to done after approval: %s",
                        ticket_id,
                        exc,
                    )
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
            # Re-acquire the claim released at review-signal time — same
            # reasoning as the column-move resume in _on_status_changed.
            self._reclaim_for_resume(ticket_id)

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
            # IN_PROGRESS stays IN_PROGRESS: the notification below says the
            # AI will "re-read and adjust", which is exactly what happens —
            # the agent keeps working against the updated AC. (An earlier
            # version flipped IN_PROGRESS → WAITING_FOR_HUMAN here, which
            # contradicted both the comment and the untouched board column,
            # and bricked completion: signal_ready_for_review cannot legally
            # transition WFH → WFH, so it returned False forever.)
            # WAITING_FOR_HUMAN resumes to IN_PROGRESS with the claim
            # re-acquired — the AC edit is the human's review feedback.
            if record.state == TicketState.WAITING_FOR_HUMAN:
                try:
                    self._lifecycle.transition(
                        ticket_id,
                        self._provider,
                        TicketState.IN_PROGRESS,
                        reason="Acceptance criteria edited by human",
                    )
                except InvalidTransitionError:
                    pass
                self._reclaim_for_resume(ticket_id)

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
    # Orchestrate mode — Marcus drives a "dumb worker" agent
    # ------------------------------------------------------------------

    @staticmethod
    def _worker_instructions() -> str:
        """The step-by-step directive Marcus hands a worker each turn."""
        return (
            "You are a worker; Marcus is the manager. Do EXACTLY this:\n"
            "1. `git clone` the `context.clone_url` into a new directory and "
            "cd into it; `git checkout -B <context.branch_name> "
            "origin/<context.branch_name>`.\n"
            "2. Implement every item in `context.acceptance_criteria` — make "
            "real code changes, commit, and push to that branch.\n"
            "3. Every ~10 seconds, call `marcus_work` again with the SAME "
            "`agent_id` and `ticket_id` and a `report` of the one thing you "
            "just did (a short sentence).\n"
            "4. When EVERY acceptance criterion is met, call `marcus_work` "
            "with `report=\"DONE - <one-line summary>\"`. If you hit something "
            "only a human can resolve, call with `report=\"BLOCKED - "
            "<reason>\"`."
        )

    def _classify_report_intent(self, report: str) -> str:
        """Classify a worker report by its leading keyword.

        Marcus instructs the worker to prefix a finishing report with
        ``DONE`` and a blocker with ``BLOCKED`` (see
        :meth:`_worker_instructions`), so this simple, reliable prefix check
        replaces brittle free-text intent detection.
        """
        head = report.strip().lower()
        if head.startswith("done"):
            return "done"
        if head.startswith("blocked"):
            return "blocked"
        if head.startswith("waiting") or head.startswith("need human"):
            return "waiting"
        return "progress"

    async def _summarize_report(self, report: str) -> str:
        """Summarize a worker's raw report into one line for a ticket comment."""
        text = report.strip()
        if self._llm_generate is None:
            return text[:280]
        prompt = (
            "Summarize this AI worker's progress update into ONE short, plain "
            "sentence for a ticket comment. No preamble, no markdown.\n\n"
            f"Update:\n{text}"
        )
        try:
            out = (await self._llm_generate(prompt) or "").strip()
            return out[:400] if out else text[:280]
        except Exception as exc:  # noqa: BLE001
            logger.debug("Report summarization failed: %s", exc)
            return text[:280]

    def _next_worker_ticket(self) -> Optional[str]:
        """Return the next unclaimed TODO/READY ticket id, or ``None``.

        Autonomous selection: Marcus hands out the lowest-numbered ticket
        that is not yet started and not held by any worker.
        """
        def _key(rec: TicketRecord) -> int:
            try:
                return int(rec.ticket_id)
            except ValueError:
                return abs(hash(rec.ticket_id))

        candidates = [
            r
            for r in self._lifecycle.all_records()
            if r.provider == self._provider
            and r.state in (TicketState.TODO, TicketState.READY)
            and r.ai_agent_id is None
        ]
        candidates.sort(key=_key)
        return candidates[0].ticket_id if candidates else None

    async def orchestrate_work(
        self,
        agent_id: Optional[str] = None,
        report: Optional[str] = None,
        ticket_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Single entry point a worker loops on — Marcus orchestrates it.

        The worker connects and repeatedly calls this. Marcus assigns the
        next available ticket, returns exact instructions, summarizes every
        report onto the ticket as a comment, and completes the ticket
        through the project's gate when the worker reports done.

        Parameters
        ----------
        agent_id : Optional[str]
            Stable worker id. Generated on the first call and echoed back for
            the worker to reuse.
        report : Optional[str]
            The worker's natural-language update of what it just did.
        ticket_id : Optional[str]
            The ticket the worker is on (echoed from a prior response).

        Returns
        -------
        Dict[str, Any]
            ``{status, agent_id, ticket_id?, context?, message}`` where
            ``status`` is one of ``assigned``/``working``/``continue``/
            ``done``/``blocked``/``waiting``/``no_work``.
        """
        agent_id = (agent_id or "").strip() or f"worker-{uuid.uuid4().hex[:8]}"
        active = (ticket_id or "").strip() or self._lifecycle.get_agent_ticket(
            agent_id
        )

        # 1. A report on the worker's active ticket → summarize + act.
        if report and report.strip() and active:
            summary = await self._summarize_report(report)
            await self._post_comment(active, f"🤖 **Worker progress:** {summary}")
            intent = self._classify_report_intent(report)
            if intent == "done":
                await self.signal_ready_for_review(active)
                gate = await self._get_effective_gate(active)
                done_msg = (
                    "Handed off for human review."
                    if gate == "human"
                    else "Verified and merged to main."
                )
                return {
                    "status": "done",
                    "agent_id": agent_id,
                    "ticket_id": active,
                    "message": (
                        f"{done_msg} You're done with this ticket — call "
                        "marcus_work again (no ticket_id) for your next task."
                    ),
                }
            if intent == "blocked":
                await self.set_blocked(active, blocked_by=report.strip()[:200])
                return {
                    "status": "blocked",
                    "agent_id": agent_id,
                    "ticket_id": active,
                    "message": (
                        "Marked blocked. Call marcus_work again (no ticket_id) "
                        "for a different task."
                    ),
                }
            if intent == "waiting":
                await self.set_waiting_for_human(active, reason=report.strip()[:300])
                return {
                    "status": "waiting",
                    "agent_id": agent_id,
                    "ticket_id": active,
                    "message": (
                        "Paused for human input. Call marcus_work again (no "
                        "ticket_id) for a different task."
                    ),
                }
            return {
                "status": "continue",
                "agent_id": agent_id,
                "ticket_id": active,
                "message": (
                    "Progress logged. Keep implementing the acceptance "
                    "criteria and report back in ~10s. Reply "
                    "'DONE - <summary>' when all criteria are met, or "
                    "'BLOCKED - <reason>' if stuck."
                ),
            }

        # 2. Worker already has a ticket → re-send its context.
        if active:
            ctx = await self.get_work_context(active)
            return {
                "status": "working",
                "agent_id": agent_id,
                "ticket_id": active,
                "context": ctx,
                "message": self._worker_instructions(),
            }

        # 3. No active ticket → assign the next available one.
        next_id = self._next_worker_ticket()
        if next_id is None:
            return {
                "status": "no_work",
                "agent_id": agent_id,
                "message": (
                    "No tickets are ready right now. Call marcus_work again "
                    "in ~10s."
                ),
            }
        rec = self._lifecycle.get(next_id, self._provider)
        if rec is not None:
            try:
                self._lifecycle.set_assignee(next_id, self._provider, agent_id)
            except KeyError:
                pass
            rec = self._lifecycle.get(next_id, self._provider) or rec
            await self._start_ai_work(next_id, rec, claim_as=agent_id)

        if self._lifecycle.get_agent_ticket(agent_id) != next_id:
            # Start bailed (e.g. missing project description → parked). Tell
            # the worker to retry; the reason is on the ticket as a comment.
            return {
                "status": "no_work",
                "agent_id": agent_id,
                "message": (
                    "Could not start the next ticket yet (see its comments). "
                    "Call marcus_work again in ~10s."
                ),
            }
        ctx = await self.get_work_context(next_id)
        return {
            "status": "assigned",
            "agent_id": agent_id,
            "ticket_id": next_id,
            "context": ctx,
            "message": self._worker_instructions(),
        }

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

        branch_mgr = await self._branch_for_ticket(ticket_id)
        commits = await branch_mgr.get_branch_commits(record.branch_name)
        comment = CommentFormatter.progress(
            ticket_id=ticket_id,
            branch_name=record.branch_name,
            percentage=percentage,
            message=message,
            commits=commits,
        )
        return await self._post_comment(ticket_id, comment)

    async def signal_ready_for_review(self, ticket_id: str) -> bool:
        """Signal that the AI agent is done.

        **Human gate (default)**: transitions to ``WAITING_FOR_HUMAN``, moves
        the kanban card to ``waiting for human``, and posts a review comment
        asking the human to approve and mark the ticket ``done``.

        **AI gate**: skips the human review step entirely.  The branch is
        merged to main automatically, the kanban card moves to ``done``, and
        a completion comment is posted — identical to what happens when a
        human marks the ticket done in human-gate mode.

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

        # Only a ticket actively IN_PROGRESS can be signalled ready. Without
        # this guard a duplicate call (agent retry, or an LLM calling the tool
        # twice) on an already-WAITING_FOR_HUMAN ticket re-posted the whole
        # "Ready for Review" comment before failing the illegal WFH→WFH
        # transition; in AI-gate mode a duplicate re-ran the merge + DONE
        # sequence on an already-done ticket (duplicate "Merged" comment). A
        # genuine retry after a *failed* post still works — that path leaves
        # the ticket IN_PROGRESS.
        if record.state != TicketState.IN_PROGRESS:
            logger.info(
                "signal_ready_for_review on %s ignored: state is %s, not "
                "in_progress (likely a duplicate call)",
                ticket_id,
                record.state.value,
            )
            return False

        gate = await self._get_effective_gate(ticket_id)

        if gate == "ai":
            return await self._autocomplete_ticket(ticket_id, record)

        # ── Human gate: wait for human review ──────────────────────────
        # Ordering is deliberate: the review comment — the human's only
        # "please review" signal — is posted BEFORE any state changes.
        # The old order transitioned to WAITING_FOR_HUMAN and released
        # the claim first; a brief Kanboard outage then lost the comment
        # and column move, and a retry was impossible forever (the record
        # was already WAITING_FOR_HUMAN, so the transition raised
        # InvalidTransitionError on every subsequent call). A failed post
        # now leaves the ticket IN_PROGRESS and claimed — the agent's
        # tool call returns False and can simply be retried.
        dev_info = self._dev_env.get_info(ticket_id, self._provider)
        dev_url = dev_info.url if dev_info else None

        branch_mgr = await self._branch_for_ticket(ticket_id)
        commits = await branch_mgr.get_branch_commits(record.branch_name)
        ac_items = self._get_ac_items(record)
        comment = CommentFormatter.ready_for_review(
            ticket_id=ticket_id,
            branch_name=record.branch_name,
            ac_items=ac_items,
            dev_env_url=dev_url,
            commit_count=len(commits),
        )
        posted = await self._post_comment(ticket_id, comment)
        if not posted:
            logger.error(
                "Ticket %s: review comment could not be posted — leaving "
                "IN_PROGRESS and claimed so the agent can retry",
                ticket_id,
            )
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

        try:
            await self._kanban.move_task_to_column(ticket_id, "waiting for human")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not update kanban column: %s", exc)

        try:
            self._lifecycle.release_ticket(ticket_id, self._provider)
        except KeyError:
            pass
        await self._pickup_next_ticket()

        return True

    async def set_waiting_for_human(
        self,
        ticket_id: str,
        reason: str = "AI agent requires human input to continue.",
    ) -> bool:
        """Signal that the AI needs external human input.

        **Human gate (default)**: transitions to ``WAITING_FOR_HUMAN`` and
        moves the kanban card to ``waiting for human``.

        **AI gate**: the ticket stays ``in progress``.  A note is posted on
        the ticket so the human can see what the AI asked, but no blocking
        state change occurs — the AI tool call returns success so the agent
        can continue with its best guess.

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

        gate = await self._get_effective_gate(ticket_id)

        if gate == "ai":
            # AI gate: acknowledge but don't block — post a note and continue.
            note = (
                f"🤖 **AI gate active** — AI had a question but is continuing "
                f"autonomously.\n\n> {reason}\n\n"
                "If you want AI to pause for your input on this ticket, "
                "switch it to **Human Gate** in the sidebar."
            )
            logger.info(
                "AI gate: ticket %s asked for human input but will continue (%s)",
                ticket_id,
                reason,
            )
            return await self._post_comment(ticket_id, note)

        # ── Human gate: block until human responds ─────────────────────
        # Comment first, state second — same recoverability guarantee as
        # signal_ready_for_review (see the comment there): a failed post
        # leaves the ticket IN_PROGRESS and claimed for a clean retry.
        comment = CommentFormatter.revision_requested(
            ticket_id=ticket_id,
            human_comment="",
            ai_understanding=reason,
        )
        posted = await self._post_comment(ticket_id, comment)
        if not posted:
            logger.error(
                "Ticket %s: waiting-for-human comment could not be posted — "
                "leaving IN_PROGRESS and claimed so the agent can retry",
                ticket_id,
            )
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

        try:
            self._lifecycle.release_ticket(ticket_id, self._provider)
        except KeyError:
            pass
        await self._pickup_next_ticket()

        return True

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

        # Record the blocker structurally (not just in transition history)
        # so completing the blocking ticket can auto-resume this one — see
        # _resume_tickets_blocked_by.
        try:
            self._lifecycle.set_blocked_by(ticket_id, self._provider, blocked_by)
        except KeyError:
            pass

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

    async def _resume_tickets_blocked_by(self, closed_ticket_id: str) -> None:
        """Auto-resume BLOCKED tickets whose blocker just completed.

        ``signal_blocked`` used to be a one-way street: nothing ever
        watched for the blocking work finishing, so a blocked ticket
        stayed blocked until a human manually dragged the card out of
        the blocked column. Called after a ticket is merged and marked
        DONE (both the human-gate close and the AI-gate autocomplete).

        Assigned matches restart through the normal ``_start_ai_work``
        path (which handles BLOCKED → IN_PROGRESS and re-claims);
        unassigned matches just get a visible comment — assignment is
        still the human's "please work on this" signal.

        Parameters
        ----------
        closed_ticket_id : str
            The ticket that just completed.
        """
        # Scope to this workflow's provider: the lifecycle store can be
        # shared across providers, but _start_ai_work claims under
        # self._provider, so a foreign-provider record would raise KeyError
        # (or claim the wrong record) at claim time.
        matches = [
            r
            for r in self._lifecycle.get_records_blocked_by(closed_ticket_id)
            if r.provider == self._provider
        ]
        for record in matches:
            blocked_id = record.ticket_id
            logger.info(
                "Ticket %s completed — unblocking dependent ticket %s "
                "(was blocked by: %s)",
                closed_ticket_id,
                blocked_id,
                record.blocked_by,
            )
            if self._is_unassigned(record):
                await self._post_comment(
                    blocked_id,
                    f"🔓 Ticket #{closed_ticket_id} (recorded as this "
                    "ticket's blocker) is done and merged. Assign this "
                    "ticket to resume AI work on it.",
                )
                continue
            await self._start_ai_work(blocked_id, record)

    async def _resolve_project_repo_mapping(
        self, kanboard_project_id: Optional[int]
    ) -> Optional[Dict[str, Any]]:
        """Resolve (provisioning on-demand if needed) a project's repo mapping.

        Shared by :meth:`get_work_context` and :meth:`start_dev_environment`
        — both need the ticket's project repo, and nothing in Marcus
        currently publishes a ``project.created`` event, so this on-demand
        lookup is the only path that actually creates the Gitea repo + push
        webhook (see ``ProjectSyncWorkflow.ensure_repo``'s docstring).
        Subsequent calls just hit the cached mapping.

        Parameters
        ----------
        kanboard_project_id : Optional[int]
            The ticket's resolved Kanboard project id, or ``None`` if
            unknown (nothing to resolve against).

        Returns
        -------
        Optional[Dict[str, Any]]
            Dict with ``local_repo_path``/``gitea_repo_url``, or ``None``
            if unresolvable.
        """
        if not self._project_sync or kanboard_project_id is None:
            return None
        mapping = self._project_sync.get_repo_for_project(kanboard_project_id)
        if mapping is None:
            get_project_name = getattr(self._kanban, "get_project_name", None)
            if get_project_name is not None:
                try:
                    project_name = await get_project_name(kanboard_project_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Could not fetch project name for %d: %s",
                        kanboard_project_id,
                        exc,
                    )
                    project_name = None
                if project_name:
                    mapping = await self._project_sync.ensure_repo(
                        kanboard_project_id, project_name
                    )
        return cast(Optional[Dict[str, Any]], mapping)

    async def _branch_for_ticket(self, ticket_id: str) -> BranchManager:
        """Return a BranchManager bound to the ticket's project repository.

        The constructor's default ``BranchManager()`` binds to
        ``os.getcwd()`` — Marcus's own directory, never the project's
        clone under ``data/repos/<slug>``. Running branch operations
        there either fails outright (CWD not a git repo) or, far worse,
        "succeeds" against the wrong repository: tickets get marked DONE
        with a "Merged" comment while the agent's real commits in the
        project repo are never merged, and AI-gate verification reviews
        an empty diff. Every branch call site must therefore resolve the
        ticket → project → ``local_repo_path`` mapping first and operate
        on that repo.

        Falls back to ``self._branch`` (the constructor-supplied manager)
        when no project mapping is resolvable — deployments without
        project sync, and unit tests that inject a mock manager.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.

        Returns
        -------
        BranchManager
            Manager whose ``config.repo_path`` is the project's local
            clone; cached per repo path so all tickets of one project
            share a single instance.
        """
        kanboard_project_id: Optional[int] = None
        try:
            task = await self._kanban.get_task_by_id(ticket_id)
            if task:
                raw = (task.source_context or {}).get("kanboard_task", {})
                project_id_raw = raw.get("project_id")
                if project_id_raw:
                    kanboard_project_id = int(project_id_raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not resolve project for ticket %s: %s", ticket_id, exc
            )

        mapping = await self._resolve_project_repo_mapping(kanboard_project_id)
        repo_path = mapping.get("local_repo_path") if mapping else None
        if not repo_path:
            return self._branch

        cached = self._branch_managers.get(repo_path)
        if cached is None:
            # Typed Any: statically this is always a BranchManagerConfig,
            # but tests inject MagicMock managers whose .config is a mock —
            # the isinstance guard below must stay reachable for them.
            base: Any = self._branch.config
            if isinstance(base, BranchManagerConfig):
                # Preserve main-branch/remote/user settings from the
                # configured manager; only the repo path differs.
                from dataclasses import replace

                cfg = replace(base, repo_path=repo_path)
            else:
                # Test doubles carry a mock config — build from defaults.
                cfg = BranchManagerConfig(repo_path=repo_path)
            cached = BranchManager(cfg)
            self._branch_managers[repo_path] = cached
        return cached

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

        kanboard_project_id: Optional[int] = None
        try:
            task = await self._kanban.get_task_by_id(ticket_id)
            if task:
                src_ctx = task.source_context or {}
                raw = src_ctx.get("kanboard_task", {})
                project_id_raw = raw.get("project_id")
                if project_id_raw:
                    kanboard_project_id = int(project_id_raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch task %s from kanban: %s", ticket_id, exc)

        mapping = await self._resolve_project_repo_mapping(kanboard_project_id)
        repo_path = mapping.get("local_repo_path") if mapping else None

        try:
            info = await self._dev_env.start(
                ticket_id=ticket_id,
                provider=self._provider,
                branch_name=record.branch_name,
                repo_path=repo_path,
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
        the Marcus–Kanboard–Gitea system.  A single call gives the agent:

        - Ticket title and description (from Kanboard)
        - Acceptance criteria checklist (from Marcus lifecycle store)
        - Git branch name to check out
        - Local repository path on disk
        - Gitea remote URL
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
        labels: List[str] = []
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
                # Already parsed onto the Task object by the provider.
                labels = task.labels or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch task %s from kanban: %s", ticket_id, exc)

        # Dependency links and comment history — best-effort; only
        # KanboardKanban implements these (see get_task_links/get_comments
        # docstrings), so skip gracefully for any other provider.
        links: Dict[str, List[Dict[str, str]]] = {
            "depends_on": [],
            "blocks": [],
            "relates_to": [],
        }
        recent_comments: List[Dict[str, Any]] = []
        get_links = getattr(self._kanban, "get_task_links", None)
        if get_links is not None:
            try:
                links = await get_links(ticket_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not fetch links for %s: %s", ticket_id, exc)
        get_comments = getattr(self._kanban, "get_comments", None)
        if get_comments is not None:
            try:
                all_comments = await get_comments(ticket_id)
                # Cap the payload — an agent needs recent clarifications,
                # not a full ticket history transcript.
                recent_comments = all_comments[-10:]
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not fetch comments for %s: %s", ticket_id, exc)

        # Repo info from ProjectSyncWorkflow (if wired up). Provisioned
        # on-demand the first time a ticket's project has no mapping yet —
        # nothing in Marcus currently publishes a `project.created` event
        # (see ProjectSyncWorkflow.ensure_repo's docstring), so this is the
        # only path that actually creates the Gitea repo + push webhook.
        # Subsequent calls just hit the cached mapping.
        local_repo_path: Optional[str] = None
        gitea_repo_url: Optional[str] = None
        clone_url: Optional[str] = None
        repo_web_url: Optional[str] = None
        branch_web_url: Optional[str] = None
        mapping = await self._resolve_project_repo_mapping(kanboard_project_id)
        if mapping:
            local_repo_path = mapping.get("local_repo_path")
            gitea_repo_url = mapping.get("gitea_repo_url")
            if gitea_repo_url:
                urls = self._agent_git_urls(gitea_repo_url, record.branch_name)
                clone_url = urls["clone_url"]
                repo_web_url = urls["repo_web_url"]
                branch_web_url = urls["branch_web_url"]

        gate_is_ai = (
            kanboard_project_id is not None
            and self._gate.get_effective_gate(ticket_id, kanboard_project_id) == "ai"
        )

        return {
            "ticket_id": ticket_id,
            "provider": self._provider,
            "title": title,
            "description": description,
            "acceptance_criteria": record.acceptance_criteria or "",
            "branch_name": record.branch_name,
            # Marcus's OWN internal clone path — the agent no longer uses it
            # for its working copy (it does a fresh clone; see instructions).
            # Kept for reference / co-located tooling.
            "local_repo_path": local_repo_path,
            "gitea_repo_url": gitea_repo_url,
            # Browser-facing, ready-to-use URLs (see _agent_git_urls). The
            # agent clones `clone_url` (credentials embedded when configured)
            # into its OWN directory — never a shared path — so parallel
            # agents never share a working tree.
            "clone_url": clone_url,
            "repo_web_url": repo_web_url,
            "branch_web_url": branch_web_url,
            "state": record.state.value,
            "assignee": record.assignee,
            "already_claimed_by": record.ai_agent_id,
            "labels": labels,
            "links": links,
            "recent_comments": recent_comments,
            # Informational reconnect hint. Hardcoding localhost handed a
            # REMOTE agent a URL pointing at its own machine; honor MARCUS_URL
            # (the deployment's public base) when set.
            "mcp_server_url": self._mcp_server_url(),
            "gate_mode": (
                self._gate.get_effective_gate(ticket_id, kanboard_project_id)
                if kanboard_project_id is not None
                else "human"
            ),
            "instructions": (
                "1. git clone <clone_url> into a NEW directory of your own "
                "(do NOT reuse local_repo_path — that is Marcus's clone), "
                "then cd into it\n"
                "2. git checkout <branch_name>  (it already exists on the "
                "remote; use `git checkout -B <branch_name> origin/<branch_name>`)\n"
                "3. Read the description and acceptance_criteria\n"
                "4. Implement the work; commit and `git push origin <branch_name>`\n"
                "5. Call signal_ready_for_review when done"
                + (
                    " — NOTE: gate_mode is 'ai', so this will auto-merge and "
                    "complete without human review."
                    if gate_is_ai
                    else ", or signal_waiting_for_human / signal_blocked if stuck"
                )
            ),
        }

    async def get_project_description(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        """Return the project description document for a ticket's project.

        The project description is a markdown document maintained per
        Kanboard project (see ``src/core/project_description.py``) — tech
        stack, architecture notes, and context that applies across every
        ticket in the project. It's the same document a human edits at
        ``/project-description?project_id={id}``; this gives an AI agent
        the same read access.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID — used only to resolve which project's
            description to return.

        Returns
        -------
        Optional[Dict[str, Any]]
            ``{"project_id": int, "description": str, "stack": {"language",
            "framework", "install_cmd", "dev_cmd"} | None}`` — ``stack`` is
            the parsed tech-stack info when the description has enough
            structure to extract it, else ``None``. Returns ``None`` if the
            ticket isn't tracked or its project can't be resolved (e.g. a
            non-Kanboard provider).
        """
        project_id: Optional[int] = None
        try:
            task = await self._kanban.get_task_by_id(ticket_id)
            if task:
                src_ctx = task.source_context or {}
                raw = src_ctx.get("kanboard_task", {})
                pid_raw = raw.get("project_id")
                if pid_raw:
                    project_id = int(pid_raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not fetch project_id for description lookup on ticket %s: %s",
                ticket_id,
                exc,
            )

        if project_id is None:
            return None

        from src.core.project_description import ProjectDescriptionManager

        mgr = ProjectDescriptionManager()
        description = mgr.get_description(project_id) or ""
        stack = mgr.get_stack(project_id)
        return {
            "project_id": project_id,
            "description": description,
            "stack": (
                {
                    "language": stack.language,
                    "framework": stack.framework,
                    "install_cmd": stack.install_cmd,
                    "dev_cmd": stack.dev_cmd,
                }
                if stack is not None
                else None
            ),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Parallel-agent slot pool
    # ------------------------------------------------------------------

    def _slot_id(self, index: int) -> str:
        """Return the claim id for parallel slot *index*.

        Slot 0 is :attr:`_agent_id` verbatim (back-compat); every other
        slot appends its index so the ids are distinct and attributable.
        ``get_agent_ticket`` matches ids exactly, so ``marcus-abcd1234``
        (slot 0) and ``marcus-abcd1234-1`` (slot 1) never collide.

        Parameters
        ----------
        index : int
            Slot number in ``range(self._max_parallel_agents)``.

        Returns
        -------
        str
            The slot's claim id.
        """
        return self._agent_id if index == 0 else f"{self._agent_id}-{index}"

    def _free_slot_id(self) -> Optional[str]:
        """Return the id of a free agent slot, or ``None`` if all are busy.

        A slot is free when it currently holds no ticket claim. Slots are
        scanned in order so slot 0 (``_agent_id``) is preferred, which
        keeps single-agent behavior byte-for-byte identical.

        Returns
        -------
        Optional[str]
            A free slot's claim id, or ``None`` when at capacity.
        """
        for i in range(self._max_parallel_agents):
            if self._lifecycle.get_agent_ticket(self._slot_id(i)) is None:
                return self._slot_id(i)
        return None

    def _slot_holding(self, ticket_id: str) -> Optional[str]:
        """Return the slot id already holding *ticket_id*, or ``None``.

        Used to make :meth:`_start_ai_work` idempotent: if one of this
        workflow's slots already claims the ticket, there is nothing new
        to start.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.

        Returns
        -------
        Optional[str]
            The holding slot's id, or ``None`` if no slot holds it.
        """
        for i in range(self._max_parallel_agents):
            sid = self._slot_id(i)
            if self._lifecycle.get_agent_ticket(sid) == ticket_id:
                return sid
        return None

    def _busy_ticket_ids(self) -> List[str]:
        """Return the ticket ids currently held across all slots (for logs)."""
        held: List[str] = []
        for i in range(self._max_parallel_agents):
            tid = self._lifecycle.get_agent_ticket(self._slot_id(i))
            if tid is not None:
                held.append(tid)
        return held

    def _reclaim_for_resume(self, ticket_id: str) -> None:
        """Re-acquire a claim for a ticket resuming to IN_PROGRESS.

        Called from the resume paths (human moved a waiting card back to
        in-progress, commented, or edited the AC). Uses a free agent slot;
        if all slots are busy the ticket is left IN_PROGRESS and unclaimed,
        so :meth:`_pickup_next_ticket` grabs it as soon as a slot frees —
        correct backpressure rather than exceeding the parallel-agent cap.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier being resumed.
        """
        slot_id = self._free_slot_id()
        if slot_id is None:
            logger.info(
                "No free agent slot to resume ticket %s now (cap=%d, busy=%s); "
                "leaving it IN_PROGRESS and unclaimed for pickup when a slot frees",
                ticket_id,
                self._max_parallel_agents,
                ", ".join(self._busy_ticket_ids()) or "none",
            )
            return
        try:
            self._lifecycle.claim_ticket(ticket_id, self._provider, slot_id)
        except KeyError:
            pass

    def _park_in_waiting_for_human(self, ticket_id: str, reason: str) -> None:
        """Move a ticket to WAITING_FOR_HUMAN and release its claim.

        ``WAITING_FOR_HUMAN`` is only reachable from ``IN_PROGRESS``, so this
        walks ``TODO → READY → IN_PROGRESS → WAITING_FOR_HUMAN`` as far as the
        state machine allows. Used to take a ticket out of the *available*
        pool (``READY``/``IN_PROGRESS``) so it awaits a human without being
        re-selected by :meth:`_pickup_next_ticket` in a loop — e.g. a missing
        project description or a merge conflict. Also frees the agent slot.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        reason : str
            Reason recorded on each transition.
        """
        next_state = {
            TicketState.TODO: TicketState.READY,
            TicketState.READY: TicketState.IN_PROGRESS,
            TicketState.IN_PROGRESS: TicketState.WAITING_FOR_HUMAN,
        }
        # At most three hops to climb from TODO to WAITING_FOR_HUMAN.
        for _ in range(len(next_state)):
            cur = self._lifecycle.get(ticket_id, self._provider)
            if cur is None or cur.state == TicketState.WAITING_FOR_HUMAN:
                break
            target = next_state.get(cur.state)
            if target is None:
                # BLOCKED / REOPENED / DONE — not on the WFH path. Leaving the
                # claim released below is enough; these are not "available".
                break
            try:
                self._lifecycle.transition(
                    ticket_id, self._provider, target, reason=reason
                )
            except InvalidTransitionError:
                break
        try:
            self._lifecycle.release_ticket(ticket_id, self._provider)
        except KeyError:
            pass

    async def _start_ai_work(
        self,
        ticket_id: str,
        record: TicketRecord,
        *,
        claim_as: Optional[str] = None,
    ) -> None:
        """Claim the ticket, create branch, set in-progress, and notify AI.

        ``claim_as`` overrides the claim id: in orchestrate mode a specific
        worker holds the claim under its OWN id (bypassing the internal slot
        pool), so ``get_agent_ticket(worker_id)`` resolves the worker's
        ticket. When ``None`` (the human-gated path), the next free parallel
        slot is used as before.

        Called whenever both conditions are met: the ticket IS assigned to
        a human (the assignment is the "please work on this" signal — see
        ``_on_ticket_assigned``) **and** the kanban column is ``READY`` or
        ``IN_PROGRESS`` (or just changed to one of those states). Callers
        enforce both conditions; this method assumes them.

        The claim gate ensures at most one Marcus instance starts work on
        the same ticket concurrently.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        record : TicketRecord
            Current lifecycle record.
        """
        # A DONE record means "reopen in progress": BoardWatcher emits
        # ticket.status_changed BEFORE ticket.reopened for the same poll
        # diff, so this method used to fire first — claiming the ticket
        # and posting "Started" while the record still said DONE — and
        # _on_ticket_reopened then had to unwind it (releasing the claim,
        # rebasing, re-transitioning), leaving a duplicate contradictory
        # "Started" comment behind. Let the reopen handler own that flow.
        if record.state == TicketState.DONE:
            logger.debug(
                "Ticket %s record is DONE — leaving restart to the "
                "reopen handler",
                ticket_id,
            )
            return

        if claim_as is not None:
            # Orchestrate mode: the worker holds the claim under its own id.
            if self._lifecycle.get_agent_ticket(claim_as) == ticket_id:
                return  # this worker already started this ticket
            slot_id = claim_as
        else:
            # Idempotency: if one of this workflow's slots already holds this
            # ticket, there is nothing new to start (a re-entrant call).
            if self._slot_holding(ticket_id) is not None:
                logger.debug(
                    "Ticket %s already held by this workflow; skipping restart",
                    ticket_id,
                )
                return

            # Parallel-agent cap: take the next FREE slot. When every slot is
            # busy the ticket simply waits — it stays available and is picked
            # up by _pickup_next_ticket the moment a slot frees. Busy slots
            # are never preempted, so in-flight work is never interrupted.
            free = self._free_slot_id()
            if free is None:
                logger.info(
                    "All %d agent slot(s) busy (%s); ticket %s waits for a slot",
                    self._max_parallel_agents,
                    ", ".join(self._busy_ticket_ids()) or "none",
                    ticket_id,
                )
                return
            slot_id = free

        # Atomically claim the ticket; abort if another agent already has it.
        claimed = self._lifecycle.claim_ticket(
            ticket_id, self._provider, slot_id
        )
        if not claimed:
            current = self._lifecycle.get(ticket_id, self._provider)
            logger.info(
                "Ticket %s already claimed by %s; skipping",
                ticket_id,
                current.ai_agent_id if current else "unknown",
            )
            return

        # Check that the project description has enough tech-stack info.
        # If the stack is unclear, ask the human and stop until they respond.
        stack_ok = await self._check_project_stack(ticket_id)
        if not stack_ok:
            # _check_project_stack already posted the "need description"
            # comment and moved the board card to "waiting for human". Park
            # the lifecycle record there too (and free the slot). Just
            # releasing left it READY+assigned+unclaimed — still "available"
            # — so every later slot-freeing event re-selected it, re-ran the
            # stack check, and re-posted the same comment (spam on a loop).
            self._park_in_waiting_for_human(
                ticket_id,
                reason="Paused: project description missing tech-stack info",
            )
            return

        # Advance the lifecycle state to IN_PROGRESS via READY if needed.
        if record.state == TicketState.TODO:
            try:
                self._lifecycle.transition(
                    ticket_id,
                    self._provider,
                    TicketState.READY,
                    reason="AI agent starting: ticket assigned and workable",
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
        elif record.state in (
            TicketState.BLOCKED,
            TicketState.WAITING_FOR_HUMAN,
            TicketState.REOPENED,
        ):
            # Re-entry into work from a paused state (all three are legal
            # AI transitions to IN_PROGRESS). Previously this method only
            # advanced TODO/READY and silently left any other state in
            # place while still claiming the ticket and posting "Started"
            # — from BLOCKED or WAITING_FOR_HUMAN the ticket then became
            # un-completable, because signal_ready_for_review cannot
            # legally fire from those states. BLOCKED especially was a
            # dead end: nothing else in the codebase ever executed
            # BLOCKED → IN_PROGRESS, so even a human dragging the card
            # out of the blocked column couldn't truly resume work.
            try:
                self._lifecycle.transition(
                    ticket_id,
                    self._provider,
                    TicketState.IN_PROGRESS,
                    reason=f"Work resuming from {record.state.value}",
                )
            except InvalidTransitionError as exc:
                logger.error("Cannot resume to IN_PROGRESS: %s", exc)
                self._lifecycle.release_ticket(ticket_id, self._provider)
                return

        # Re-fetch after transitions so branch_name is current.
        record = self._lifecycle.get(ticket_id, self._provider) or record

        # Create the ticket branch.
        branch_name = record.branch_name or BranchManager.make_branch_name(
            self._provider, ticket_id
        )
        branch_mgr = await self._branch_for_ticket(ticket_id)
        created = await branch_mgr.create_branch(branch_name)
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
        """Fill every free agent slot with the next available tickets.

        Called whenever a ticket frees a slot — it moved to
        ``WAITING_FOR_HUMAN``, ``BLOCKED``, or ``DONE``, or a human
        unassigned it or reset it to ``TODO`` — so idle slots do not sit
        unused while assigned work is ready. Starts work on as many
        available tickets as there are free slots — up to the parallel-agent
        cap — and leaves the rest to wait.

        Selection order (dependency approximation):

        1. ``READY`` tickets before ``IN_PROGRESS`` ones.
        2. Lower numeric ticket ID first (earlier-created tickets are more
           likely to be prerequisites for later work).
        """
        # Scope to this workflow's provider — get_available_tickets() spans
        # every provider in a shared store, but _start_ai_work claims under
        # self._provider (a foreign record would KeyError or mis-claim).
        candidates = [
            r
            for r in self._lifecycle.get_available_tickets()
            if r.provider == self._provider
        ]
        if not candidates:
            logger.debug("No next ticket to pick up (no available work)")
            return

        candidates.sort(key=_ticket_priority_key)
        for next_rec in candidates:
            # Stop as soon as we are at capacity — remaining tickets wait.
            if self._free_slot_id() is None:
                logger.debug(
                    "All %d agent slot(s) busy; remaining available tickets wait",
                    self._max_parallel_agents,
                )
                break
            logger.info(
                "Picking up next ticket: %s (state=%s)",
                next_rec.ticket_id,
                next_rec.state.value,
            )
            await self._start_ai_work(next_rec.ticket_id, next_rec)

    async def _resolve_kanboard_project_id(
        self, ticket_id: str
    ) -> Optional[int]:
        """Best-effort resolve a ticket's Kanboard project id, or ``None``."""
        try:
            task = await self._kanban.get_task_by_id(ticket_id)
            if task:
                raw = (task.source_context or {}).get("kanboard_task", {})
                pid_raw = raw.get("project_id")
                if pid_raw:
                    return int(pid_raw)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not resolve project id for %s: %s", ticket_id, exc)
        return None

    def _project_internal_repo_url(
        self, project_id: Optional[int]
    ) -> Optional[str]:
        """Return a project's stored (internal) Gitea clone URL, or ``None``.

        NON-provisioning: uses the cached mapping only, so read-only callers
        (the Kanboard UI link routes) never trigger repo creation as a side
        effect. Returns ``None`` until the repo has actually been provisioned.
        """
        if self._project_sync is None or project_id is None:
            return None
        mapping = self._project_sync.get_repo_for_project(project_id)
        if not mapping:
            return None
        return cast(Optional[str], mapping.get("gitea_repo_url"))

    async def get_repo_links(self, ticket_id: str) -> Optional[Dict[str, str]]:
        """Return browser links to a ticket's repo and branch, or ``None``.

        Credential-free (unlike ``get_work_context``'s ``clone_url``) — these
        are for humans clicking through from Kanboard. Non-provisioning:
        returns ``None`` until the project's repo exists.

        Parameters
        ----------
        ticket_id : str
            Kanboard task id.

        Returns
        -------
        Optional[Dict[str, str]]
            ``{repo_web_url, branch_web_url}`` or ``None``.
        """
        project_id = await self._resolve_kanboard_project_id(ticket_id)
        internal = self._project_internal_repo_url(project_id)
        if not internal:
            return None
        record = self._lifecycle.get(ticket_id, self._provider)
        branch = (
            record.branch_name
            if record and record.branch_name
            else BranchManager.make_branch_name(self._provider, ticket_id)
        )
        urls = self._agent_git_urls(internal, branch)
        return {
            "repo_web_url": urls["repo_web_url"],
            "branch_web_url": urls["branch_web_url"],
        }

    async def apply_agent_project_description(
        self, ticket_id: str, text: str
    ) -> Dict[str, Any]:
        """Store an agent-supplied project description, unless a human locked it.

        Written with ``SOURCE_AGENT`` (still auto-updatable), so a human's
        later correction wins. Refuses if a human has already edited the
        description.

        Parameters
        ----------
        ticket_id : str
            Any ticket in the target project.
        text : str
            Full markdown description to store.

        Returns
        -------
        Dict[str, Any]
            ``{updated: bool, project_id?: int, reason?: str}``.
        """
        from src.core.project_description import (
            SOURCE_AGENT,
            ProjectDescriptionManager,
        )

        project_id = await self._resolve_kanboard_project_id(ticket_id)
        if project_id is None:
            return {"updated": False, "reason": "could not resolve a project"}
        mgr = ProjectDescriptionManager()
        if not mgr.can_auto_update(project_id):
            return {
                "updated": False,
                "project_id": project_id,
                "reason": "a human has edited this description; not overwriting",
            }
        try:
            mgr.update_description(project_id, text, source=SOURCE_AGENT)
        except Exception as exc:  # noqa: BLE001
            return {
                "updated": False,
                "project_id": project_id,
                "reason": f"could not write description: {exc}",
            }
        return {"updated": True, "project_id": project_id}

    def get_project_repo_url(self, project_id: int) -> Optional[str]:
        """Return the browser URL of a project's Gitea repo, or ``None``.

        Non-provisioning (cached mapping only). Used by the Kanboard board
        header to link a project to its repository.

        Parameters
        ----------
        project_id : int
            Kanboard project id.

        Returns
        -------
        Optional[str]
            The repo's browser URL, or ``None`` if not provisioned yet.
        """
        internal = self._project_internal_repo_url(project_id)
        if not internal:
            return None
        return self._agent_git_urls(internal, "")["repo_web_url"]

    def _agent_git_urls(
        self, internal_clone_url: str, branch_name: str
    ) -> Dict[str, str]:
        """Build browser-facing git URLs to hand an agent for a ticket.

        Marcus stores clone URLs on its OWN internal Gitea address (e.g.
        ``http://gitea:3000`` in Docker), which a remote agent or a human's
        browser cannot reach. This rehosts them onto ``GITEA_PUBLIC_URL``
        (default ``http://localhost:3000``) and returns:

        - ``clone_url`` — ready to ``git clone``. Credentials are embedded
          (so a private repo clones with no separate setup) when
          ``MARCUS_EMBED_GIT_CREDENTIALS`` is truthy (default) AND a token is
          available. **Security:** this hands a Gitea token to every agent
          and into its LLM context — prefer a dedicated, repo-scoped token
          via ``GITEA_AGENT_TOKEN`` over the admin ``GITEA_TOKEN``; set
          ``MARCUS_EMBED_GIT_CREDENTIALS=false`` to return a credential-less
          URL and configure git auth on the agent host instead.
        - ``repo_web_url`` — browser link to the repository.
        - ``branch_web_url`` — browser link to this ticket's branch.

        Parameters
        ----------
        internal_clone_url : str
            The ``gitea_repo_url`` from the project mapping.
        branch_name : str
            The ticket's branch.

        Returns
        -------
        Dict[str, str]
            ``{clone_url, repo_web_url, branch_web_url}``.
        """
        from src.integrations.gitea_manager import (
            public_authenticated_clone_url,
            public_branch_web_url,
            public_repo_web_url,
        )

        public_base = (
            os.environ.get("GITEA_PUBLIC_URL") or "http://localhost:3000"
        ).strip()

        embed = os.environ.get(
            "MARCUS_EMBED_GIT_CREDENTIALS", "true"
        ).strip().lower() in ("1", "true", "yes", "on")

        # Prefer a dedicated, repo-scoped agent token (a leak is contained to
        # the project repos) over Marcus's admin GITEA_TOKEN (full instance
        # access). GITEA_AGENT_USERNAME names that token's owner for HTTP
        # Basic auth; default to the admin username otherwise.
        gitea = getattr(self._project_sync, "_gitea", None)
        admin_user = (getattr(gitea, "_username", None) if gitea else None) or "root"
        agent_token = os.environ.get("GITEA_AGENT_TOKEN", "").strip()
        if agent_token:
            token = agent_token
            username = os.environ.get("GITEA_AGENT_USERNAME", "").strip() or admin_user
        else:
            token = (
                (getattr(gitea, "_token", None) if gitea else None)
                or os.environ.get("GITEA_TOKEN", "")
            )
            username = admin_user

        clone_url = (
            public_authenticated_clone_url(
                internal_clone_url, public_base, username, token
            )
            if embed
            else public_repo_web_url(internal_clone_url, public_base) + ".git"
        )
        return {
            "clone_url": clone_url,
            "repo_web_url": public_repo_web_url(internal_clone_url, public_base),
            "branch_web_url": public_branch_web_url(
                internal_clone_url, public_base, branch_name
            ),
        }

    @staticmethod
    def _mcp_server_url() -> str:
        """Return the MCP endpoint URL to advertise to agents.

        Prefers ``MARCUS_URL`` (the deployment's public base, set by
        ``scripts/setup.sh`` for remote access) so a remote agent gets a
        reachable address; falls back to the localhost default otherwise.

        Returns
        -------
        str
            The ``/mcp`` endpoint URL.
        """
        base = (os.environ.get("MARCUS_URL") or "").strip().rstrip("/")
        if base:
            return f"{base}/mcp"
        return "http://localhost:4298/mcp"

    @staticmethod
    def _is_approval_comment(body: str) -> bool:
        """Return ``True`` if a human comment means "approve and merge".

        Recognizes the explicit ``@marcus approve`` / ``@marcus merge``
        command, and plain natural approvals (``approve``, ``approved``,
        ``lgtm``, ``ship it``, ``merge``, ``looks good``) — but never when
        the comment is negated or conditional (``don't merge``, ``approve
        after you fix…``), so a nuanced review isn't mistaken for a blanket
        approval.

        Parameters
        ----------
        body : str
            The human comment text.

        Returns
        -------
        bool
            ``True`` if the comment is an unconditional approval.
        """
        text = body.strip().lower()
        if not text:
            return False
        if CommentParser.contains_command(body, "approve") or (
            CommentParser.contains_command(body, "merge")
        ):
            return True
        # Never treat a negated / conditional comment as approval.
        negations = (
            "don't", "do not", "not ", "n't", " after ", " once ",
            "unless", " but ", "wait", "hold", "before",
        )
        if any(neg in text for neg in negations):
            return False
        approvals = (
            "approve",
            "approved",
            "lgtm",
            "ship it",
            "merge",
            "looks good",
            "looks great",
            "good to merge",
            "good to go",
        )
        return text.startswith(approvals)

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

    async def _autocomplete_ticket(
        self,
        ticket_id: str,
        record: TicketRecord,
    ) -> bool:
        """Merge and complete a ticket without waiting for human review.

        Used by :meth:`signal_ready_for_review` when the effective gate is
        ``"ai"``.  Replicates the merge + DONE transition that normally
        happens in :meth:`_on_ticket_closed` when a human marks the card done.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        record : TicketRecord
            Current lifecycle record (for branch name / AC).

        Returns
        -------
        bool
            ``True`` on success.
        """
        branch_name = record.branch_name
        branch_mgr = await self._branch_for_ticket(ticket_id)
        main_branch = branch_mgr.config.main_branch

        if not branch_name:
            await self._post_error(
                ticket_id,
                "Cannot auto-merge: no branch was created for this ticket.",
            )
            return False

        # ── AI verification (multi-round when enabled) ─────────────────────────
        # Each call to signal_ready_for_review completes one round.  When the
        # configured verify_count > 0 we track how many rounds are done in
        # self._ticket_verify_rounds.  Only when all rounds pass does the
        # branch merge.
        verify_count = await self._get_effective_verify_count(ticket_id)
        if verify_count > 0:
            rounds_done = self._ticket_verify_rounds.get(ticket_id, 0)

            if rounds_done >= verify_count:
                # All N rounds completed and the agent made the final fix.
                # Clear the counter and fall through to merge.
                self._ticket_verify_rounds.pop(ticket_id, None)

            else:
                current_round = rounds_done + 1
                result = await self._run_verification_round(ticket_id, record, branch_name)
                self._ticket_verify_rounds[ticket_id] = current_round

                if result.passed and current_round == verify_count:
                    # Last round passed → post the final round comment then merge.
                    self._ticket_verify_rounds.pop(ticket_id, None)
                    comment = CommentFormatter.verification_round_result(
                        ticket_id, current_round, verify_count, result
                    )
                    await self._post_comment(ticket_id, comment)
                    # fall through to merge

                else:
                    # Issues found (any round) OR passed but more rounds remain.
                    # Post a round-result comment, release the ticket so the agent
                    # can pick it up again to fix issues (or re-signal if clean).
                    comment = CommentFormatter.verification_round_result(
                        ticket_id, current_round, verify_count, result
                    )
                    await self._post_comment(ticket_id, comment)
                    try:
                        self._lifecycle.release_ticket(ticket_id, self._provider)
                    except KeyError:
                        pass
                    try:
                        await self._kanban.move_task_to_column(ticket_id, "in progress")
                    except Exception:  # noqa: BLE001
                        pass
                    await self._pickup_next_ticket()
                    return False

        merge_msg = (
            f"merge: ticket/{self._provider}/{ticket_id} (auto-completed, AI gate)"
        )
        merged = await branch_mgr.merge_to_main(branch_name, commit_message=merge_msg)

        if not merged:
            # Clean up the verify-round counter so a retry starts fresh.
            self._ticket_verify_rounds.pop(ticket_id, None)
            await self._post_error(
                ticket_id,
                f"Auto-merge of `{branch_name}` to `{main_branch}` failed — "
                "there may be conflicts.  Please merge manually or rebase the branch.",
            )
            return False

        try:
            self._lifecycle.transition(
                ticket_id,
                self._provider,
                TicketState.DONE,
                reason="AI gate: auto-completed after AI signalled ready",
            )
        except InvalidTransitionError:
            try:
                self._lifecycle.human_transition(
                    ticket_id,
                    self._provider,
                    TicketState.DONE,
                    reason="AI gate: forced DONE after auto-merge",
                )
            except (InvalidTransitionError, KeyError):
                logger.error(
                    "Could not transition ticket %s to DONE after merge; "
                    "lifecycle state is inconsistent",
                    ticket_id,
                )
                return False

        try:
            self._lifecycle.set_merged(ticket_id, self._provider)
        except KeyError:
            pass
        try:
            self._lifecycle.release_ticket(ticket_id, self._provider)
        except KeyError:
            pass

        await self._dev_env.stop(ticket_id, self._provider)

        try:
            await self._kanban.move_task_to_column(ticket_id, "done")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not move ticket %s to done: %s", ticket_id, exc)

        comment = CommentFormatter.merged(
            ticket_id=ticket_id,
            branch_name=branch_name,
            main_branch=main_branch,
        )
        await self._post_comment(ticket_id, comment)
        logger.info(
            "AI gate: ticket %s auto-completed and merged to %s", ticket_id, main_branch
        )

        # This completion may unblock other tickets.
        await self._resume_tickets_blocked_by(ticket_id)

        await self._pickup_next_ticket()
        return True

    async def _get_effective_gate(self, ticket_id: str) -> GateMode:
        """Resolve the effective gate mode for a ticket.

        Fetches the kanboard task to discover its project ID, then calls
        ``GateSettingManager.get_effective_gate``.  On any error the safe
        default ``"human"`` is returned.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID.

        Returns
        -------
        GateMode
            ``"human"`` or ``"ai"``.
        """
        project_id: Optional[int] = None
        try:
            task = await self._kanban.get_task_by_id(ticket_id)
            if task:
                src_ctx = task.source_context or {}
                raw = src_ctx.get("kanboard_task", {})
                pid_raw = raw.get("project_id")
                if pid_raw:
                    project_id = int(pid_raw)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not fetch project_id for gate check on %s: %s", ticket_id, exc)

        if project_id is None:
            return "human"
        return self._gate.get_effective_gate(ticket_id, project_id)

    async def _run_verification_round(
        self,
        ticket_id: str,
        record: TicketRecord,
        branch_name: str,
    ) -> VerificationResult:
        """Run one LLM verification pass and return the raw result.

        This method has NO side effects — it does not post comments or release
        tickets.  The caller in ``_autocomplete_ticket`` handles those actions
        based on the result and the current round number.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        record : TicketRecord
            Current lifecycle record (for AC items and title).
        branch_name : str
            Branch to diff and verify.

        Returns
        -------
        VerificationResult
            Passed/failed result from the LLM.  On diff error the result is
            ``passed=True`` (fail-open — a transient diff failure should not
            block merging).
        """
        logger.info(
            "AI Verify: running verification round for ticket %s (branch %s)",
            ticket_id,
            branch_name,
        )

        try:
            branch_mgr = await self._branch_for_ticket(ticket_id)
            diff_text = await branch_mgr.get_branch_diff(branch_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "AI Verify: could not get diff for %s: %s — passing (fail-open)",
                branch_name,
                exc,
            )
            await self._post_verification_skipped_notice(
                ticket_id, f"could not read the branch diff ({exc})"
            )
            return VerificationResult(passed=True, findings=[], raw_response="")

        ac_items = self._get_ac_items(record)
        ticket_title = ticket_id
        try:
            task = await self._kanban.get_task_by_id(ticket_id)
            if task and task.name:
                ticket_title = task.name
        except Exception:  # noqa: BLE001
            pass

        try:
            return await self._verifier.verify(
                ticket_id=ticket_id,
                ticket_title=ticket_title,
                acceptance_criteria=ac_items,
                diff_text=diff_text,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "AI Verify: verifier error for ticket %s: %s — passing (fail-open)",
                ticket_id,
                exc,
            )
            await self._post_verification_skipped_notice(
                ticket_id, f"the verification LLM call failed ({exc})"
            )
            return VerificationResult(passed=True, findings=[], raw_response="")

    async def _post_verification_skipped_notice(
        self, ticket_id: str, cause: str
    ) -> None:
        """Post a visible notice that an AI-verify round was skipped.

        The fail-open behavior itself is deliberate (a transient diff or
        LLM failure should not block an auto-merge forever), but it was
        previously SILENT — under a persistent failure (bad LLM
        credentials, wrong repo path) every round "passed" at
        warning-log level and AI-gate verification quietly degraded to
        zero review. The human configured verification precisely to get
        review before merges, so the skip must be visible where they
        look: on the ticket.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        cause : str
            Short human-readable reason the round could not run.
        """
        notice = (
            "⚠️ **AI verification round skipped** — this round was counted "
            f"as passed because {cause}.\n\n"
            "If this keeps happening, the AI-gate verification you "
            "configured is NOT actually reviewing changes — check "
            "Marcus's logs before trusting auto-merged tickets."
        )
        await self._post_comment(ticket_id, notice)

    async def _get_effective_verify_count(self, ticket_id: str) -> int:
        """Resolve how many verification rounds are configured for a ticket.

        Fetches the kanboard task to discover its project ID, then calls
        ``GateSettingManager.get_effective_verify_count``.  Returns ``1`` on
        any kanban API error (fail-safe — a transient outage should not
        silently bypass all verification rounds).

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID.

        Returns
        -------
        int
            Number of required verification rounds (0 = disabled).
        """
        project_id: Optional[int] = None
        try:
            task = await self._kanban.get_task_by_id(ticket_id)
            if task:
                src_ctx = task.source_context or {}
                raw = src_ctx.get("kanboard_task", {})
                pid_raw = raw.get("project_id")
                if pid_raw:
                    project_id = int(pid_raw)
        except Exception as exc:  # noqa: BLE001
            # Kanban API is unreachable — fail-safe: assume at least one round
            # rather than silently allowing unreviewed branches to auto-merge.
            logger.warning(
                "Could not fetch project_id for verify check on ticket %s: %s "
                "— defaulting to verify_count=1 (fail-safe)",
                ticket_id,
                exc,
            )
            return 1

        if project_id is None:
            # Task has no project_id in its source context (e.g. non-Kanboard
            # provider or task not yet fully synced). Verification not configured.
            return 0
        return self._gate.get_effective_verify_count(ticket_id, project_id)

    async def _infer_project_description(
        self,
        ticket_id: str,
        project_id: int,
        mgr: Any,
    ) -> bool:
        """Infer + store a project description from this ticket.

        Returns ``True`` only if the inferred description now yields a
        parseable tech stack (so ``_check_project_stack`` can proceed
        instead of pausing on the human). The write is stamped
        :data:`SOURCE_INFERRED`, so a human's later edit still wins, and a
        comment tells the human where to correct it.

        Parameters
        ----------
        ticket_id : str
            Kanboard task id whose content drives the inference.
        project_id : int
            The ticket's project.
        mgr : ProjectDescriptionManager
            Already-constructed manager (shares the data dir).

        Returns
        -------
        bool
            ``True`` if a usable description was inferred and stored.
        """
        from src.core.project_description import SOURCE_INFERRED

        if self._desc_inferrer is None:
            return False

        # Gather ticket content + a project name for the inference prompt.
        title = ticket_id
        description = ""
        project_name = f"Project {project_id}"
        try:
            task = await self._kanban.get_task_by_id(ticket_id)
            if task:
                title = task.name or title
                description = task.description or ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("Infer: could not fetch ticket %s: %s", ticket_id, exc)
        get_project_name = getattr(self._kanban, "get_project_name", None)
        if get_project_name is not None:
            try:
                name = await get_project_name(project_id)
                if name:
                    project_name = name
            except Exception:  # noqa: BLE001
                pass

        record = self._lifecycle.get(ticket_id, self._provider)
        ac = record.acceptance_criteria if record else ""

        try:
            inferred = await self._desc_inferrer.infer(
                project_name, title, description, ac
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Project description inference failed: %s", exc)
            return False

        if not inferred:
            return False

        try:
            mgr.update_description(project_id, inferred, source=SOURCE_INFERRED)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not store inferred description: %s", exc)
            return False

        if mgr.get_stack(project_id) is None:
            # Inference produced text but still no usable stack — don't
            # pretend it's ready; let the caller pause on the human.
            return False

        logger.info(
            "Inferred project description for project %d from ticket %s",
            project_id,
            ticket_id,
        )
        await self._post_comment(
            ticket_id,
            "🧭 **Marcus inferred this project's tech stack** from the ticket "
            "so work can start now. If it's wrong, open the **Project "
            "Description** page (button in the board header) and correct it — "
            "your edit takes over from Marcus's guess.",
        )
        return True

    async def _check_project_stack(self, ticket_id: str) -> bool:
        """Verify the project description has enough stack info to start work.

        If the stack cannot be determined, post a clarification comment on the
        ticket and move it to "waiting for human" so the human can fill in the
        Project Description before work resumes.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID.

        Returns
        -------
        bool
            ``True`` if the stack is known (or check is not applicable);
            ``False`` if the ticket was paused awaiting human input.
        """
        try:
            from src.core.project_description import (
                ProjectDescriptionManager,
                _WAITING_COMMENT,
            )

            project_id: Optional[int] = None
            try:
                task = await self._kanban.get_task_by_id(ticket_id)
                if task:
                    src_ctx = task.source_context or {}
                    raw = src_ctx.get("kanboard_task", {})
                    pid_raw = raw.get("project_id")
                    if pid_raw:
                        project_id = int(pid_raw)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not fetch task for stack check: %s", exc)

            if project_id is None:
                return True  # non-Kanboard providers skip description check

            mgr = ProjectDescriptionManager()
            stack = mgr.get_stack(project_id)
            if stack is not None:
                return True  # description is complete — proceed normally

            # Stack missing. Before pausing on the human, try to INFER the
            # project description from this ticket — but never over a
            # description a human has already edited (that correction wins).
            if self._desc_inferrer is not None and mgr.can_auto_update(project_id):
                if await self._infer_project_description(
                    ticket_id, project_id, mgr
                ):
                    return True

            # Stack still unknown: ask the human and pause.
            await self._post_comment(ticket_id, _WAITING_COMMENT)
            try:
                await self._kanban.move_task_to_column(ticket_id, "waiting for human")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not move ticket to waiting for human: %s", exc)
            logger.info(
                "Ticket %s paused — project description missing tech-stack info",
                ticket_id,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("Project stack check failed, proceeding anyway: %s", exc)
            return True

    async def _post_comment(self, ticket_id: str, body: str) -> bool:
        """Post a comment via the kanban provider (best-effort).

        Also emits ``ui.refresh`` so the SSE stream
        (``/api/events/stream``) pushes an instant page refresh to open
        Kanboard tabs — every Marcus/agent update posts a comment, so this
        one hook covers them all with no polling and no delay.
        """
        try:
            result = await self._kanban.add_comment(ticket_id, body)
            await self._signal_ui_refresh(ticket_id)
            return bool(result)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to post comment on %s: %s", ticket_id, exc)
            return False

    async def _signal_ui_refresh(self, ticket_id: str) -> None:
        """Publish ``ui.refresh`` so the SSE stream refreshes the Kanboard UI."""
        try:
            await self._events.publish(
                "ui.refresh",
                source="human_gated_workflow",
                data={"ticket_id": ticket_id, "provider": self._provider},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not emit ui.refresh for %s: %s", ticket_id, exc)

    async def _post_error(self, ticket_id: str, error_summary: str) -> None:
        """Post an error comment on a ticket."""
        comment = CommentFormatter.error(
            ticket_id=ticket_id, error_summary=error_summary
        )
        await self._post_comment(ticket_id, comment)

    async def _on_watcher_error(self, exc: Exception) -> None:
        """Handle a poll cycle failure reported by the BoardWatcher."""
        logger.error("Board watcher error in HumanGatedWorkflow: %s", exc)
