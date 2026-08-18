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

import asyncio
import logging
import math
import os
import textwrap
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, cast

from src.ai.verification.ai_verifier import AIVerifier, VerificationResult
from src.core.acceptance_criteria import ACChangeDetector, ACGenerator, ACParser
from src.core.board_watcher import BoardWatcher
from src.core.comment_protocol import CommentFormatter, CommentParser
from src.core.dev_environment import DevEnvironmentManager
from src.core.events import Events
from src.core.gate_settings import GateMode, GateSettingManager
from src.core.project_access_settings import ProjectAccessSettingManager
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

#: How long after an agent's last progress report a ticket is still considered
#: "actively worked" for the board highlight. Agents in orchestrate mode report
#: roughly every 10s, so this comfortably spans a few missed beats; a longer
#: silence (the agent finished, crashed, or stalled) lets the highlight lapse.
_WORKING_WINDOW_SECONDS = 40.0

#: How long after an agent's last contact it still counts as "connected".
#: MUST be >= _WORKING_WINDOW_SECONDS: any agent whose ticket is still lit as
#: "working" has to also count as connected, otherwise the board shows a lit
#: golden ring but 0 connected/working agents. Real agents contact Marcus via
#: several channels at different cadences (a marcus_work poll, a progress
#: report, or a branch push — each refreshes this stamp), and a work chunk
#: between contacts can easily exceed 30s, so keep this generous; a longer
#: silence means the agent has disconnected/stopped.
_AGENT_POLL_WINDOW = 60.0

#: How long an IN_PROGRESS ticket may go without any sign of life from its
#: claiming agent before Marcus treats it as abandoned and reclaims it for
#: whichever agent asks next (see _reclaim_stuck_ticket). Deliberately much
#: larger than _WORKING_WINDOW_SECONDS/_AGENT_POLL_WINDOW above (those tune
#: a live UI indicator refreshed every ~10-15s; this tunes when Marcus
#: actively takes an ACTION on a ticket) — long enough that an agent mid a
#: slow build/test cycle is never punished, short enough that a session
#: that lost track of its own agent_id (a fresh session, a compacted
#: context) doesn't leave real work abandoned indefinitely.
_STUCK_AGENT_TIMEOUT_SECONDS = 600.0

#: How stale a board read may be and still be reused when a worker asks for
#: its next ticket. Every marcus_work poll re-reads the enabled boards so a
#: just-readied ticket is handed out immediately rather than after the next
#: BoardWatcher tick — but with several agents polling every ~10s, an
#: unconditional read per poll would be two RPCs per enabled project each
#: time, straight into Kanboard's SQLite backend. A short window lets
#: near-simultaneous polls share one read without adding noticeable delay.
_BOARD_RESCAN_MAX_AGE = 3.0

#: How often the background sweep re-checks every BLOCKED (decomposed
#: parent) ticket for whether all its children have reached DONE. This is
#: a SAFETY NET alongside the event-driven path (_maybe_complete_parent,
#: triggered when a child ticket closes) — that path can be missed
#: entirely (a dropped webhook, a restart landing between the last
#: child's completion and the parent check running, or any other gap),
#: silently leaving a parent stuck in Blocked forever even though every
#: child is done. The sweep only does cheap in-memory lifecycle-record
#: iteration (no RPC calls unless it actually finds something to
#: complete), so a short interval costs nothing.
_PARENT_RECONCILE_INTERVAL_SECONDS = 60.0

#: Hard caps applied to the agent-supplied ``usage`` payload — it is fully
#: untrusted (any connected agent can send anything) and flows into stored
#: state and the Kanboard UI, so it is sanitized on ingest to bound memory and
#: keep values to safe JSON scalars.
_MAX_TRACKED_ACCOUNTS = 100   # distinct accounts kept in memory (evict oldest)
_ACCOUNT_ID_MAX = 128         # max length of an account id / agent id
_USAGE_SCALAR_MAX = 64        # max length of a used/limit/unit string value

#: Matches AIVerifier's own diff cap (src/ai/verification/ai_verifier.py) —
#: keeps the testing-instructions prompt size bounded regardless of ticket
#: size.
_MAX_TESTING_DIFF_CHARS = 12_000

#: Hard cap on the LLM's raw output before it's posted to the ticket —
#: matches _summarize_report's [:400] safety net (below), just sized for
#: a multi-step numbered list rather than a one-line summary. The prompt
#: already asks for something short; this is the defensive backstop for
#: when it doesn't comply.
_MAX_TESTING_INSTRUCTIONS_CHARS = 3_000

#: KanboardKanban.move_task_to_column() often fails CLEANLY (returns False,
#: no exception) rather than raising — e.g. a transient SQLite lock during
#: the move or its verifying re-fetch. A short retry survives that kind of
#: one-off blip without adding meaningful latency to a completion signal.
_COLUMN_MOVE_MAX_ATTEMPTS = 3
_COLUMN_MOVE_RETRY_DELAY_SECONDS = 1.0


def _safe_usage_scalar(value: Any, max_len: int = _USAGE_SCALAR_MAX) -> Any:
    """Coerce an agent-supplied usage value to a safe JSON scalar, or ``None``.

    Accepts a finite number or a length-capped string; everything else
    (bools, NaN/Inf, dicts, lists, objects) becomes ``None``. This keeps a
    malicious or buggy agent from planting non-serializable values, huge
    blobs, or nested structures in Marcus's state or the ``/api/active-agents``
    response.
    """
    if isinstance(value, bool):
        return None  # bool is an int subclass — not a usage number
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:max_len]
    return None


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
        project_access: Optional[ProjectAccessSettingManager] = None,
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
        self._project_access = project_access or ProjectAccessSettingManager()
        # ticket id → Kanboard project id, and project id → project name.
        # Both are effectively immutable, and resolving them costs an RPC
        # each — see _resolve_kanboard_project_id for why that matters on
        # a 10-second agent poll.
        self._ticket_project_ids: Dict[str, int] = {}
        self._project_names: Dict[int, str] = {}
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
        # Liveness heartbeat for the board's "actively worked" highlight:
        # ``{"<provider>:<ticket_id>": <monotonic ts>}`` set to *now* each time
        # an agent reports progress on a ticket. This is a pure ACTIVITY
        # signal, deliberately decoupled from ticket STATE — a bug that leaves
        # a ticket stuck in a column must never turn the highlight on or off.
        # In-memory only: a Marcus restart clears it and agents re-populate it
        # on their next report.
        self._progress_activity: Dict[str, float] = {}
        # "Connected agents" heartbeat: ``{agent_id: <monotonic ts>}`` stamped on
        # EVERY marcus_work poll (see orchestrate_work), whether or not the agent
        # got work. Powers the board's "connected" count — an idle-but-polling
        # agent still counts. In-memory only (a restart clears it; agents
        # repopulate on their next poll).
        self._agent_seen: Dict[str, float] = {}
        # Subscription/account usage the agents self-report via marcus_work,
        # keyed by ACCOUNT so agents sharing one subscription share one figure:
        # ``{account_id: {"used", "limit", "unit", "ts"}}`` (limit None = ∞, e.g.
        # a self-hosted model). Plus ``{agent_id: account_id}`` so a ticket's
        # working agent maps to its account's usage for display.
        self._account_usage: Dict[str, Dict[str, Any]] = {}
        self._agent_account: Dict[str, str] = {}
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
        # Whether the MOST RECENTLY completed round actually passed —
        # tracked separately from the round count above so a mid-flight
        # verify_count decrease (via the live gate-setting API) can never
        # make _autocomplete_ticket treat "N rounds have been ATTEMPTED"
        # as "safe to merge" when the last attempt genuinely failed. Both
        # dicts are cleared together — see every call site.
        self._ticket_verify_last_passed: Dict[str, bool] = {}
        # Background sweep task for _reconcile_blocked_parents — see
        # _PARENT_RECONCILE_INTERVAL_SECONDS.
        self._parent_reconcile_task: Optional[asyncio.Task[None]] = None
        self._parent_reconcile_running = False

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
        # Re-sync with the board before anything else looks at the
        # lifecycle: tickets deleted while Marcus was down are invisible to
        # BoardWatcher (its snapshots start empty), so this is the only
        # thing that notices them.
        try:
            await self._reconcile_deleted_tickets()
        except Exception as exc:  # noqa: BLE001 - never block startup
            logger.warning("Startup board reconcile failed: %s", exc)

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
        # A parent stuck in Blocked with all children already Done from
        # BEFORE this restart must not wait a full sweep interval to be
        # caught — check once immediately (the background loop below
        # handles every LATER occurrence on its own cadence).
        try:
            await self._reconcile_blocked_parents()
        except Exception as exc:  # noqa: BLE001 - never block startup
            logger.warning("Startup parent reconcile failed: %s", exc)
        await self._watcher.start()
        if not self._parent_reconcile_running:
            self._parent_reconcile_running = True
            self._parent_reconcile_task = asyncio.create_task(
                self._parent_reconcile_loop(), name="parent-reconcile"
            )
        logger.info("HumanGatedWorkflow started for provider=%s", self._provider)

    async def stop(self) -> None:
        """Stop polling and shut down all dev environments."""
        await self._watcher.stop()
        self._parent_reconcile_running = False
        if self._parent_reconcile_task and not self._parent_reconcile_task.done():
            self._parent_reconcile_task.cancel()
            try:
                await self._parent_reconcile_task
            except asyncio.CancelledError:
                pass
        await self._dev_env.stop_all()
        logger.info("HumanGatedWorkflow stopped for provider=%s", self._provider)

    async def _parent_reconcile_loop(self) -> None:
        """Background loop calling :meth:`_reconcile_blocked_parents` on a
        fixed interval until :meth:`stop` cancels it."""
        while self._parent_reconcile_running:
            remaining = _PARENT_RECONCILE_INTERVAL_SECONDS
            while remaining > 0 and self._parent_reconcile_running:
                await asyncio.sleep(min(remaining, 5.0))
                remaining -= 5.0
            try:
                await self._reconcile_blocked_parents()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Parent-reconcile sweep failed: %s", exc, exc_info=True
                )

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
        self._events.subscribe("ticket.deleted", self._on_ticket_deleted)
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

        # Sub-tickets created by decompose_ticket already carry their own
        # acceptance criteria AND a `<!-- Sub-ticket of #N -->` parent marker
        # in the lifecycle record. Regenerating/overwriting that AC here would
        # DROP the marker (the child's board description uses a `## Acceptance
        # Criteria` heading that ACParser.extract doesn't recognise, so the
        # generic path below would regenerate from scratch), which in turn
        # breaks _parent_of() and leaves the parent stuck in BLOCKED forever
        # once its children finish. Leave a sub-ticket's AC untouched — but
        # still fall through to the first-sight recovery below so the child
        # can be auto-started like any other ready+assigned ticket.
        is_subticket = "Sub-ticket of #" in (record.acceptance_criteria or "")
        if not is_subticket:
            # If there's no Marcus AC block yet, generate one — but only for
            # a project a human has explicitly enabled Marcus for (see the
            # same gate in _start_ai_work). This is a Kanboard write
            # (add_comment + update_task) just like claiming a ticket, so a
            # disabled project must not get it either.
            existing_ac = ACParser.extract(description)
            if existing_ac is None:
                kanboard_project_id = await self._resolve_kanboard_project_id(
                    ticket_id
                )
                if kanboard_project_id is None or self._project_access.is_enabled(
                    kanboard_project_id
                ):
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
        has_assignee = bool(board_assignee) and board_assignee != "0"

        # Record the board's real assignee on first sight UNCONDITIONALLY —
        # not gated on the column being Ready/In Progress. A ticket assigned
        # while still in Todo (then later moved to Ready without touching
        # assignment again, since it's already assigned) would otherwise
        # never get an assignee recorded at all: BoardWatcher only emits
        # ticket.assigned on a CHANGE relative to its own snapshot, and that
        # snapshot's baseline already matches the board's assignee from this
        # exact moment, so no LATER diff event ever corrects the gap.
        # _is_unassigned()/_is_human_owner() (and so _next_worker_ticket's
        # hand-out gate) read this persisted field, not a live Kanboard
        # lookup — a permanently-empty assignee here silently and
        # indefinitely blocks the ticket from ever being handed to an
        # agent, even though the board has always shown a real owner.
        if has_assignee:
            try:
                self._lifecycle.set_assignee(
                    ticket_id, self._provider, board_assignee
                )
            except KeyError:
                pass
            record = self._lifecycle.get(ticket_id, self._provider) or record

        if has_assignee and board_status in (
            TaskStatus.READY.value,
            TaskStatus.IN_PROGRESS.value,
        ):
            if record.ai_agent_id is None:
                # Mirror the board column BEFORE attempting the start —
                # same reasoning as the column-move resume in
                # _on_status_changed. _start_ai_work can refuse for reasons
                # that are not this ticket's fault (disabled project, an
                # unmet dependency, every agent slot busy) and a record
                # left at TODO while the board already shows Ready/In
                # Progress is invisible to _next_worker_ticket forever:
                # ticket.new only ever fires once per ticket, so no later
                # event re-examines a column that never moves again.
                if record.state == TicketState.TODO:
                    try:
                        self._lifecycle.human_transition(
                            ticket_id,
                            self._provider,
                            TicketState.READY,
                            reason=(
                                "Ticket first seen already assigned and in "
                                "a workable column (restart recovery)"
                            ),
                        )
                    except (InvalidTransitionError, KeyError):
                        pass
                    record = (
                        self._lifecycle.get(ticket_id, self._provider) or record
                    )
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
        # Deliberately not narrowed to READY/IN_PROGRESS only: reassigning
        # a BLOCKED or WAITING_FOR_HUMAN ticket is exactly how a human
        # resumes one that got stuck (see TestDeadEndStateRecovery) —
        # _start_ai_work itself now guards against the one case that
        # resumption must NOT apply to (a decomposed parent; see its own
        # docstring).
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

    async def _reconcile_deleted_tickets(self) -> None:
        """Drop lifecycle records whose tickets no longer exist.

        BoardWatcher's disappeared-ticket check compares the board against
        snapshots built during THIS process, and those start empty — so a
        ticket deleted while Marcus was down is invisible to it. That
        record would otherwise outlive its ticket forever and keep being
        handed to agents.

        Each candidate is confirmed with a direct lookup: a ticket that
        still exists is kept, including one whose project is merely
        DISABLED (Marcus can see those, it just may not act on them). A
        lookup that FAILS is read as "still exists", so Kanboard being
        unreachable at boot can never wipe Marcus's state.
        """
        records = [
            r for r in self._lifecycle.all_records() if r.provider == self._provider
        ]
        if not records:
            return
        purged = 0
        for rec in records:
            try:
                task = await self._kanban.get_task_by_id(rec.ticket_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Could not check whether ticket %s still exists (%s) — "
                    "assuming it does",
                    rec.ticket_id,
                    exc,
                )
                continue
            if task is not None:
                continue
            try:
                await self._dev_env.stop(rec.ticket_id, self._provider)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Could not stop dev env for deleted ticket %s: %s",
                    rec.ticket_id,
                    exc,
                )
            if self._lifecycle.purge(rec.ticket_id, self._provider):
                purged += 1
        if purged:
            logger.info(
                "Startup reconcile: dropped %d ticket(s) that no longer "
                "exist on the board",
                purged,
            )

    async def _may_touch(self, ticket_id: str) -> bool:
        """Whether Marcus is allowed to ACT on this ticket.

        Marcus reads every Kanboard project so it has visibility into
        boards it is not allowed to work — to report "this project has
        ready tickets but isn't enabled", and so a deletion on any board is
        noticed. That means events arrive for disabled projects too, and
        the handlers behind them write to the board: comments, column
        moves, merges to main. Seeing a project must never turn into
        touching it.

        A ticket whose project cannot be resolved (non-Kanboard provider,
        RPC failure) is allowed through — the gate only applies where it
        can actually be evaluated.
        """
        pid = await self._resolve_kanboard_project_id(ticket_id)
        if pid is None or self._project_access.is_enabled(pid):
            return True
        logger.debug(
            "Ignoring event for ticket %s: Kanboard project %d is not "
            "enabled for Marcus (visible, but not actionable)",
            ticket_id,
            pid,
        )
        return False

    async def _on_ticket_deleted(self, event: Any) -> None:
        """Stop tracking a ticket that was deleted from the board.

        Marcus hands out work from lifecycle records, not from the board,
        so a record that outlives its ticket keeps being selected: an agent
        is told to work a ticket that no longer exists, and the slot it
        holds is never freed. Its preview container has to go too — every
        other teardown path keys off the ticket, so nothing else would ever
        stop it.

        The watcher only emits this once a direct lookup has confirmed the
        ticket is really gone, so a ticket that merely fell out of scope
        (its project was switched off) is never purged here.
        """
        ticket_id = event.data["ticket_id"]

        try:
            await self._dev_env.stop(ticket_id, self._provider)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not stop dev environment for deleted ticket %s: %s",
                ticket_id,
                exc,
            )

        if self._lifecycle.purge(ticket_id, self._provider):
            logger.info(
                "Ticket %s was deleted on the board — stopped tracking it",
                ticket_id,
            )
            # A freed slot should be filled straight away rather than
            # idling until some unrelated board event comes along.
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

        Claim invariant
        ----------------
        An AI agent may hold a claim on a ticket ONLY while it sits in the
        Ready or In Progress column — Ready covers the brief in-flight
        window before :meth:`_start_ai_work` finishes moving a fresh claim
        on to In Progress; in practice a claim is essentially always
        observed as In Progress. Every move to any other column (Todo,
        Blocked, Waiting for Human, Done, or a custom one) releases the
        claim here, before any other branch below runs — regardless of
        whether the move was made by a human or by Marcus itself (this
        handler fires the same way either way: from BoardWatcher's poll
        diff or the Kanboard webhook). This also drives the board's
        golden-ring highlight (see kanboard/plugins/MarcusDevEnv/Template/
        board/header.php), which is additionally filtered to state ==
        in_progress there, so it only lights while a ticket is genuinely
        being worked, not during this transient window. This is a
        backstop as much as a rule: most individual branches below already
        release the claim for their own reason (todo reset, done/merge,
        review-signal into waiting_for_human) — BLOCKED was the one gap,
        left claimed with no later event to release it. A human dragging
        an ACTIVE (claimed, In Progress) card backward to Ready is a
        distinct case handled explicitly further below, since this
        backstop alone would leave it exempt (Ready is an allowed claim
        column) — see the comment at that branch.
        """
        data = event.data
        ticket_id = data["ticket_id"]
        new_status = data.get("new_status", "")

        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            record = self._lifecycle.get_or_create(ticket_id, self._provider)

        if (
            new_status not in (TaskStatus.READY.value, TaskStatus.IN_PROGRESS.value)
            and record.ai_agent_id is not None
        ):
            try:
                self._lifecycle.release_ticket(ticket_id, self._provider)
            except KeyError:
                pass

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
                    new_status == TaskStatus.IN_PROGRESS.value
                    and record.state == TicketState.IN_PROGRESS
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
                elif (
                    new_status == TaskStatus.READY.value
                    and record.state == TicketState.IN_PROGRESS
                    and record.ai_agent_id is not None
                ):
                    # Human dragged an ACTIVE (claimed) card BACKWARD to
                    # Ready — a real, deliberate board change, not a
                    # poll-echo of the current state (unlike the branch
                    # above, new_status here does NOT match record.state).
                    # Treat it as "un-starting" the ticket: release the
                    # claim and mirror the lifecycle state back to Ready,
                    # so the golden ring clears immediately and the next
                    # worker pickup claims it fresh, instead of silently
                    # leaving Marcus's internal state stuck at In Progress
                    # while the board visibly shows Ready.
                    try:
                        self._lifecycle.release_ticket(ticket_id, self._provider)
                    except KeyError:
                        pass
                    try:
                        self._lifecycle.human_transition(
                            ticket_id,
                            self._provider,
                            TicketState.READY,
                            reason="Human moved an in-progress ticket back to ready",
                        )
                    except (InvalidTransitionError, KeyError):
                        pass
                else:
                    # Mirror the board column BEFORE attempting the start.
                    # The start can be refused for reasons that are not the
                    # ticket's fault — the project isn't enabled for Marcus,
                    # the project description has no tech stack, a dependency
                    # is unmet — and _next_worker_ticket only ever considers
                    # READY/IN_PROGRESS records. A record left at TODO while
                    # the board says Ready is therefore invisible to every
                    # worker FOREVER, including after the block is lifted:
                    # the column does not change again, so no further status
                    # event fires to re-examine it. Mirroring first keeps the
                    # ticket a valid candidate the moment it is unblocked.
                    if record.state == TicketState.TODO:
                        try:
                            self._lifecycle.human_transition(
                                ticket_id,
                                self._provider,
                                TicketState.READY,
                                reason=(
                                    "Human moved assigned ticket to a "
                                    "workable column"
                                ),
                            )
                        except (InvalidTransitionError, KeyError):
                            pass
                        record = (
                            self._lifecycle.get(ticket_id, self._provider)
                            or record
                        )
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
                # Record a sentinel blocker so the decompose-parent
                # heuristics in _check_parent_completion /
                # _reconcile_blocked_parents (which treat "no recognized
                # children AND no recorded blocked_by" as "must be a
                # decompose parent whose AC marker was lost") don't
                # mistake an ordinary, never-decomposed ticket a human
                # blocked for an unrelated reason as a decompose parent.
                # Without this, such a ticket that also happens to carry
                # a normal Kanboard "is blocked by" link to an unrelated
                # Done ticket would be wrongly auto-completed by the
                # link-fallback sweep — in AI-gate mode, marked DONE
                # outright with no branch ever merged and no acceptance
                # criteria ever verified. "human" never matches a real
                # ticket id, so get_records_blocked_by (which resumes a
                # ticket when its recorded blocker completes) can never
                # spuriously fire on it either.
                self._lifecycle.set_blocked_by(ticket_id, self._provider, "human")
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
        if not await self._may_touch(ticket_id):
            return
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return

        # A decomposed parent ticket is a tracking shell with no branch of
        # its own to merge — its children did the real work. Complete it
        # directly rather than falling into the merge path below, which
        # would attempt a git merge on a branch that doesn't exist.
        if self._children_of(ticket_id):
            await self._complete_parent_ticket(ticket_id, record)
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
            await self._maybe_complete_parent(ticket_id)
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

            # Clear any merge-conflict flag from a previous failed attempt
            # on this same ticket — it's resolved now.
            await self._clear_merge_conflict_flag(ticket_id)

            comment = CommentFormatter.merged(
                ticket_id=ticket_id,
                branch_name=branch_name,
                main_branch=main_branch,
            )
            await self._post_comment(ticket_id, comment)
            logger.info("Ticket %s done and merged to %s", ticket_id, main_branch)

            # This completion may unblock other tickets.
            await self._resume_tickets_blocked_by(ticket_id)
            # If this was a sub-ticket, its parent may now be fully complete.
            await self._maybe_complete_parent(ticket_id)

            # Agent is now free — pick up the next ticket in dependency order.
            await self._pickup_next_ticket()
            return True
        else:
            comment = CommentFormatter.merge_conflict(
                ticket_id=ticket_id,
                branch_name=branch_name,
                main_branch=main_branch,
            )
            await self._post_comment(ticket_id, comment)
            # Flag the card itself — visible on the board without opening
            # the ticket — so a card bouncing back to Ready/In Progress
            # isn't mistaken for a fresh, never-started ticket. Best-effort:
            # only a KanboardKanban-specific capability, never blocks the
            # rest of the recovery flow.
            if hasattr(self._kanban, "set_merge_conflict_flag"):
                try:
                    await self._kanban.set_merge_conflict_flag(
                        ticket_id, present=True
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Could not set merge-conflict flag for %s: %s",
                        ticket_id,
                        exc,
                    )
            # Send the ticket back to an AI agent to rebase and resolve the
            # conflict itself, rather than parking it for a human — this is
            # an implementation detail like any other (Invariant #2). The
            # old behavior left it IN_PROGRESS and *claimed* — permanently
            # leaking one parallel slot (a full deadlock at cap=1), since no
            # later event ever released it. Parking in READY removes it
            # from that trap AND keeps it in the available pool so the next
            # poll picks it straight back up (no re-merge loop, since the
            # merge itself is never retried without new commits).
            self._park_in_ready_for_rebase(
                ticket_id,
                reason="Merge to main failed; branch needs rebase and conflict resolution",
            )
            try:
                await self._kanban.move_task_to_column(ticket_id, "ready")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not move %s to ready after merge fail: %s",
                    ticket_id,
                    exc,
                )
            await self._pickup_next_ticket()
            return False

    async def _on_ticket_reopened(self, event: Any) -> None:
        """Handle a ticket being reopened — rebase branch on main and resume."""
        data = event.data
        ticket_id = data["ticket_id"]
        if not await self._may_touch(ticket_id):
            return
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return

        # Only a genuinely COMPLETED ticket can be "reopened". Kanboard's
        # task.open event also fires when openTask touches a board-closed
        # task whose lifecycle record never reached DONE (e.g. stale records
        # from before the drag-to-Done merge fix). Without this guard, that
        # event triggered a catastrophic feedback loop: reopen → release
        # claim → pickup re-claims → move column → openTask → task.open →
        # reopen … flooding Kanboard with RPCs until its SQLite locked
        # ("database is locked") and every real board update failed.
        if record.state != TicketState.DONE:
            logger.debug(
                "Ignoring reopen for %s: record state is %s, not DONE",
                ticket_id,
                record.state.value,
            )
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
        if not await self._may_touch(ticket_id):
            return
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

        # @marcus decompose → split this ticket into linked sub-tickets.
        if CommentParser.contains_command(body, "decompose"):
            children = await self.decompose_ticket(ticket_id)
            if not children:
                await self._post_comment(
                    ticket_id,
                    "I couldn't split this into independent sub-tickets — it "
                    "looks atomic (or no LLM is configured for decomposition).",
                )
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
        if not await self._may_touch(ticket_id):
            return
        new_ac = data.get("new_ac_text", "")
        new_hash = data.get("new_hash", "")

        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return

        # A human editing a sub-ticket's AC on the board sends AC text without
        # the invisible `<!-- Sub-ticket of #N -->` parent marker; re-append it
        # so _parent_of() keeps working. new_hash is left as the board's hash
        # of the VISIBLE AC, so change detection still compares like-for-like.
        import re

        marker = re.search(
            r"<!-- Sub-ticket of #\d+ -->", record.acceptance_criteria or ""
        )
        if marker and marker.group(0) not in new_ac:
            new_ac = f"{new_ac}\n{marker.group(0)}"

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
            #
            # EXCEPT for a decompose parent: it reached WAITING_FOR_HUMAN
            # via _check_parent_completion once all its children finished,
            # not via an agent's own signal_ready_for_review — it has no
            # branch and nothing an agent could implement in response to
            # an AC edit here (a human touching its description while
            # reviewing — even just re-saving it in Kanboard's editor —
            # is enough to trigger this). Resuming it to IN_PROGRESS would
            # wrongly bounce it out of the human's review queue and
            # re-claim an agent slot for work that doesn't exist, undoing
            # _check_parent_completion's parking for no reason a human
            # asked for.
            if record.state == TicketState.WAITING_FOR_HUMAN:
                if self._children_of(ticket_id):
                    logger.info(
                        "AC change detected on decompose parent %s while "
                        "awaiting review — leaving it in Waiting for Human",
                        ticket_id,
                    )
                    return
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
            "origin/<context.branch_name>`. If `git log` shows existing "
            "commits on this branch, this ticket was worked on before — "
            "review that history first and continue building on it, don't "
            "redo it from scratch.\n"
            "2. Implement every item in `context.acceptance_criteria` — make "
            "real code changes. After EACH change: `git commit` then "
            "`git push origin <context.branch_name>`. Push every commit (not "
            "just at the end) so your work is visible on the remote branch for "
            "review.\n"
            "3. Every ~10 seconds, call `marcus_work` again with the SAME "
            "`agent_id` and `ticket_id` and a `report` of the one thing you "
            "just did (a short sentence). If you can determine your account's "
            "subscription usage, also pass `usage={\"account\": \"<your account "
            "id/email>\", \"used\": <number>, \"limit\": <number or null>, "
            "\"unit\": \"<e.g. tokens>\"}` (null/omit `limit` for a self-hosted "
            "or unlimited model) so the human sees it on the ticket.\n"
            "4. When EVERY acceptance criterion is met, FIRST push your final "
            "commit, THEN call `marcus_work` with `report=\"DONE - <one-line "
            "summary>\"` (start the report with the word DONE). If you hit "
            "something only a human can resolve, call with `report=\"BLOCKED - "
            "<reason>\"`.\n"
            "DO NOT run dev servers, start Docker containers, or bind host "
            "ports (no `npm run dev`, `python -m http.server`, `docker run`, "
            "etc.). Your ONLY outputs are commits pushed to the branch. The "
            "human previews your pushed work through Marcus — you never run it."
        )

    def _classify_report_intent(self, report: str) -> str:
        """Classify a worker report into done / blocked / waiting / progress.

        Marcus instructs the worker to PREFIX a finishing report with ``DONE``
        and a blocker with ``BLOCKED`` (see :meth:`_worker_instructions`), and
        those explicit prefixes always win. But agents don't reliably use the
        exact prefix — one that reports "finished implementing everything" or
        "all acceptance criteria met" would otherwise be read as "keep going"
        and the ticket would never move to waiting-for-human. So a lenient
        fallback also recognizes common completion / blocked phrasings, guarded
        against negation ("not done yet", "isn't finished") so a partial update
        is never mistaken for completion.
        """
        head = report.strip().lower()

        # 1. Explicit prefixes (the documented contract) — highest priority.
        if head.startswith("done"):
            return "done"
        if head.startswith("blocked"):
            return "blocked"
        if head.startswith("waiting") or head.startswith("need human"):
            return "waiting"

        negated = any(
            n in head
            for n in ("not ", "n't", "no longer", "not yet", "still working")
        )

        # 2. Lenient completion phrasings (only when not negated).
        if not negated and any(
            k in head
            for k in (
                "all acceptance criteria",
                "ready for review",
                "finished implement",
                "implementation complete",
                "implementation is complete",
                "task complete",
                "work is complete",
                "work complete",
                "all done",
                "everything is done",
                "completed all",
                "i'm done",
                "i am done",
            )
        ):
            return "done"

        # 3. Lenient blocked / waiting phrasings.
        if not negated and ("i'm blocked" in head or "i am blocked" in head):
            return "blocked"
        if not negated and (
            "need human" in head
            or "waiting for human" in head
            or "need a human" in head
            or "human input" in head
        ):
            return "waiting"

        return "progress"

    async def _generate_testing_instructions(
        self,
        ticket_title: str,
        ticket_description: str,
        diff_text: str,
        ac_items: List[str],
    ) -> Optional[str]:
        """Generate step-by-step manual-testing instructions for a human.

        Posted alongside the "Ready for Review" comment (see
        :meth:`signal_ready_for_review`) so the human doesn't have to
        reverse-engineer HOW to exercise the change from the diff or the
        acceptance criteria themselves — told concretely what to click,
        type, or look at in the live preview, tailored to what THIS
        ticket actually changed (e.g. "open the Checkout page and
        confirm the Submit button now renders green") rather than a
        generic "review the code" instruction.

        Parameters
        ----------
        ticket_title : str
            Ticket title.
        ticket_description : str
            Ticket description/body.
        diff_text : str
            The ticket branch's diff against main (may be empty if it
            could not be fetched — degrades to the heuristic fallback).
        ac_items : List[str]
            Acceptance criteria items, used both as LLM context and as
            the heuristic fallback's own checklist.

        Returns
        -------
        Optional[str]
            Markdown (typically a numbered list), or ``None`` when
            there's nothing meaningful to say (no AC and no LLM
            configured, or an empty diff with no AC).
        """
        if self._llm_generate is None or not diff_text.strip():
            # No LLM configured, OR a diff that could not be fetched
            # (empty/whitespace-only) — asking the LLM to describe how
            # to test a change it cannot see produces an ungrounded (or
            # outright hallucinated) response. The AC-based heuristic is
            # strictly more reliable in that case, matching what this
            # method's own docstring already promised.
            return self._heuristic_testing_instructions(ac_items)

        truncated_diff = diff_text[:_MAX_TESTING_DIFF_CHARS]
        if len(diff_text) > _MAX_TESTING_DIFF_CHARS:
            truncated_diff += (
                f"\n\n[... diff truncated at {_MAX_TESTING_DIFF_CHARS} "
                "characters ...]"
            )
        ac_section = (
            "\n".join(f"- {item}" for item in ac_items)
            if ac_items
            else "(none recorded)"
        )
        prompt = textwrap.dedent(f"""
            You are writing instructions for a NON-technical human reviewer
            who will open a live preview of this change in their browser and
            needs to know exactly what to click, type, or look at to confirm
            it works.

            Ticket title: {ticket_title}
            Ticket description:
            {ticket_description}

            Acceptance criteria:
            {ac_section}

            Code diff:
            ```diff
            {truncated_diff}
            ```

            Write a short numbered list of CONCRETE manual testing steps a
            human can follow in the running preview app to verify this
            change — name the specific page/screen/element/button to look
            at or interact with, and what result to expect. Do NOT describe
            the code or say "review the code" — describe what to DO in the
            running software. Output ONLY the numbered list, nothing else.
        """).strip()
        try:
            result = await self._llm_generate(prompt)
            text = (result or "").strip()
            if not text:
                return self._heuristic_testing_instructions(ac_items)
            if len(text) > _MAX_TESTING_INSTRUCTIONS_CHARS:
                text = (
                    text[:_MAX_TESTING_INSTRUCTIONS_CHARS]
                    + f"\n\n[... truncated at {_MAX_TESTING_INSTRUCTIONS_CHARS} "
                    "characters ...]"
                )
            return text
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LLM testing-instructions generation failed, falling back "
                "to heuristic: %s",
                exc,
            )
            return self._heuristic_testing_instructions(ac_items)

    @staticmethod
    def _heuristic_testing_instructions(ac_items: List[str]) -> Optional[str]:
        """Rule-based fallback when no LLM is configured (or it failed).

        Reuses the acceptance criteria as the checklist of things to
        verify in the preview — the best available signal without an
        LLM to interpret the diff.
        """
        if not ac_items:
            return None
        lines = [
            "1. Open the live preview (see link below, or start it from "
            "the ticket sidebar / `@marcus start-dev-env`)."
        ]
        for i, item in enumerate(ac_items, start=2):
            lines.append(f"{i}. Verify: {item}")
        return "\n".join(lines)

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

    @staticmethod
    def _is_human_owner(assignee: Optional[str]) -> bool:
        """True if *assignee* is a human (set, not '0', not a worker id)."""
        return assignee not in (None, "", "0") and not str(
            assignee
        ).startswith("worker-")

    @staticmethod
    def _extract_json_obj(text: str) -> Dict[str, Any]:
        """Best-effort extract the first JSON object from an LLM response."""
        import json
        import re

        if not text:
            return {}
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            obj = json.loads(match.group(0))
            return obj if isinstance(obj, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _should_attempt_decompose(self, record: TicketRecord) -> bool:
        """Cheap gate before spending an LLM call: only large tickets.

        A ticket is a decomposition candidate when it has several acceptance
        criteria (a proxy for multiple deliverables). Sub-tickets (already
        created by a decomposition) are never re-decomposed.
        """
        if self._llm_generate is None:
            return False
        if "Sub-ticket of #" in (record.acceptance_criteria or ""):
            return False
        return len(self._get_ac_items(record)) >= 4

    async def _llm_decompose(
        self, title: str, description: str, acceptance_criteria: str
    ) -> List[Dict[str, str]]:
        """Ask the LLM to split a ticket into independent sub-tickets.

        Returns ``[]`` when the ticket is atomic/coupled or no LLM is wired —
        Marcus only decomposes when there are genuinely independent pieces.
        """
        if self._llm_generate is None:
            return []
        prompt = (
            "Split this software ticket into smaller INDEPENDENT sub-tickets "
            "that different agents can implement in parallel. If it is already "
            "small/atomic, or its parts are tightly coupled, return an empty "
            "list. Otherwise return 2-5 self-contained sub-tickets.\n\n"
            f"Title: {title}\nDescription:\n{description}\n"
            f"Acceptance criteria:\n{acceptance_criteria}\n\n"
            'Respond with ONLY JSON: {"subtasks": [{"title": "...", '
            '"description": "...", "acceptance_criteria": "- [ ] ..."}]}'
        )
        try:
            out = await self._llm_generate(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Decomposition LLM call failed: %s", exc)
            return []
        data = self._extract_json_obj(out or "")
        raw = data.get("subtasks")
        if not isinstance(raw, list):
            return []
        clean: List[Dict[str, str]] = []
        for s in raw:
            if isinstance(s, dict) and s.get("title"):
                clean.append(
                    {
                        "title": str(s["title"])[:200],
                        "description": str(s.get("description", ""))[:2000],
                        "acceptance_criteria": str(
                            s.get("acceptance_criteria", "")
                        )[:2000],
                    }
                )
        return clean if len(clean) >= 2 else []

    async def decompose_ticket(self, ticket_id: str) -> List[str]:
        """Split a ticket into linked child tickets that inherit its status.

        Creates each sub-ticket as a Kanboard task in the Ready column,
        inheriting the parent's human owner (so workers can pick them up
        independently), links it to the parent (``is a child of``), then parks
        the parent as BLOCKED (it completes once its children are done).

        Returns
        -------
        List[str]
            The created child ticket ids (empty if not decomposed).
        """
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None or record.state == TicketState.BLOCKED:
            return []
        create = getattr(self._kanban, "create_task", None)
        if create is None:
            return []

        title, description = ticket_id, ""
        parent_project_id: Optional[int] = None
        try:
            task = await self._kanban.get_task_by_id(ticket_id)
            if task:
                title = task.name or title
                description = task.description or ""
                raw = (task.source_context or {}).get("kanboard_task", {})
                pid_raw = raw.get("project_id")
                if pid_raw:
                    parent_project_id = int(pid_raw)
        except Exception as exc:  # noqa: BLE001
            # Fail CLOSED, not open: every gate below is
            # `if parent_project_id is not None and not <check>`, which a
            # bare `parent_project_id = None` here would silently skip —
            # bypassing both the project-enabled and decompose-enabled
            # checks. Worse, create_task's payload then omits
            # "project_id" entirely, so the provider falls back to its
            # own DEFAULT configured project: a transient RPC blip while
            # decomposing project B's ticket would silently create child
            # tickets on project A's board instead. Abort rather than
            # guess which project this ticket belongs to.
            logger.warning(
                "Decompose: could not resolve project for ticket %s (%s) — "
                "aborting rather than risk creating sub-tickets on the "
                "wrong project.",
                ticket_id,
                exc,
            )
            return []

        # Children inherit the parent's card color — the most visible way
        # to show "these belong together" on the board itself, since the
        # "is a child of" link is only visible once a card is opened.
        # Best-effort: only a KanboardKanban-specific capability, and a
        # lookup failure must never block decomposing the ticket.
        parent_color: Optional[str] = None
        get_color = getattr(self._kanban, "get_task_color", None)
        if get_color is not None:
            try:
                parent_color = await get_color(ticket_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Decompose: could not fetch color for %s: %s", ticket_id, exc
                )

        # Gate BEFORE the LLM call, not after. That call takes seconds, not
        # microseconds, and is the actual reason a caller's own gate check
        # can go stale before this method's writes happen — checking here,
        # right before either occurs, is what keeps the two in sync.
        if parent_project_id is not None and not self._project_access.is_enabled(
            parent_project_id
        ):
            logger.debug(
                "Refusing to decompose ticket %s: Kanboard project %d is "
                "not enabled for Marcus",
                ticket_id,
                parent_project_id,
            )
            return []

        # Same reasoning, separate switch: a human can disable decomposition
        # for a project (the "no ticket splitting" button) independently of
        # disabling the project entirely — checked whether this call came
        # from the automatic large-ticket path or the explicit
        # "@marcus decompose" comment command; both go through this method.
        if (
            parent_project_id is not None
            and not self._gate.get_effective_decompose_enabled(parent_project_id)
        ):
            logger.debug(
                "Refusing to decompose ticket %s: decomposition is disabled "
                "for Kanboard project %d",
                ticket_id,
                parent_project_id,
            )
            return []

        # Sub-tickets ALWAYS start in Ready, whatever column the parent sat
        # in. A column reflects who is working a card, not where it belongs
        # in the plan: a freshly created child has not been claimed by any
        # agent, so putting it in In Progress just because its parent was
        # there would advertise work nobody is doing. Ready is exactly the
        # "assigned and available to claim" state, which is what these are.
        child_column = "ready"

        subs = await self._llm_decompose(
            title, description, record.acceptance_criteria
        )
        if not subs:
            return []

        # Re-verify once more, right before any write happens. The pre-call
        # gate above only catches a call that was already pointless — it
        # cannot see a disable that happens WHILE the LLM call is in
        # flight. Without this, a project disabled during that call would
        # still get child tickets created, assigned, linked, and its
        # parent parked as BLOCKED, regardless of whether those children
        # ever reach an agent (orchestrate_work re-filters them before
        # handing anything out, but the writes themselves would already be
        # done).
        if parent_project_id is not None and not self._project_access.is_enabled(
            parent_project_id
        ):
            logger.debug(
                "Refusing to write sub-tickets for %s: Kanboard project %d "
                "was disabled during decomposition",
                ticket_id,
                parent_project_id,
            )
            return []
        if (
            parent_project_id is not None
            and not self._gate.get_effective_decompose_enabled(parent_project_id)
        ):
            logger.debug(
                "Refusing to write sub-tickets for %s: decomposition was "
                "disabled for Kanboard project %d during decomposition",
                ticket_id,
                parent_project_id,
            )
            return []

        link = getattr(self._kanban, "create_task_link", None)
        create_subtask = getattr(self._kanban, "create_subtask", None)
        owner = record.assignee if self._is_human_owner(record.assignee) else None
        child_ids: List[str] = []
        for s in subs:
            child_desc = s["description"]
            if s.get("acceptance_criteria"):
                child_desc += (
                    f"\n\n## Acceptance Criteria\n{s['acceptance_criteria']}"
                )
            child_desc += f"\n\n_Sub-ticket of #{ticket_id}._"
            payload: Dict[str, Any] = {
                "name": s["title"],
                "description": child_desc,
            }
            if parent_project_id is not None:
                # Children must land on the PARENT's board — the provider
                # defaults to the configured project, which may differ.
                payload["project_id"] = parent_project_id
            if parent_color is not None:
                payload["color_id"] = parent_color
            try:
                child = await create(payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not create sub-ticket: %s", exc)
                continue
            child_id = str(getattr(child, "id", "") or "")
            if not child_id:
                continue
            # Lifecycle record inheriting the parent's status: Ready + owner,
            # and a "Sub-ticket of #" marker so it's never re-decomposed.
            child_ac = (
                (s.get("acceptance_criteria", "") or "")
                + f"\n<!-- Sub-ticket of #{ticket_id} -->"
            )
            child_record = self._lifecycle.get_or_create(
                child_id, self._provider, acceptance_criteria=child_ac
            )
            # get_or_create() only applies acceptance_criteria on FIRST
            # creation — a no-op if the record already exists. That race
            # is real: create(payload) above already made this ticket
            # visible on the board via at least one RPC round trip before
            # this line runs, so a CONCURRENT BoardWatcher poll (its own
            # background loop, or another agent's on-demand marcus_work
            # triggering one) can see the new ticket and fire ticket.new
            # first — _on_ticket_new's own get_or_create (no AC) then
            # wins the race, silently dropping this marker. Without it,
            # _parent_of/_children_of can never recognize this child, so
            # the parent stays BLOCKED forever even once every child is
            # actually Done. Patch it in explicitly if that happened.
            if "Sub-ticket of #" not in (child_record.acceptance_criteria or ""):
                logger.warning(
                    "Sub-ticket %s's lifecycle record already existed "
                    "(lost the race with a concurrent board poll) — "
                    "patching in its parent marker now",
                    child_id,
                )
                self._lifecycle.update_acceptance_criteria(
                    child_id,
                    self._provider,
                    child_ac,
                    ACChangeDetector.hash_ac(child_ac),
                )
            # Check the result: move_task_to_column returns False (no
            # exception) when the board has no matching column or the task
            # actually lives in a different Kanboard project than expected
            # — exactly how a child silently stayed in Kanboard's default
            # "Todo" column instead of visibly inheriting Ready from its
            # parent. Surface that at WARNING so it's diagnosable instead
            # of vanishing at debug level (matches the parent's own
            # move-to-blocked handling below).
            child_moved = False
            try:
                child_moved = await self._kanban.move_task_to_column(
                    child_id, child_column
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not move sub-ticket %s to %r: %s",
                    child_id,
                    child_column,
                    exc,
                )
            if not child_moved:
                logger.warning(
                    "Sub-ticket %s did NOT move to the %r column (stayed "
                    "in Kanboard's default column). It is READY in Marcus's "
                    "lifecycle regardless.",
                    child_id,
                    child_column,
                )
            if owner:
                try:
                    self._lifecycle.set_assignee(child_id, self._provider, owner)
                except KeyError:
                    pass
                # Also assign it ON THE BOARD. Inheriting the owner into
                # Marcus's own record only is not enough: the Kanboard card
                # shows no assignee, so a human sees unowned sub-tickets,
                # and anything that re-derives state from the board (a
                # restarted Marcus meeting an existing board) loses the
                # owner entirely — at which point the children stop being
                # handout candidates, since _next_worker_ticket requires a
                # human owner.
                assign = getattr(self._kanban, "assign_task", None)
                if assign is not None:
                    try:
                        if not await assign(child_id, owner):
                            logger.warning(
                                "Could not assign sub-ticket %s to %r on the "
                                "board; it is owned by %r in Marcus's "
                                "lifecycle regardless.",
                                child_id,
                                owner,
                                owner,
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Could not assign sub-ticket %s to %r: %s",
                            child_id,
                            owner,
                            exc,
                        )
            try:
                self._lifecycle.human_transition(
                    child_id,
                    self._provider,
                    TicketState.READY,
                    reason=f"Inherited status from parent #{ticket_id}",
                )
            except (InvalidTransitionError, KeyError):
                pass
            if link is not None:
                try:
                    # The PARENT depends on the child: "parent is blocked by
                    # child" (Kanboard link type 3). Kanboard auto-adds the
                    # reciprocal "child blocks parent" on the child. This is
                    # the correct direction — the decomposed ticket waits on
                    # its sub-tickets, not the other way around — and makes
                    # the board/sidebar show the parent depending on each
                    # child. (The old `link(child, parent, 6)` "is a child
                    # of" was classified as depends_on, so the CHILD wrongly
                    # showed as blocked by the parent.)
                    await link(ticket_id, child_id, 3)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Could not link sub-ticket: %s", exc)
            if create_subtask is not None:
                try:
                    # A native Kanboard Subtask on the PARENT, separate from
                    # the functional dependency link above — gives the
                    # parent's own task view a dedicated "Subtasks" section
                    # listing every child clearly. The "#<child_id> " prefix
                    # lets _maybe_complete_parent find and update the right
                    # entry later without storing a subtask id anywhere.
                    await create_subtask(ticket_id, f"#{child_id} {s['title']}")
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Could not create subtask entry: %s", exc)
            child_ids.append(child_id)

        if child_ids:
            await self._post_comment(
                ticket_id,
                "🧩 **Decomposed into sub-tickets** so agents can work them in "
                f"parallel: {', '.join('#' + c for c in child_ids)}. This "
                "ticket completes once its sub-tickets are done.",
            )
            try:
                self._lifecycle.release_ticket(ticket_id, self._provider)
            except KeyError:
                pass
            try:
                self._lifecycle.human_transition(
                    ticket_id,
                    self._provider,
                    TicketState.BLOCKED,
                    reason="Decomposed into sub-tickets",
                )
            except (InvalidTransitionError, KeyError):
                pass
            # Move the decomposed parent's card to the Blocked column. Check
            # the result: move_task_to_column returns False (no exception)
            # when the board has no matching column, which is exactly how a
            # parent silently stayed in Ready. Surface that at WARNING so it's
            # diagnosable instead of vanishing at debug level. Track whether
            # the call itself raised separately from a clean False return —
            # otherwise a raised exception (already logged with the real
            # error) ALSO falls through to the "no such column?" diagnostic
            # below, which is actively misleading for e.g. an RPC timeout.
            moved = False
            raised = False
            try:
                moved = await self._kanban.move_task_to_column(ticket_id, "blocked")
            except Exception as exc:  # noqa: BLE001
                raised = True
                logger.warning(
                    "Could not move decomposed parent %s to blocked column: %s",
                    ticket_id,
                    exc,
                )
            if not moved and not raised:
                logger.warning(
                    "Decomposed parent %s did NOT move to the 'Blocked' column "
                    "(does this project's board have a column named 'Blocked'?). "
                    "It is BLOCKED in Marcus's lifecycle regardless.",
                    ticket_id,
                )
        return child_ids

    async def _rescan_boards(self) -> None:
        """Refresh lifecycle state from every enabled board (best-effort).

        Runs before handing a worker its next ticket so a ticket that has
        just become assigned+Ready is picked up on this poll rather than
        after the next BoardWatcher tick. Delegates to the watcher so the
        board→Marcus translation stays in one place (and inherits its
        serialisation, so this can never race the background loop).

        Never fatal: a failed board read leaves Marcus handing out the work
        it already knows about, which is strictly better than refusing to
        hand out anything.
        """
        watcher = getattr(self, "_watcher", None)
        if watcher is None:
            return
        try:
            await watcher.poll_once(max_age=_BOARD_RESCAN_MAX_AGE)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not re-read the board before handing out work "
                "(continuing with known tickets): %s",
                exc,
            )

    async def _scope_summary(self) -> str:
        """Name the boards Marcus is actually watching.

        "No tickets are ready" is indistinguishable from "Marcus is not
        looking at your board at all" — which is the state a fresh install
        is in, since every project starts disabled. Saying which boards are
        in scope makes that difference visible without the human having to
        go read logs.
        """
        lister = getattr(self._project_access, "enabled_project_ids", None)
        if lister is None:
            return ""
        try:
            pids = list(lister())
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not list enabled projects: %s", exc)
            return ""
        if not pids:
            return (
                "NOTE: Marcus is not enabled for ANY Kanboard project, so it "
                "is not reading any board — no ticket can ever be handed out. "
                "Open the board you want worked on and switch 'Marcus: OFF' "
                "to ON in its header."
            )
        labels = [await self._project_display_name(pid) for pid in pids]
        return f"Marcus is watching Kanboard project(s): {', '.join(labels)}."

    async def _withheld_ticket_reasons(self) -> List[str]:
        """Explain assigned tickets that a worker cannot be handed.

        ``marcus_work`` otherwise answers "No tickets are ready right now"
        whenever a ticket is ready but Marcus is not allowed to start it —
        which is actively misleading, and leaves the human nothing to act
        on, because the refusal is only an INFO log line inside the
        container. Each reason names the ticket and what to do about it.

        Returns
        -------
        List[str]
            Human-readable reasons, empty when nothing is being withheld.
        """
        paused: List[str] = []
        blocked: List[str] = []
        unassigned: List[str] = []
        # Kanboard project id → tickets held up by that project's toggle.
        # Grouped, because one toggle unblocks all of them: listing the
        # same instruction once per ticket buries the single action the
        # human actually has to take.
        by_project: Dict[int, List[str]] = {}

        for rec in self._lifecycle.all_records():
            if rec.provider != self._provider:
                continue
            if not self._is_human_owner(rec.assignee):
                # A Ready-but-unassigned ticket in an ENABLED project is a
                # distinct, common reason marcus_work returns no_work — the
                # generic "no tickets are ready" message doesn't mention
                # this ticket at all, since it isn't a hand-out candidate
                # to begin with (not "withheld", simply not eligible). An
                # agent with no visibility into that has to go dig through
                # other tool calls to find it and guess why — in practice
                # leading to a wrong diagnosis (mistaking the unrelated
                # per-ticket gate_mode setting for the real cause).
                # Skipped for a disabled project: the actionable
                # instruction there is "enable the project", already
                # covered below for assigned tickets in the same project.
                if rec.state == TicketState.READY:
                    pid = await self._resolve_kanboard_project_id(rec.ticket_id)
                    if pid is None or self._project_access.is_enabled(pid):
                        unassigned.append(f"#{rec.ticket_id}")
                continue
            if rec.state == TicketState.WAITING_FOR_HUMAN:
                # Same project-enablement check as every other branch here
                # (unassigned-Ready above, by_project below) — a paused
                # ticket in a project Marcus isn't enabled for is that
                # project's state, not something to hand to an agent
                # working a different (or no) project. Regression: this
                # branch used to append unconditionally.
                pid = await self._resolve_kanboard_project_id(rec.ticket_id)
                if pid is None or self._project_access.is_enabled(pid):
                    paused.append(f"#{rec.ticket_id}")
                continue
            if rec.state == TicketState.BLOCKED:
                pid = await self._resolve_kanboard_project_id(rec.ticket_id)
                if pid is None or self._project_access.is_enabled(pid):
                    blocked.append(
                        f"#{rec.ticket_id} (blocked by "
                        f"{rec.blocked_by or 'another ticket'})"
                    )
                continue
            if rec.state not in (TicketState.READY, TicketState.IN_PROGRESS):
                continue
            pid = await self._resolve_kanboard_project_id(rec.ticket_id)
            if pid is not None and not self._project_access.is_enabled(pid):
                by_project.setdefault(pid, []).append(f"#{rec.ticket_id}")

        reasons: List[str] = []
        for pid in sorted(by_project):
            tickets = by_project[pid]
            label = await self._project_display_name(pid)
            reasons.append(
                f"{len(tickets)} ready ticket(s) — {', '.join(tickets)} — "
                f"are in Kanboard project {label}, which is not enabled for "
                "Marcus. Open THAT project's board in Kanboard and switch "
                "'Marcus: OFF' to ON in the board header. Enabling a "
                "different project does not cover this one: the setting is "
                "per project."
            )
        if paused:
            reasons.append(
                f"{len(paused)} ticket(s) — {', '.join(paused)} — are paused "
                "waiting for human input; see each ticket's comments on the "
                "board."
            )
        if blocked:
            reasons.append(
                f"{len(blocked)} ticket(s) are blocked by dependencies: "
                + ", ".join(blocked)
                + "."
            )
        if unassigned:
            reasons.append(
                f"{len(unassigned)} ticket(s) — {', '.join(unassigned)} — "
                "are Ready but have no assignee yet. Assign one (any human) "
                "in Kanboard to make it available to an agent."
            )
        return reasons

    async def _next_worker_ticket(self) -> Optional[str]:
        """Return the next human-readied ticket to hand a worker, or ``None``.

        HUMAN-TRIGGERED selection: Marcus only hands out tickets that are
        **assigned to a human — ANYONE, not necessarily you — AND moved to
        Ready** (the existing gate). A ticket is workable when it is
        READY/IN_PROGRESS, has any human owner (assignee set and not the
        Kanboard "0" no-owner sentinel), and is not already held by a worker
        (an internal ``marcus-`` slot claim from the human-gated auto-start is
        fine — the worker adopts it).

        Candidates whose Kanboard project is not enabled for Marcus are
        skipped (not just refused): _start_ai_work would refuse them too,
        but since this method returns only ONE ticket — the lowest id — a
        disabled-project ticket sitting first in sort order would otherwise
        starve every agent forever, retried every ~10s while legitimately
        available tickets in enabled projects never get reached.
        """
        def _key(rec: TicketRecord) -> int:
            try:
                return int(rec.ticket_id)
            except ValueError:
                return abs(hash(rec.ticket_id))

        def _held_by_worker(rec: TicketRecord) -> bool:
            # A claim by anything other than an internal ``marcus-`` slot is
            # a worker that already owns the ticket — don't re-hand it.
            return rec.ai_agent_id is not None and not str(
                rec.ai_agent_id
            ).startswith("marcus-")

        candidates = [
            r
            for r in self._lifecycle.all_records()
            if r.provider == self._provider
            and r.state in (TicketState.READY, TicketState.IN_PROGRESS)
            and self._is_human_owner(r.assignee)
            and not _held_by_worker(r)
        ]
        candidates.sort(key=_key)
        for candidate in candidates:
            pid = await self._resolve_kanboard_project_id(candidate.ticket_id)
            if pid is not None and not self._project_access.is_enabled(pid):
                logger.debug(
                    "Skipping ticket %s for worker hand-out: project %d is "
                    "not enabled for Marcus",
                    candidate.ticket_id,
                    pid,
                )
                continue
            return candidate.ticket_id
        return None

    async def _reclaim_stuck_ticket(self, agent_id: str) -> Optional[str]:
        """Recover an IN_PROGRESS ticket whose claiming agent has gone
        silent for too long, reassigning its claim to *agent_id*.

        An agent that loses track of its own agent_id — a fresh session, a
        compacted context, anything that makes it call ``marcus_work``
        without the id it was given — leaves its old ticket claimed and
        IN_PROGRESS forever: nothing else releases that claim except the
        agent's own done/blocked/waiting-for-human signal, and a claimed
        ticket is invisible to :meth:`_next_worker_ticket`. Called from
        :meth:`orchestrate_work` BEFORE it looks for a fresh ticket, so
        recovering abandoned work always takes priority over starting new
        work — the ticket is handed back out with its full context, same
        as a matched agent_id resuming its own ticket, so the new session
        continues exactly where the old one left off.

        Staleness is judged by the more recent of two signals: the
        progress-activity heartbeat (:meth:`_mark_progress_activity`) and
        the record's own ``updated_at`` (covers a ticket claimed but never
        once reported on). The single MOST stale eligible ticket is
        reclaimed, if any exceeds ``_STUCK_AGENT_TIMEOUT_SECONDS``.

        Parameters
        ----------
        agent_id : str
            The agent id to reassign the stuck ticket's claim to.

        Returns
        -------
        Optional[str]
            The reclaimed ticket id, or ``None`` if nothing is stuck.
        """
        now_wall = datetime.now(timezone.utc)
        now_mono = time.monotonic()
        stale: List[Tuple[float, str, str]] = []
        for record in self._lifecycle.all_records():
            if record.provider != self._provider:
                continue
            if record.state != TicketState.IN_PROGRESS:
                continue
            held_by = str(record.ai_agent_id or "")
            # Unclaimed, an internal auto-start slot (not a real worker), or
            # already this very agent's own ticket (handled by the normal
            # "re-send context" path, not a reclaim) — none are stuck.
            if not held_by or held_by.startswith("marcus-") or held_by == agent_id:
                continue
            key = f"{self._provider}:{record.ticket_id}"
            heartbeat_ts = self._progress_activity.get(key)
            heartbeat_age = (
                (now_mono - heartbeat_ts) if heartbeat_ts is not None else math.inf
            )
            claim_age = (now_wall - record.updated_at).total_seconds()
            age = min(heartbeat_age, claim_age)
            if age > _STUCK_AGENT_TIMEOUT_SECONDS:
                stale.append((age, record.ticket_id, held_by))

        stale.sort(reverse=True)  # most stale first
        for _, ticket_id, expected_holder in stale:
            if not await self._may_touch(ticket_id):
                continue
            # Re-verify the ticket is STILL held by the same stale agent
            # right before acting — the await above (_may_touch) is a real
            # yield point, so a concurrent _reclaim_stuck_ticket call for a
            # DIFFERENT new agent_id may have already reclaimed this exact
            # ticket while we were suspended. Without this check, our own
            # unconditional release_ticket() would silently steal back a
            # claim a concurrent caller just legitimately won, and both
            # callers would believe they own the ticket even though the
            # lifecycle store can only actually record one of them.
            current = self._lifecycle.get(ticket_id, self._provider)
            if current is None or str(current.ai_agent_id or "") != expected_holder:
                continue
            try:
                self._lifecycle.release_ticket(ticket_id, self._provider)
                claimed = self._lifecycle.claim_ticket(
                    ticket_id, self._provider, agent_id
                )
            except KeyError:
                continue
            if not claimed:
                continue
            self._mark_progress_activity(ticket_id)
            minutes = int(_STUCK_AGENT_TIMEOUT_SECONDS // 60)
            await self._post_comment(
                ticket_id,
                f"🔄 **Reassigned** — the previous session went quiet for "
                f"over {minutes} minutes. A new agent session is resuming "
                "this ticket from where it was left.",
            )
            logger.info(
                "Ticket %s reclaimed from a stale agent session; "
                "reassigned to %s",
                ticket_id,
                agent_id,
            )
            return ticket_id
        return None

    async def orchestrate_work(
        self,
        agent_id: Optional[str] = None,
        report: Optional[str] = None,
        ticket_id: Optional[str] = None,
        usage: Optional[Dict[str, Any]] = None,
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
        usage : Optional[Dict[str, Any]]
            The worker's self-reported subscription/account usage, e.g.
            ``{"account": "team@x.com", "used": 12.5, "limit": 50, "unit": "M"}``.
            Stored per account and surfaced on the tickets that account's agents
            are working (``limit`` None/absent → unlimited, e.g. self-hosted).

        Returns
        -------
        Dict[str, Any]
            ``{status, agent_id, ticket_id?, context?, message}`` where
            ``status`` is one of ``assigned``/``working``/``continue``/
            ``done``/``blocked``/``waiting``/``no_work``.
        """
        # Cap the length: agent_id is caller-supplied, used as an in-memory
        # dict key, and rendered in the Kanboard UI.
        agent_id = (agent_id or "").strip()[:_ACCOUNT_ID_MAX] or (
            f"worker-{uuid.uuid4().hex[:8]}"
        )
        # Every poll — with or without a report, with or without a ticket —
        # means this agent is connected right now. Record its self-reported
        # subscription/account usage too, if it sent any.
        self._mark_agent_seen(agent_id)
        if usage is not None:
            self._record_agent_usage(agent_id, usage)
        # ticket_id is an unauthenticated echo of what Marcus itself
        # returned in a prior response (see the docstring above), never an
        # authority — an agent reporting DONE/BLOCKED against some other
        # agent's in-progress ticket (LLM mix-up, stale/hallucinated value
        # after context compaction, or a bare copy-paste) must not be able
        # to act on it. get_agent_ticket() is the lifecycle store's actual
        # record of what this agent_id holds, so it — not the caller's
        # claim — decides which ticket "active" refers to.
        own_ticket = self._lifecycle.get_agent_ticket(agent_id)
        requested = (ticket_id or "").strip()
        if requested and own_ticket and requested != own_ticket:
            logger.warning(
                "agent %s supplied ticket_id=%s but actually holds %s; "
                "using its real claim instead",
                agent_id,
                requested,
                own_ticket,
            )
        active = own_ticket

        # 1. A report on the worker's active ticket → summarize + act.
        if report and report.strip() and active:
            summary = await self._summarize_report(report)
            await self._post_comment(active, f"🤖 **Worker progress:** {summary}")
            # A report of any kind means the agent is alive on this ticket —
            # stamp its heartbeat. Terminal intents below (done/blocked/waiting)
            # go through signal_*/set_* which clear it again immediately.
            self._mark_progress_activity(active)
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

        # 3. No active ticket → first check whether some OTHER ticket has
        # gone stuck (claimed by a now-unreachable agent session — see
        # _reclaim_stuck_ticket) and needs recovering. This takes priority
        # over starting fresh work: unfinished work must never sit
        # abandoned in favor of a brand new ticket just because the agent
        # that was doing it lost track of its own agent_id.
        stuck_id = await self._reclaim_stuck_ticket(agent_id)
        if stuck_id is not None:
            ctx = await self.get_work_context(stuck_id)
            return {
                "status": "working",
                "agent_id": agent_id,
                "ticket_id": stuck_id,
                "context": ctx,
                "message": (
                    "Reassigned: a previous session on this ticket went "
                    "quiet. " + self._worker_instructions()
                ),
            }

        # Defensive re-check: the awaits above (_reclaim_stuck_ticket) give
        # a concurrent orchestrate_work call for this SAME agent_id a
        # window to have claimed a ticket in the meantime — claim_ticket
        # itself now refuses to give one agent a second, different claim,
        # but resuming that ticket here (rather than racing to claim
        # another) is what keeps this call correct rather than merely safe.
        active = self._lifecycle.get_agent_ticket(agent_id)
        if active:
            ctx = await self.get_work_context(active)
            return {
                "status": "working",
                "agent_id": agent_id,
                "ticket_id": active,
                "context": ctx,
                "message": self._worker_instructions(),
            }

        # Re-read the enabled boards first. _next_worker_ticket selects from
        # lifecycle records, which are otherwise only refreshed by
        # BoardWatcher's own timer (30s by default) and by webhooks — so an
        # agent polling every ~10s could wait a full watcher interval before
        # a ticket a human just moved to Ready became visible, and where
        # webhooks aren't reaching Marcus this is the only thing that closes
        # that gap at all. Coalesced: several agents polling at once share
        # one board read rather than each triggering their own.
        await self._rescan_boards()
        next_id = await self._next_worker_ticket()
        if next_id is None:
            withheld = await self._withheld_ticket_reasons()
            if withheld:
                return {
                    "status": "no_work",
                    "agent_id": agent_id,
                    "message": (
                        "No tickets can be handed out right now. Assigned "
                        "tickets are being withheld for these reasons:\n"
                        + "\n".join(f"- {r}" for r in withheld)
                        + "\nRelay the above to the human VERBATIM, including "
                        "the project name and number — do not paraphrase it "
                        "as a generic 'enable Marcus'. Then call marcus_work "
                        "again in ~10s."
                    ),
                }
            return {
                "status": "no_work",
                "agent_id": agent_id,
                "message": (
                    "No tickets are ready right now. "
                    + await self._scope_summary()
                    + " Call marcus_work again in ~10s."
                ),
            }

        # Re-verify right before acting on it. _next_worker_ticket's own
        # is_enabled check can be stale by the time we get here:
        # decompose_ticket below awaits an LLM call — seconds, not
        # microseconds — during which a human has a real window to disable
        # the project. The reclaim branch further down (a ticket already
        # IN_PROGRESS under an internal 'marcus-' slot claim) skips
        # _start_ai_work — and its own re-check — entirely, so without this
        # a disabled-project ticket already claimed by an internal slot
        # could still be handed to a worker.
        if not await self._may_touch(next_id):
            return {
                "status": "no_work",
                "agent_id": agent_id,
                "message": (
                    "No tickets are ready right now. Call marcus_work "
                    "again in ~10s."
                ),
            }

        rec = self._lifecycle.get(next_id, self._provider)
        # Auto-decompose a large ticket into sub-tickets before handing it
        # out, so agents work independent pieces in parallel. The parent is
        # parked (BLOCKED); re-picking finds the newly-created child tickets.
        if rec is not None and self._should_attempt_decompose(rec):
            children = await self.decompose_ticket(next_id)
            if children:
                return await self.orchestrate_work(agent_id=agent_id)

            # decompose_ticket declined to split it (LLM said "atomic") —
            # but that call is an LLM round-trip, seconds not microseconds,
            # so the _may_touch check made before attempting it can be
            # stale by now. Re-verify before falling through to claim/start
            # the ORIGINAL ticket below, or a disable that happened during
            # that call would go unnoticed.
            if not await self._may_touch(next_id):
                return {
                    "status": "no_work",
                    "agent_id": agent_id,
                    "message": (
                        "No tickets are ready right now. Call marcus_work "
                        "again in ~10s."
                    ),
                }

        if rec is not None:
            # Adopt the human-readied ticket under the WORKER's claim. The
            # human stays the assignee (owner); the worker becomes the one
            # doing the work. Release any internal-slot claim first.
            if rec.ai_agent_id and str(rec.ai_agent_id).startswith("marcus-"):
                try:
                    self._lifecycle.release_ticket(next_id, self._provider)
                except KeyError:
                    pass
                rec = self._lifecycle.get(next_id, self._provider) or rec
            if rec.state == TicketState.READY:
                # Not started yet → full start (branch, IN_PROGRESS, comment).
                await self._start_ai_work(next_id, rec, claim_as=agent_id)
            else:
                # Already IN_PROGRESS (human-gated auto-start prepared it) →
                # just take over the claim; the branch already exists.
                try:
                    self._lifecycle.claim_ticket(
                        next_id, self._provider, agent_id
                    )
                except KeyError:
                    pass

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

    def _mark_progress_activity(self, ticket_id: str) -> None:
        """Stamp *now* as this ticket's last agent-progress report.

        Called wherever an agent reports it is working (a progress comment,
        a marcus_work report, a branch push). Drives the board's "actively
        worked" golden highlight — see :meth:`get_working_ticket_ids`.

        Because a progress report or a branch push is itself live contact
        from the ticket's agent, this also refreshes that agent's
        "connected" heartbeat. Without it, a real agent that commits and
        pushes (its highlight refreshed via the Gitea webhook) but polls
        ``marcus_work`` only occasionally would light the golden ring yet
        show 0 connected / 0 working agents. Internal ``marcus-<slot>``
        auto-start reservations are NOT live agents, so they are excluded
        (an internal reservation must never be counted as a connected
        agent — see :meth:`get_active_agent_ids`).
        """
        self._progress_activity[f"{self._provider}:{ticket_id}"] = time.monotonic()

        record = self._lifecycle.get(ticket_id, self._provider)
        if record is not None:
            agent_id = str(record.ai_agent_id or "")
            if agent_id and not agent_id.startswith("marcus-"):
                self._mark_agent_seen(agent_id)

    def _clear_progress_activity(self, ticket_id: str) -> None:
        """Drop a ticket's heartbeat so its highlight clears at once.

        Called on the terminal outcomes an agent reports — done, blocked, or
        waiting-for-human — so the human sees the ring vanish immediately
        rather than waiting for the activity window to lapse.
        """
        self._progress_activity.pop(f"{self._provider}:{ticket_id}", None)

    def get_working_ticket_ids(
        self, window_seconds: float = _WORKING_WINDOW_SECONDS
    ) -> List[str]:
        """Return ticket ids an agent has reported progress on very recently.

        "Recently" means within ``window_seconds`` of now — i.e. an agent is
        actively working the ticket RIGHT NOW. This is intentionally derived
        only from real agent activity (progress reports), never from ticket
        state, so it stays accurate even if a state-management bug leaves a
        ticket stuck in a column. Stale entries are pruned as they are found.

        Parameters
        ----------
        window_seconds : float
            Maximum age of the last report for a ticket to still count as
            actively worked.

        Returns
        -------
        List[str]
            Ticket ids (for this workflow's provider) currently being worked.
        """
        now = time.monotonic()
        working: List[str] = []
        for key, ts in list(self._progress_activity.items()):
            if now - ts <= window_seconds:
                working.append(key.split(":", 1)[1])
            else:
                self._progress_activity.pop(key, None)
        return working

    # ------------------------------------------------------------------
    # Agent presence (connected vs active) + reported account usage
    # ------------------------------------------------------------------

    def _mark_agent_seen(self, agent_id: str) -> None:
        """Stamp *now* as this agent's last poll — it is connected right now."""
        if agent_id:
            self._agent_seen[agent_id] = time.monotonic()

    def get_connected_agent_ids(
        self, window_seconds: float = _AGENT_POLL_WINDOW
    ) -> List[str]:
        """Return agent ids that have polled ``marcus_work`` within the window.

        This is the "connected" set: every agent asking Marcus for work counts,
        whether or not it currently holds a ticket — an idle-but-polling agent
        is still connected. Stale entries are pruned.
        """
        now = time.monotonic()
        connected: List[str] = []
        for agent_id, ts in list(self._agent_seen.items()):
            if now - ts <= window_seconds:
                connected.append(agent_id)
            else:
                self._agent_seen.pop(agent_id, None)
                self._agent_account.pop(agent_id, None)
        return connected

    def get_active_agent_ids(self) -> List[str]:
        """Return agent ids that are ACTIVELY working a ticket right now.

        Active = a CONNECTED agent (recently polling ``marcus_work``) that holds
        a claimed ticket whose progress heartbeat is live (see
        :meth:`get_working_ticket_ids`) — the strict "connected AND claimed AND
        working" definition. Requiring connectedness makes active a subset of
        connected and naturally excludes internal ``marcus-<slot>`` claims
        (human-gated auto-start reservations), which never poll and so are not
        connected agents.
        """
        working = set(self.get_working_ticket_ids())
        connected = set(self.get_connected_agent_ids())
        active = {
            str(r.ai_agent_id)
            for r in self._lifecycle.all_records()
            if r.provider == self._provider
            and r.ticket_id in working
            and r.ai_agent_id
            and str(r.ai_agent_id) in connected
        }
        return list(active)

    def _record_agent_usage(self, agent_id: str, usage: Any) -> None:
        """Store an agent's self-reported subscription/account usage (sanitized).

        ``usage`` is a dict the agent passes to marcus_work, e.g.
        ``{"account": "team@x.com", "used": 12.5, "limit": 50, "unit": "M tok"}``.
        Stored per ACCOUNT so agents sharing one subscription share one figure;
        ``limit`` None/absent means unlimited (e.g. a self-hosted model).

        The payload is UNTRUSTED (any connected agent can send anything) and is
        sanitized here before it ever reaches state or the UI: values are coerced
        to safe JSON scalars (:func:`_safe_usage_scalar`), the account id is
        length-capped, and the number of distinct accounts is bounded (evicting
        the least-recently-updated) so a rogue agent can't exhaust memory. A
        malformed report is ignored, never raised.
        """
        if not agent_id or not isinstance(usage, dict):
            return
        raw_account = usage.get("account")
        account = (
            str(raw_account)[:_ACCOUNT_ID_MAX]
            if raw_account not in (None, "")
            else agent_id
        )
        # Bound distinct accounts held in memory; evict the stalest when full.
        if (
            account not in self._account_usage
            and len(self._account_usage) >= _MAX_TRACKED_ACCOUNTS
        ):
            oldest = min(
                self._account_usage,
                key=lambda k: self._account_usage[k].get("ts", 0.0),
            )
            self._account_usage.pop(oldest, None)
        self._agent_account[agent_id] = account
        self._account_usage[account] = {
            "used": _safe_usage_scalar(usage.get("used")),
            "limit": _safe_usage_scalar(usage.get("limit")),  # None → unlimited
            "unit": _safe_usage_scalar(usage.get("unit")),
            "ts": time.monotonic(),
        }

    def usage_for_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Return the account-level usage figure for *agent_id*, or ``None``.

        Two agents on the same reported ``account`` resolve to the SAME figure.
        """
        account = self._agent_account.get(agent_id)
        if account is None:
            return None
        return self._account_usage.get(account)

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

        # The agent is actively working — refresh its liveness heartbeat.
        self._mark_progress_activity(ticket_id)

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

    async def handle_branch_push(
        self, branch_name: str, commit_messages: List[str]
    ) -> bool:
        """Announce commits pushed to a ticket branch, from the Gitea webhook.

        Wired to the Gitea push webhook so the board shows REAL, code-driven
        progress the instant an agent pushes — independent of whether the
        agent bothers to self-report via ``marcus_work``. A push is also
        activity, so this refreshes the ticket's liveness heartbeat (keeping
        the "actively worked" highlight lit) and lets a human review the
        pushed code on the branch whenever they want.

        Also syncs Marcus's local clone of this branch (see
        :meth:`_sync_branch_for_ticket`) BEFORE the webhook's own dev-env
        refresh runs (:class:`~src.core.gitea_webhook_receiver.
        GiteaWebhookReceiver` calls this ``on_commits`` hook first, then
        ``DevEnvironmentManager.refresh_by_branch``) — an already-open
        preview's ``git fetch`` inside its container reaches Marcus's local
        clone, not Gitea directly, so without this the hot-reload the push
        was supposed to trigger would silently reset to the same stale
        commit it already had.

        Parameters
        ----------
        branch_name : str
            The pushed branch, e.g. ``ticket/kanboard/5``. Matched against a
            lifecycle record's stored ``branch_name`` (exact, by construction
            — never re-parsed back into a ticket id).
        commit_messages : List[str]
            Commit messages in the push (newest last). An empty list (e.g.
            Marcus's own branch-create push, which carries no new commits) is
            a no-op.

        Returns
        -------
        bool
            ``True`` if a matching, still-open ticket was found and a comment
            was posted; ``False`` otherwise.
        """
        if not commit_messages:
            return False
        record = next(
            (
                r
                for r in self._lifecycle.all_records()
                if r.provider == self._provider and r.branch_name == branch_name
            ),
            None,
        )
        # Only comment while the ticket is still being worked / reviewed — a
        # push to an already-DONE ticket's branch is noise.
        if record is None or record.state == TicketState.DONE:
            return False

        ticket_id = record.ticket_id
        # A push is agent activity — keep the "actively worked" ring lit.
        self._mark_progress_activity(ticket_id)

        await self._sync_branch_for_ticket(ticket_id, branch_name)

        count = len(commit_messages)
        shown = [m.splitlines()[0][:100] for m in commit_messages[-5:] if m.strip()]
        lines = "\n".join(f"- {line}" for line in shown)
        more = "" if count <= 5 else f"\n_…and {count - 5} earlier commit(s)._"
        comment = (
            f"🔨 **{count} new commit{'s' if count != 1 else ''} pushed** to "
            f"`{branch_name}`:\n{lines}{more}\n\n"
            "Open the branch from this ticket's **Marcus Code** link to review "
            "the code."
        )
        return await self._post_comment(ticket_id, comment)

    async def _move_column_with_retry(
        self,
        ticket_id: str,
        column_name: str,
        *,
        attempts: int = _COLUMN_MOVE_MAX_ATTEMPTS,
        delay: float = _COLUMN_MOVE_RETRY_DELAY_SECONDS,
    ) -> bool:
        """Move a ticket's kanban card to *column_name*, retrying and
        surfacing a persistent failure on the ticket itself.

        ``KanboardKanban.move_task_to_column`` frequently fails CLEANLY —
        it returns ``False`` with no exception when its own resolve→move→
        verify sequence doesn't stick (a transient SQLite lock during the
        move or the verifying re-fetch is the common case; see that
        method's own docstring). Every existing call site in this file
        that doesn't already check the return value is exactly the bug
        this fixes: the caller's ``try/except`` only ever catches a
        RAISED exception, so a clean ``False`` return is invisible, and
        even a raised-and-caught exception was previously just logged and
        forgotten. Either way, Marcus's own lifecycle state moves on (the
        ticket is correctly marked done/waiting/whatever in Marcus) while
        the visible Kanboard card silently stays wherever it was — which
        reads to a human as "the agent never actually finished," when it
        did.

        A short retry survives the common transient case outright. If
        every attempt still fails, a warning comment is posted on the
        ticket so a human isn't left staring at a stale card with no
        explanation — the alternative (this method's predecessor
        behaviour) left literally nothing on the ticket to explain the
        mismatch.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        column_name : str
            Target Kanboard column name (case-insensitive).
        attempts : int
            Total attempts before giving up.
        delay : float
            Seconds to wait between attempts.

        Returns
        -------
        bool
            ``True`` once the move is verified to have taken effect,
            ``False`` if every attempt failed (a warning comment was
            posted in that case).
        """
        last_exc: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            moved = False
            try:
                moved = await self._kanban.move_task_to_column(
                    ticket_id, column_name
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                moved = False
            if moved:
                return True
            if attempt < attempts:
                await asyncio.sleep(delay)

        if last_exc is not None:
            logger.warning(
                "Could not move %s to '%s' after %d attempt(s): %s",
                ticket_id,
                column_name,
                attempts,
                last_exc,
            )
        else:
            logger.warning(
                "Ticket %s did NOT move to the '%s' column after %d "
                "attempt(s) (does this project's board have a column "
                "named %r?). Marcus's own lifecycle state is correct "
                "regardless — only the visible Kanboard card may be "
                "stale.",
                ticket_id,
                column_name,
                attempts,
                column_name,
            )
        try:
            await self._post_comment(
                ticket_id,
                f"⚠️ Marcus could not move this card to the "
                f"**{column_name.title()}** column after {attempts} "
                "attempts (a Kanboard hiccup). This ticket's actual "
                "status is correct in Marcus even though the card here "
                "may look stale — refresh the board, or drag the card "
                "manually if it still doesn't update.",
            )
        except Exception:  # noqa: BLE001
            pass
        return False

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

        # Genuine done signal → the agent is no longer working this ticket.
        # Clear its liveness heartbeat so the board highlight drops at once.
        self._clear_progress_activity(ticket_id)

        # Record when the AI agent finished — once, here, regardless of
        # which gate the ticket goes through next (see
        # CommentFormatter.ai_work_finished's docstring for why this is a
        # comment rather than Kanboard's native "Completed" date field).
        # Best-effort: a post failure must not block the completion signal
        # itself.
        try:
            await self._post_comment(
                ticket_id,
                CommentFormatter.ai_work_finished(
                    ticket_id, datetime.now(timezone.utc)
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not post work-finished comment for ticket %s: %s",
                ticket_id,
                exc,
            )

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

        # Best-effort: neither of these must ever block the review signal
        # itself (see the recoverability note above) — a transient
        # failure here just means the comment omits testing instructions,
        # not that the ticket fails to reach Waiting for Human. Skipped
        # entirely when no LLM is configured: _generate_testing_instructions
        # falls straight to its AC-based heuristic in that case and never
        # reads diff_text/ticket_title/ticket_description, so fetching
        # them would just be a wasted git diff fetch (network I/O) and a
        # Kanboard RPC call on every single review signal.
        diff_text = ""
        ticket_title, ticket_description = ticket_id, ""
        if self._llm_generate is not None:
            try:
                diff_text = await branch_mgr.get_branch_diff(record.branch_name)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Ticket %s: could not fetch diff for testing instructions: %s",
                    ticket_id,
                    exc,
                )
            try:
                task = await self._kanban.get_task_by_id(ticket_id)
                if task:
                    ticket_title = task.name or ticket_title
                    ticket_description = task.description or ""
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Ticket %s: could not fetch task details for testing "
                    "instructions: %s",
                    ticket_id,
                    exc,
                )
        testing_instructions = await self._generate_testing_instructions(
            ticket_title, ticket_description, diff_text, ac_items
        )

        comment = CommentFormatter.ready_for_review(
            ticket_id=ticket_id,
            branch_name=record.branch_name,
            ac_items=ac_items,
            dev_env_url=dev_url,
            commit_count=len(commits),
            testing_instructions=testing_instructions,
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

        await self._move_column_with_retry(ticket_id, "waiting for human")

        # Reaching WAITING_FOR_HUMAN means the agent just resubmitted its
        # work for review — the actual merge attempt happens LATER, when
        # a human accepts it (_on_ticket_closed), not here. Without this,
        # a ticket that had a conflict, got fixed, and was resubmitted
        # would sit in "waiting for human" still showing the stale tag.
        await self._clear_merge_conflict_flag(ticket_id)

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

        # Human gate: the ticket genuinely pauses for the human, so the agent
        # is no longer working it — clear its liveness heartbeat.
        self._clear_progress_activity(ticket_id)

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

        await self._move_column_with_retry(ticket_id, "waiting for human")

        await self._clear_merge_conflict_flag(ticket_id)

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

        # Blocked → the agent has stopped working this ticket; clear its
        # liveness heartbeat so the board highlight drops at once.
        self._clear_progress_activity(ticket_id)

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

    # ------------------------------------------------------------------
    # Dependency gating + parent (sub-ticket) auto-completion
    # ------------------------------------------------------------------

    @staticmethod
    def _parent_of(record: TicketRecord) -> Optional[str]:
        """Return the parent ticket id for a sub-ticket, or ``None``.

        Reads the ``<!-- Sub-ticket of #<parent> -->`` marker that
        :meth:`decompose_ticket` embeds in a child's acceptance criteria.
        """
        import re

        m = re.search(
            r"<!-- Sub-ticket of #(\d+) -->", record.acceptance_criteria or ""
        )
        return m.group(1) if m else None

    def _children_of(self, parent_id: str) -> List[TicketRecord]:
        """Return the sub-ticket records created for *parent_id*."""
        marker = f"<!-- Sub-ticket of #{parent_id} -->"
        return [
            r
            for r in self._lifecycle.all_records()
            if r.provider == self._provider
            and marker in (r.acceptance_criteria or "")
        ]

    async def _dependencies_satisfied(
        self, ticket_id: str
    ) -> Tuple[bool, List[str]]:
        """Return (all deps done?, [unmet dep ids]) for a ticket.

        A dependency is any ticket this one ``depends_on`` (Kanboard "is
        blocked by" / "depends on" links). A dependency is satisfied only
        when its lifecycle record is ``DONE``. The ticket's OWN parent is
        excluded — Kanboard lumps "is a child of" into depends_on, and a
        sub-ticket must never wait on its parent (the parent completes when
        its children do → deadlock). Fail-open on any Kanboard error so a
        transient outage can't wedge every start.
        """
        get_links = getattr(self._kanban, "get_task_links", None)
        if get_links is None:
            return True, []
        try:
            links = await get_links(ticket_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Dependency check: links fetch failed: %s", exc)
            return True, []

        record = self._lifecycle.get(ticket_id, self._provider)
        parent_id = self._parent_of(record) if record else None

        unmet: List[str] = []
        for dep in links.get("depends_on", []):
            dep_id = str(dep.get("task_id", "")).strip()
            if not dep_id or dep_id == parent_id:
                continue
            dep_rec = self._lifecycle.get(dep_id, self._provider)
            if dep_rec is None or dep_rec.state != TicketState.DONE:
                unmet.append(dep_id)
        return (len(unmet) == 0), unmet

    async def _block_on_dependencies(
        self, ticket_id: str, unmet: List[str]
    ) -> None:
        """Park a ticket in BLOCKED until its dependencies are done+merged.

        Records the blockers so :meth:`_resume_tickets_blocked_by` moves the
        ticket back to In Progress the moment the LAST dependency completes.
        """
        blockers = ", ".join("#" + d for d in unmet)
        try:
            self._lifecycle.release_ticket(ticket_id, self._provider)
        except KeyError:
            pass
        try:
            self._lifecycle.human_transition(
                ticket_id,
                self._provider,
                TicketState.BLOCKED,
                reason=f"Waiting on dependencies: {blockers}",
            )
        except (InvalidTransitionError, KeyError):
            pass
        try:
            self._lifecycle.set_blocked_by(ticket_id, self._provider, blockers)
        except KeyError:
            pass
        try:
            await self._kanban.move_task_to_column(ticket_id, "blocked")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not move %s to blocked: %s", ticket_id, exc)
        await self._post_comment(
            ticket_id,
            f"⛔ **Waiting on {blockers}** to be done and merged before this "
            "ticket can start. It will resume automatically once they're done.",
        )
        logger.info("Ticket %s blocked on dependencies: %s", ticket_id, blockers)

    async def _maybe_complete_parent(self, child_id: str) -> None:
        """Complete a parent ticket once ALL its sub-tickets are DONE.

        **Human gate (default)**: moves the parent to WAITING_FOR_HUMAN —
        the parent has no branch of its own to merge or verify, so a
        human reviews the completed children and marks the parent Done
        themselves (see :meth:`_complete_parent_ticket`, wired into
        :meth:`_on_ticket_closed`), the same as any other ticket a human
        closes.

        **AI gate**: skips the human review step and marks the parent
        Done directly via :meth:`_complete_parent_ticket`. No AI-verify
        step runs here even if the project has one configured — each
        child already went through its OWN gate/verification
        individually before being merged, so the parent's completion is
        just aggregating already-verified work, not new code to review
        (the parent has no branch/diff of its own for a verifier to look
        at in the first place).
        """
        child = self._lifecycle.get(child_id, self._provider)
        if child is None:
            return
        parent_id = self._parent_of(child)
        if parent_id is None:
            return

        if child.state == TicketState.DONE:
            # Reflect THIS child's completion on the parent's native
            # Subtasks section right away — not only once every sibling
            # is also done, so the list fills in as work actually finishes.
            mark_subtask_done = getattr(self._kanban, "mark_subtask_done", None)
            if mark_subtask_done is not None:
                try:
                    await mark_subtask_done(parent_id, f"#{child_id} ")
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "Could not sync subtask status for %s: %s", child_id, exc
                    )

        await self._check_parent_completion(parent_id)

    async def _children_done_via_links(self, parent_id: str) -> bool:
        """Cross-check via Kanboard's native internal links whether every
        ticket *parent_id* "is blocked by" currently sits in a Done-like
        column, independent of Marcus's own AC-marker bookkeeping.

        decompose_ticket links each child unconditionally
        (``create_task_link(parent, child, 3)`` — "parent is blocked by
        child"), regardless of whether that child's separate
        "Sub-ticket of #N" AC marker (what :meth:`_children_of` actually
        matches on) landed correctly. That marker write lost a race once
        in production (a concurrent board poll's own bare
        ``get_or_create()`` won and silently dropped it — fixed at the
        source in :meth:`decompose_ticket`), which made
        :meth:`_children_of` return nothing for an affected parent even
        though its children were genuinely done and the board's own link
        data still showed the relationship correctly. This is the
        self-healing fallback :meth:`_check_parent_completion` uses when
        the marker-based check finds no children at all.

        Parameters
        ----------
        parent_id : str
            Ticket identifier.

        Returns
        -------
        bool
            ``True`` only if there is at least one "is blocked by" link
            AND every linked ticket's current column normalizes to
            ``TaskStatus.DONE``. ``False`` on no links, an RPC failure,
            or when the provider doesn't support link queries.
        """
        get_links = getattr(self._kanban, "get_task_links", None)
        normalize = getattr(self._kanban, "normalize_status", None)
        if get_links is None or normalize is None:
            return False
        try:
            links = await get_links(parent_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Could not fetch task links for %s: %s", parent_id, exc
            )
            return False
        depends_on = links.get("depends_on") or []
        if not depends_on:
            return False
        all_done = all(
            normalize(entry.get("column")) == TaskStatus.DONE
            for entry in depends_on
        )
        if all_done:
            logger.info(
                "Parent %s: no children recognized via AC marker, but "
                "Kanboard's own internal links show all %d linked "
                "ticket(s) Done — completing via link fallback",
                parent_id,
                len(depends_on),
            )
        return all_done

    async def _check_parent_completion(self, parent_id: str) -> None:
        """Complete *parent_id* if it's BLOCKED and ALL its children are DONE.

        Shared by two callers: :meth:`_maybe_complete_parent` (the
        event-driven path, triggered when a specific child ticket
        closes) and :meth:`_reconcile_blocked_parents` (the periodic
        safety-net sweep — see :data:`_PARENT_RECONCILE_INTERVAL_SECONDS`
        for why the event-driven path alone isn't sufficient). No-op if
        the parent doesn't exist, is already DONE/WAITING_FOR_HUMAN, has
        no children, or any child isn't DONE yet.

        Unlike :meth:`_on_ticket_closed` (this method's sibling entry
        point into :meth:`_complete_parent_ticket`), neither of this
        method's own callers gate on project enablement first — a
        completed CHILD ticket can trigger this for a parent whose
        project a human disabled in the meantime (in-progress work is
        deliberately not force-interrupted, per
        ``ProjectAccessSettingManager.set_project_enabled``'s docstring),
        and the periodic sweep in :meth:`_reconcile_blocked_parents` has
        no per-event trigger to gate at all. So the check happens here
        instead — see :meth:`_may_touch`'s own docstring: "seeing a
        project must never turn into touching it".
        """
        if not await self._may_touch(parent_id):
            return
        parent = self._lifecycle.get(parent_id, self._provider)
        if parent is None or parent.state in (
            TicketState.DONE,
            TicketState.WAITING_FOR_HUMAN,
        ):
            return
        children = self._children_of(parent_id)
        children_done = bool(children) and all(
            c.state == TicketState.DONE for c in children
        )
        if not children_done:
            # Fallback: cross-check Kanboard's own "is blocked by" internal
            # links (created unconditionally by decompose_ticket via
            # create_task_link, independent of the AC-marker Marcus's OWN
            # _children_of() relies on). Only worth the extra RPC when the
            # marker-based check found NOTHING — a parent with at least one
            # recognized child is trusted as-is (partial marker loss is
            # closed at the source in decompose_ticket now), and a BLOCKED
            # ticket with a recorded blocker (parent.blocked_by) is an
            # ordinary dependency block, not a decompose parent at all —
            # _resume_tickets_blocked_by already owns that case correctly.
            if children or parent.blocked_by:
                return
            children_done = await self._children_done_via_links(parent_id)
            if not children_done:
                return

        gate = await self._get_effective_gate(parent_id)
        if gate == "ai":
            await self._complete_parent_ticket(parent_id, parent)
            return

        self._park_in_waiting_for_human(
            parent_id, reason="All sub-tickets complete; awaiting human review"
        )
        # A parent never merges its own branch, so it can't get a
        # merge-conflict tag from ITS OWN work — but it can still carry a
        # STALE one: a ticket that already failed a merge (tag set, sent
        # back to Ready for the agent to rebase — see
        # _park_in_ready_for_rebase) can be decomposed from there via
        # "@marcus decompose" instead of being resubmitted, becoming a
        # parent that still shows the old tag. Every other path into
        # Waiting-for-Human already clears it; this one must too.
        await self._clear_merge_conflict_flag(parent_id)
        # Check the result: move_task_to_column returns False (no exception)
        # when the board has no matching column or the task actually lives
        # in a different Kanboard project than expected — exactly how the
        # parent's lifecycle record could already say WAITING_FOR_HUMAN
        # while its board card silently stays in Blocked forever (nothing
        # re-checks a ticket whose column hasn't changed again). Surface
        # that at WARNING so it's diagnosable, matching the same check on
        # the parent's earlier move to Blocked in decompose_ticket. Track
        # whether the call raised separately from a clean False return —
        # otherwise an exception (already logged with the real error) ALSO
        # falls through to the "no such column?" diagnostic below, which is
        # actively misleading for e.g. an RPC timeout.
        moved = False
        raised = False
        try:
            moved = await self._kanban.move_task_to_column(
                parent_id, "waiting for human"
            )
        except Exception as exc:  # noqa: BLE001
            raised = True
            logger.warning(
                "Could not move parent %s to waiting-for-human: %s", parent_id, exc
            )
        if not moved and not raised:
            logger.warning(
                "Parent %s did NOT move to the 'Waiting for Human' column "
                "(does this project's board have a column named 'Waiting "
                "for Human'?). It is WAITING_FOR_HUMAN in Marcus's "
                "lifecycle regardless.",
                parent_id,
            )
        await self._post_comment(
            parent_id,
            "✅ **All sub-tickets are complete** — ready for your review. "
            "Move this ticket to Done once you're satisfied.",
        )
        logger.info("Parent %s ready for human review (all children done)", parent_id)

    async def _reconcile_blocked_parents(self) -> None:
        """Safety-net sweep: complete any BLOCKED parent whose children
        are ALL already DONE, regardless of whether the event-driven
        trigger (:meth:`_maybe_complete_parent`, called when a child
        closes) ever fired for it.

        That event-driven path can be missed entirely — a dropped
        webhook, a Marcus restart landing between the last child's
        completion and the parent check running, or any other gap —
        silently leaving a parent stuck in Blocked forever even though
        every child is done. Called once at startup and then on a fixed
        interval (see :data:`_PARENT_RECONCILE_INTERVAL_SECONDS`) so
        such a ticket is caught within one sweep regardless of cause.

        Also covers a parent whose children are entirely unrecognized by
        the AC-marker matching :meth:`_children_of` uses (see
        :meth:`_children_done_via_links`'s docstring) — :meth:`_check_
        parent_completion` falls back to Kanboard's own internal links
        for those. A BLOCKED ticket that has a recorded blocker
        (``record.blocked_by``) and no recognized children is an
        ordinary dependency block (:meth:`_resume_tickets_blocked_by`
        already owns that case), not a decompose parent — skipped here
        to avoid an RPC per sweep for every such ticket.
        """
        for record in list(self._lifecycle.all_records()):
            if record.provider != self._provider:
                continue
            if record.state != TicketState.BLOCKED:
                continue
            if not self._children_of(record.ticket_id) and record.blocked_by:
                continue
            try:
                await self._check_parent_completion(record.ticket_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Reconcile: could not check parent %s: %s",
                    record.ticket_id,
                    exc,
                )

    async def _complete_parent_ticket(
        self, parent_id: str, record: TicketRecord
    ) -> None:
        """Mark a decomposed parent ticket DONE directly.

        A parent is a tracking shell with no branch of its own — its
        children did the real work — so closing it must never attempt a
        git merge: there is nothing to merge, and a failed merge on an
        empty branch would incorrectly trigger the rebase-recovery flow
        meant for tickets that actually have conflicting commits. Called
        from two places: :meth:`_on_ticket_closed` (instead of
        :meth:`_merge_ticket_to_main`) when a human approves a parent
        already parked in Waiting for Human, or drags it straight to
        Done; and directly from :meth:`_maybe_complete_parent` when the
        parent's effective gate is ``"ai"``, skipping the human-review
        parking step entirely once all children are done.

        Parameters
        ----------
        parent_id : str
            Parent ticket identifier.
        record : TicketRecord
            The parent's current lifecycle record.
        """
        if record.state == TicketState.DONE:
            return
        try:
            self._lifecycle.release_ticket(parent_id, self._provider)
        except KeyError:
            pass
        try:
            self._lifecycle.human_transition(
                parent_id,
                self._provider,
                TicketState.DONE,
                reason="Parent ticket closed",
            )
        except (InvalidTransitionError, KeyError):
            pass
        try:
            self._lifecycle.set_merged(parent_id, self._provider)
        except KeyError:
            pass
        try:
            await self._kanban.move_task_to_column(parent_id, "done")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not move parent %s to done: %s", parent_id, exc)
        await self._post_comment(
            parent_id,
            "✅ **Parent ticket complete** — all sub-tickets are merged and "
            "this ticket is done.",
        )
        logger.info("Parent %s completed", parent_id)
        await self._resume_tickets_blocked_by(parent_id)

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
        return self._branch_for_repo_path(repo_path)

    def branch_manager_for_repo(self, repo_path: Optional[str]) -> BranchManager:
        """Public accessor for the per-repo :class:`BranchManager` cache.

        Thin wrapper over :meth:`_branch_for_repo_path`, exposed so
        external callers (e.g. ``ProjectCloneWorkflow``, which needs to
        seed a cloned in-flight ticket's branch from its baseline
        ticket's branch) don't have to reach into a private attribute to
        get the manager bound to a project's local clone.

        Parameters
        ----------
        repo_path : Optional[str]
            The project's local clone path (from a
            :class:`~src.workflows.project_sync_workflow.ProjectSyncWorkflow`
            mapping's ``local_repo_path``), or ``None``.

        Returns
        -------
        BranchManager
            See :meth:`_branch_for_repo_path`.
        """
        return self._branch_for_repo_path(repo_path)

    def _branch_for_repo_path(self, repo_path: Optional[str]) -> BranchManager:
        """Return a cached BranchManager bound to *repo_path*.

        Split out from :meth:`_branch_for_ticket` so callers that already
        resolved the ticket's repo path (e.g. ``start_dev_environment``) can
        reuse it WITHOUT re-running the project→repo lookup (which can trigger
        an on-demand Gitea provisioning). ``None`` → the constructor's fallback
        manager (deployments without project sync, and unit-test doubles).
        """
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

    async def _sync_branch_for_ticket(
        self, ticket_id: str, branch_name: str
    ) -> Optional[str]:
        """Pull *ticket_id*'s branch from the remote into Marcus's local clone.

        Marcus's own local clone is what a preview container's ``git fetch
        origin`` reaches (``origin`` there is a bind-mount of THIS clone,
        not Gitea directly — see :meth:`~src.core.dev_environment.
        DevEnvironmentManager.refresh`'s docstring). It only advances when
        something explicitly fetches from the real Gitea remote — an agent
        pushing new commits does not touch it. Without this call first,
        both :meth:`start_dev_environment` (opening a fresh preview) and a
        push-triggered :meth:`~src.core.dev_environment.
        DevEnvironmentManager.refresh` (an already-open preview's hot
        reload) would reset the container to whatever stale commit this
        clone happened to be on, silently showing no change at all.

        Best-effort: a failed sync is logged, never raised — the caller
        proceeds regardless (a stale preview is better than none).

        Parameters
        ----------
        ticket_id : str
            Ticket identifier, used to resolve which repo this branch lives
            in via the ticket's Kanboard project.
        branch_name : str
            Branch to sync from the remote.

        Returns
        -------
        Optional[str]
            The resolved local repo path (or ``None``), so a caller that
            also needs it (e.g. :meth:`start_dev_environment`) doesn't have
            to re-run the project→repo lookup, which can re-trigger repo
            provisioning.
        """
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
            branch_mgr = self._branch_for_repo_path(repo_path)
            await branch_mgr.sync_branch(branch_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not sync branch %s for %s: %s", branch_name, ticket_id, exc
            )

        return repo_path

    async def sync_main_branch_for_project(
        self, kanboard_project_id: int
    ) -> Optional[str]:
        """Pull a project's main branch from the remote into Marcus's local clone.

        The project_id-keyed analog of :meth:`_sync_branch_for_ticket`, for
        the project-level "main branch preview" — the caller already knows
        the project (there's no ticket to resolve one from), so this skips
        straight to :meth:`_resolve_project_repo_mapping` instead of first
        looking up a ticket's kanboard project id.

        Best-effort, same as :meth:`_sync_branch_for_ticket`: a failed sync
        is logged, never raised — the caller proceeds regardless (a stale
        preview is better than none).

        Parameters
        ----------
        kanboard_project_id : int
            The project whose main branch should be synced.

        Returns
        -------
        Optional[str]
            The resolved local repo path (or ``None``), so the caller
            (the main-branch preview route) doesn't redo the project→repo
            lookup, which can trigger on-demand repo provisioning.
        """
        mapping = await self._resolve_project_repo_mapping(kanboard_project_id)
        repo_path = mapping.get("local_repo_path") if mapping else None

        try:
            branch_mgr = self._branch_for_repo_path(repo_path)
            main_branch = getattr(branch_mgr.config, "main_branch", "main")
            await branch_mgr.sync_branch(main_branch)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not sync main branch for project %d: %s",
                kanboard_project_id,
                exc,
            )

        return repo_path

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

        # Pull the agent's latest pushed commits into Marcus's local clone
        # FIRST, so the preview reflects the ticket's REMOTE branch (the
        # committed-and-pushed work), not whatever stale state the local
        # clone held. The container is then cloned from this freshly-synced
        # clone. repo_path comes back from the same call so the project→repo
        # lookup (which can re-trigger repo provisioning) doesn't run twice.
        repo_path: Optional[str] = None
        if record.branch_name:
            repo_path = await self._sync_branch_for_ticket(
                ticket_id, record.branch_name
            )

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
        walks ``TODO → READY → IN_PROGRESS → WAITING_FOR_HUMAN`` (BLOCKED
        joins the path via IN_PROGRESS too — a decomposed parent whose last
        child just finished sits in BLOCKED) as far as the state machine
        allows. Used to take a ticket out of the *available* pool
        (``READY``/``IN_PROGRESS``) so it awaits a human without being
        re-selected by :meth:`_pickup_next_ticket` in a loop — e.g. a missing
        project description, or all of a parent's sub-tickets completing.
        Also frees the agent slot.

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
            TicketState.BLOCKED: TicketState.IN_PROGRESS,
            TicketState.IN_PROGRESS: TicketState.WAITING_FOR_HUMAN,
        }
        # At most four hops to climb from TODO/BLOCKED to WAITING_FOR_HUMAN.
        for _ in range(len(next_state)):
            cur = self._lifecycle.get(ticket_id, self._provider)
            if cur is None or cur.state == TicketState.WAITING_FOR_HUMAN:
                break
            target = next_state.get(cur.state)
            if target is None:
                # REOPENED / DONE — not on the WFH path. Leaving the
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

    def _park_in_ready_for_rebase(self, ticket_id: str, reason: str) -> None:
        """Move a ticket back to READY and release its claim, for a merge
        conflict the AI agent must resolve itself.

        Unlike :meth:`_park_in_waiting_for_human` (which takes a ticket OUT
        of the available pool for a human to unblock), this puts it BACK
        IN: READY + still assigned + unclaimed is exactly what
        :meth:`_next_worker_ticket` selects from, so the next agent poll
        picks the ticket straight back up. Forces the state directly to
        READY via ``human_transition`` — none of IN_PROGRESS,
        WAITING_FOR_HUMAN, or BLOCKED (the only states a failed merge can
        be attempted from) have READY as a legal AI-transition target, and
        this is Marcus correcting the record to match the board move it
        just made, not a normal AI-initiated step. The branch and its
        commit history are untouched — :meth:`~src.core.git_branch_manager.
        BranchManager.create_branch` resumes an existing branch rather than
        recreating it — so whichever agent picks this up next finds the
        same branch (and the rebase-needed comment) where it was left.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        reason : str
            Reason recorded on the transition.
        """
        try:
            self._lifecycle.human_transition(
                ticket_id, self._provider, TicketState.READY, reason=reason
            )
        except (InvalidTransitionError, KeyError):
            pass
        try:
            self._lifecycle.release_ticket(ticket_id, self._provider)
        except KeyError:
            pass

    async def _clear_merge_conflict_flag(self, ticket_id: str) -> None:
        """Clear the visible ``merge-conflict`` card tag, if set.

        Called from every place a ticket moves OUT of Ready/In Progress
        into Waiting-for-Human (for whatever reason — resubmitted for
        review, the AI got stuck, a missing project description) and
        from a successful merge. A ticket sitting in Waiting-for-Human
        still showing a merge-conflict tag from an earlier, now-resolved
        attempt wrongly implies to the reviewing human that it's still
        broken. If a LATER merge attempt fails again, :meth:`_merge_to_main`
        (via :meth:`set_merge_conflict_flag`) sets it right back.

        Best-effort: only a KanboardKanban-specific capability, and a
        failure here must never block whatever workflow step called it.
        """
        if not hasattr(self._kanban, "set_merge_conflict_flag"):
            return
        try:
            await self._kanban.set_merge_conflict_flag(ticket_id, present=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not clear merge-conflict flag for %s: %s", ticket_id, exc
            )

    async def _set_verify_round_tag(
        self, ticket_id: str, round_number: Optional[int]
    ) -> None:
        """Set (or clear) the visible ``Verify N`` card tag, best-effort.

        Called from :meth:`_autocomplete_ticket` so a ticket cycling
        through AI-gate multi-round verification shows which round is
        active directly on the board, not just in a buried comment — see
        :meth:`KanboardKanban.set_verify_round_tag` for the full
        rationale. ``round_number=None`` removes the tag (all rounds
        finished, or a merge failure reset verification for a retry).

        Best-effort: only a KanboardKanban-specific capability, and a
        failure here must never block whatever workflow step called it.
        """
        if not hasattr(self._kanban, "set_verify_round_tag"):
            return
        try:
            await self._kanban.set_verify_round_tag(ticket_id, round_number)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not set verify-round tag (%s) for %s: %s",
                round_number,
                ticket_id,
                exc,
            )

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

        Called whenever the ticket IS assigned to a human (the assignment
        is the "please work on this" signal — see ``_on_ticket_assigned``)
        and its column suggests work should happen: normally ``READY`` or
        ``IN_PROGRESS``, but also ``BLOCKED``/``WAITING_FOR_HUMAN`` when a
        human re-assigns a stuck ticket to resume it (see
        ``TestDeadEndStateRecovery``) — this method itself walks the
        record forward from wherever it currently sits. The one state it
        must never be called for is a decomposed PARENT ticket (one with
        recognized children), guarded below.

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

        # Any AI-verify round bookkeeping left over from an EARLIER,
        # abandoned verification episode is stale and must not be reused.
        # A ticket reaching here with record.state already IN_PROGRESS is
        # the ordinary "picked back up mid-episode to fix issues from the
        # last verify round" resume (see _pickup_next_ticket, called right
        # after _autocomplete_ticket releases a ticket whose round failed
        # or whose gate/state was never actually left) — that case must
        # NOT be cleared, or multi-round verify would never get past round
        # 1. Any OTHER state here (TODO/READY/BLOCKED/WAITING_FOR_HUMAN)
        # means the ticket had to LEAVE IN_PROGRESS to get there, so any
        # verify-round data still keyed on its id belongs to a DIFFERENT,
        # already-over episode — e.g. the gate flipped to human mid-round
        # (leaving self._ticket_verify_rounds[ticket_id] set from the AI
        # path), the ticket went through human review/close instead, and
        # is now being reopened or re-assigned with the gate back on AI.
        # Reusing that stale count would silently run FEWER verification
        # rounds than configured for code that was never actually
        # verified under the new episode. Harmless no-op when there is
        # nothing to clear (the overwhelmingly common case, since most
        # tickets never touch AI-verify at all).
        if record.state != TicketState.IN_PROGRESS:
            self._ticket_verify_rounds.pop(ticket_id, None)
            self._ticket_verify_last_passed.pop(ticket_id, None)
            await self._set_verify_round_tag(ticket_id, None)

        # A ticket with recognized children is a decomposed PARENT — a
        # tracking shell with no branch of its own (see
        # _complete_parent_ticket's docstring). Its lifecycle is owned
        # entirely by _check_parent_completion / _reconcile_blocked_parents,
        # never by this generic "start AI work" path. Without this guard, a
        # parent sitting BLOCKED with all its children already DONE passes
        # the dependency gate below exactly like an ordinary
        # dependency-blocked ticket would: decompose_ticket's own
        # create_task_link(parent, child, 3) ("parent is blocked by
        # child") links are structurally indistinguishable from a real
        # dependency link at the Kanboard API level, so _dependencies_
        # satisfied() reports the parent's "dependencies" (its children)
        # as met the moment they're all Done. _on_ticket_assigned's own
        # "resume a stuck ticket by re-assigning it" recovery path (see
        # TestDeadEndStateRecovery) deliberately calls this method for
        # BLOCKED/WAITING_FOR_HUMAN tickets too — exactly the states a
        # decomposed parent sits in — so without this guard a stray
        # re-assignment (or any other future caller) would claim and
        # start the parent like a normal ticket, moving its card to
        # Ready/In Progress instead of Waiting for Human. Regression:
        # this is exactly the bug where a fully-completed decomposed
        # parent's card showed up in "Ready" instead of "Waiting for
        # Human".
        if self._children_of(ticket_id):
            logger.debug(
                "Ticket %s has recognized children — refusing to start it "
                "directly (it's a decomposed parent; "
                "_check_parent_completion owns its lifecycle)",
                ticket_id,
            )
            return

        # Project access gate: Marcus (and any AI agent) must not claim or
        # touch a ticket whose Kanboard project has not been explicitly
        # enabled by a human — see ProjectAccessSettingManager. Checked
        # before the dependency gate below (no point spending an RPC
        # resolving dependencies for a ticket Marcus isn't allowed to
        # touch) and before any claim is taken, so there is nothing to
        # unwind on the early return besides the release-if-present below
        # (matches the failed-branch-creation bail-out further down).
        # Unresolvable project id (non-Kanboard provider, RPC failure)
        # does not block — the access gate only applies where it can
        # actually be evaluated.
        kanboard_project_id = await self._resolve_kanboard_project_id(ticket_id)
        if kanboard_project_id is not None and not self._project_access.is_enabled(
            kanboard_project_id
        ):
            logger.info(
                "Refusing to start ticket %s: Kanboard project %d is not "
                "enabled for Marcus (toggle it on from the project's board "
                "header).",
                ticket_id,
                kanboard_project_id,
            )
            try:
                self._lifecycle.release_ticket(ticket_id, self._provider)
            except KeyError:
                pass
            return

        # Preemptive dependency gate: never START a ticket whose "is blocked
        # by"/"depends on" tickets aren't Done+merged yet. Park it BLOCKED
        # (recording the blockers); _resume_tickets_blocked_by moves it back
        # to In Progress the moment the LAST dependency completes. Only gate
        # fresh starts and dependency-resumes (TODO/READY/BLOCKED) — not the
        # WAITING_FOR_HUMAN/REOPENED feedback resumes, which already passed.
        if record.state in (
            TicketState.TODO,
            TicketState.READY,
            TicketState.BLOCKED,
        ):
            deps_ok, unmet = await self._dependencies_satisfied(ticket_id)
            if not deps_ok:
                await self._block_on_dependencies(ticket_id, unmet)
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
            # A ticket that previously failed to merge (tag set), got
            # parked back in Ready, and is picked up again but fails
            # this stack check would otherwise land in Waiting for
            # Human still showing a stale "merge-conflict" tag.
            # Mirrors every other path into Waiting for Human.
            await self._clear_merge_conflict_flag(ticket_id)
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

        # Record when the AI agent actually began work — mirrors Kanboard's
        # native "Start now" link so a human never has to click it
        # themselves. Only a KanboardKanban-specific capability (other
        # providers don't have this concept); best-effort, never blocks
        # starting the ticket.
        if hasattr(self._kanban, "set_task_started_if_unset"):
            try:
                await self._kanban.set_task_started_if_unset(ticket_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not set start date for ticket %s: %s", ticket_id, exc
                )

        # This ticket is not necessarily brand new: create_branch RESUMES an
        # existing remote branch rather than overwriting it (see its
        # docstring), so check whether it already had commits from a prior
        # session — best-effort; a lookup failure must never block starting
        # the ticket, it just means the comment reads as a fresh start.
        resumed_commits: List[str] = []
        try:
            resumed_commits = await branch_mgr.get_branch_commits(branch_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not check %s for prior commits: %s", branch_name, exc
            )

        # Post "started" comment.
        ac_items = self._get_ac_items(record)
        comment = CommentFormatter.started(
            ticket_id=ticket_id,
            branch_name=branch_name,
            assignee=record.assignee or "AI agent",
            ac_items=ac_items,
            resumed_commits=resumed_commits,
        )
        await self._post_comment(ticket_id, comment)
        if resumed_commits:
            logger.info(
                "AI work RESUMED for ticket %s (branch %s already had %d "
                "commit(s) from a prior session)",
                ticket_id,
                branch_name,
                len(resumed_commits),
            )
        else:
            logger.info(
                "AI work started for ticket %s (branch %s)", ticket_id, branch_name
            )

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

        # Re-check immediately before writing: a concurrent decompose_ticket
        # call can apply its "Sub-ticket of #N" parent-marker patch (see
        # the race-recovery comment in decompose_ticket's child-creation
        # loop) at any point during the awaits above (LLM generation,
        # comment post, description update). _on_ticket_new decides
        # is_subticket from a record captured before those awaits ran, so
        # without this recheck an unconditional write here would silently
        # re-clobber a patch that just landed, stranding the parent BLOCKED
        # forever despite that fix. If the marker is present now, a
        # concurrent caller's AC (with the marker) wins — skip this write.
        current = self._lifecycle.get(ticket_id, self._provider)
        if current is not None and "Sub-ticket of #" in (
            current.acceptance_criteria or ""
        ):
            logger.info(
                "Skipping AC overwrite for %s — a concurrent decompose_ticket "
                "call already applied the parent marker",
                ticket_id,
            )
            return

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
        """Best-effort resolve a ticket's Kanboard project id, or ``None``.

        Memoised. This is called for every assigned ticket by both
        :meth:`_next_worker_ticket` and :meth:`_withheld_ticket_reasons`,
        i.e. twice per ticket on every ``marcus_work`` poll — with a
        handful of blocked tickets and an agent polling every ~10s that is
        a steady stream of ``getTask`` calls into Kanboard's SQLite
        backend, which is precisely the write contention that surfaces as
        "database is locked". A ticket does not move between projects in
        practice (Kanboard's moveTaskToProject is a deliberate, rare
        action Marcus never performs), so resolve it once and keep it.
        Failures are NOT cached, so a transient RPC error is retried.
        """
        cached = self._ticket_project_ids.get(ticket_id)
        if cached is not None:
            return cached
        try:
            task = await self._kanban.get_task_by_id(ticket_id)
            if task:
                raw = (task.source_context or {}).get("kanboard_task", {})
                pid_raw = raw.get("project_id")
                if pid_raw:
                    pid = int(pid_raw)
                    self._ticket_project_ids[ticket_id] = pid
                    return pid
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not resolve project id for %s: %s", ticket_id, exc)
        return None

    async def _project_display_name(self, project_id: int) -> str:
        """Return ``"7 ('Website')"``-style label for a Kanboard project.

        Kanboard shows humans project NAMES and never ids, so a bare id in
        a message asking someone to change a project setting cannot be
        acted on — worse, a human who has already enabled a DIFFERENT
        project reads it as Marcus being confused rather than as a second
        project needing the same toggle. Falls back to the bare id when
        the provider can't name it (non-Kanboard provider, RPC failure).
        """
        cached = self._project_names.get(project_id)
        if cached is None:
            getter = getattr(self._kanban, "get_project_name", None)
            if getter is not None:
                try:
                    cached = await getter(project_id) or ""
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "Could not resolve name for project %s: %s", project_id, exc
                    )
                    cached = ""
            else:
                cached = ""
            self._project_names[project_id] = cached
        return f"{project_id} ({cached!r})" if cached else str(project_id)

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
            # Whether the LAST completed round actually passed — checked
            # alongside rounds_done below so a verify_count DECREASE via
            # the live gate-setting API (e.g. a human watching round 1
            # fail and deciding 3 rounds was overkill) can never make the
            # "already satisfied" fast path merge code whose most recent
            # verification attempt genuinely FAILED. See
            # TestVerifyCountLoweredMidFlight.
            last_passed = self._ticket_verify_last_passed.get(ticket_id, False)

            if rounds_done >= verify_count and last_passed:
                # All N (now possibly fewer than originally configured)
                # rounds are done and the last one genuinely passed —
                # clear the bookkeeping and fall through to merge.
                self._ticket_verify_rounds.pop(ticket_id, None)
                self._ticket_verify_last_passed.pop(ticket_id, None)
                # Defensive: the "Verify N" card tag should already be gone
                # (cleared when the final round passed), but a prior crash
                # between that clear and the merge could leave it stale.
                await self._set_verify_round_tag(ticket_id, None)

            else:
                current_round = rounds_done + 1
                # Stamp the round on the card BEFORE running it, so "Verify N"
                # is what a human sees on the board for the whole time this
                # ticket sits in "in progress" going through round N — not
                # just a comment they'd have to open the ticket to find.
                await self._set_verify_round_tag(ticket_id, current_round)
                result = await self._run_verification_round(ticket_id, record, branch_name)
                self._ticket_verify_rounds[ticket_id] = current_round
                self._ticket_verify_last_passed[ticket_id] = result.passed

                # >= rather than == : verify_count may have been lowered
                # since rounds_done was last recorded (the branch above
                # only skips straight to merge when the last attempt
                # already passed — otherwise a round always runs here,
                # which can make current_round exceed a since-lowered
                # verify_count). This round is still the first to confirm
                # a pass against the CURRENT threshold, so it's final.
                if result.passed and current_round >= verify_count:
                    # Last round passed → all verification is done. Clear the
                    # card tag before falling through to merge/Done.
                    self._ticket_verify_rounds.pop(ticket_id, None)
                    self._ticket_verify_last_passed.pop(ticket_id, None)
                    await self._set_verify_round_tag(ticket_id, None)
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
            # Clean up the verify-round bookkeeping so a retry starts fresh.
            # (No "Verify N" card tag to clear here: every path that
            # reaches this merge attempt already cleared it — either the
            # final-round-passed branch above, or the defensive
            # rounds_done>=verify_count branch. verify_count==0 never sets
            # it in the first place.)
            self._ticket_verify_rounds.pop(ticket_id, None)
            self._ticket_verify_last_passed.pop(ticket_id, None)
            comment = CommentFormatter.merge_conflict(
                ticket_id=ticket_id,
                branch_name=branch_name,
                main_branch=main_branch,
            )
            await self._post_comment(ticket_id, comment)
            # Send the ticket back to an AI agent to rebase and resolve the
            # conflict itself — see the matching comment in
            # _merge_ticket_to_main. Without this the ticket stayed
            # IN_PROGRESS and claimed forever, leaking a parallel slot.
            self._park_in_ready_for_rebase(
                ticket_id,
                reason="Auto-merge to main failed; branch needs rebase and conflict resolution",
            )
            try:
                await self._kanban.move_task_to_column(ticket_id, "ready")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not move %s to ready after auto-merge fail: %s",
                    ticket_id,
                    exc,
                )
            await self._pickup_next_ticket()
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
        # If this was a sub-ticket, its parent may now be fully complete.
        await self._maybe_complete_parent(ticket_id)

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
            await self._move_column_with_retry(ticket_id, "waiting for human")
            await self._clear_merge_conflict_flag(ticket_id)
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
