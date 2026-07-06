"""
Ticket lifecycle state machine for human-gated AI workflows.

Every ticket processed by Marcus moves through a defined set of states.

Assignment rules
----------------
- A ticket is **assigned to a human** when Kanboard's ``owner_id`` is
  non-zero.  While a human holds the ticket, AI stays out.
- A ticket is **unassigned** (``owner_id == 0``) when AI should work.
- Humans can set **any state except** ``waiting_for_human`` (that state is
  AI-only — it signals "I finished; please review").
- AI should work on a ticket whenever its status is ``ready`` or
  ``in_progress`` **and** the ticket is unassigned.

Anti-duplication
----------------
:meth:`TicketLifecycleManager.claim_ticket` is an atomic test-and-set
that lets exactly one AI agent claim a ticket.  If a second agent calls
it while the first holds the claim, it returns ``False``.  The claim is
released when the ticket moves to ``done``, ``todo``, or ``reopened``.

State diagram
-------------
::

    TODO  ◄─────────────── (human reset from any state)
        │  status → ready or in_progress, unassigned
        ▼
    READY / IN_PROGRESS ◄──────────────────────────────┐
        │  AI claims ticket, creates branch             │ human responds /
        ▼  status set to in_progress                    │ moves back to
    WAITING_FOR_HUMAN ─────────────────────────────────┘  "in progress"
        │  (AI-only state; humans cannot set this)
        │
    IN_PROGRESS
        │  ticket depends on unfinished work
        ▼
    BLOCKED
        │  dependency resolved
        ▼
    IN_PROGRESS
        │  human marks "done"
        ▼
    DONE  ◄────── (branch merged to main, claim released)
        │  human reopens ticket
        ▼
    REOPENED
        │  board watcher notifies Marcus
        ▼
    IN_PROGRESS  (branch rebased on main, work resumes)

Classes
-------
TicketState
    Enum of all possible ticket states.
TicketRecord
    Persistent record of a single ticket's lifecycle state.
TicketLifecycleManager
    Creates, transitions, and persists ticket records.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_STATE_FILE_DEFAULT = "./data/ticket_lifecycle.json"


class TicketState(Enum):
    """All states a ticket can occupy in the human-gated workflow.

    Attributes
    ----------
    TODO : str
        Ticket exists but has not been assigned or readied.  AI is idle.
    READY : str
        Human assigned the ticket to themselves AND moved it to the ready
        column.  AI will start creating the branch and working.
    IN_PROGRESS : str
        AI agent is actively working on the ticket.  The kanban column is
        "in progress".
    WAITING_FOR_HUMAN : str
        AI finished or needs external input; waiting for human response.
        The kanban column is "waiting for human".
    BLOCKED : str
        Ticket depends on another unfinished ticket.  AI is paused.
        The kanban column is "blocked".
    DONE : str
        Human marked the ticket done; branch has been merged to main.
    REOPENED : str
        Human reopened an already-done ticket; AI will rebase and continue.
    """

    TODO = "todo"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    WAITING_FOR_HUMAN = "waiting_for_human"
    BLOCKED = "blocked"
    DONE = "done"
    REOPENED = "reopened"


# Legal state transitions for AI-initiated moves: {from: [allowed_to, ...]}
_AI_TRANSITIONS: Dict[TicketState, List[TicketState]] = {
    TicketState.TODO: [TicketState.READY],
    TicketState.READY: [TicketState.IN_PROGRESS, TicketState.TODO],
    TicketState.IN_PROGRESS: [
        TicketState.WAITING_FOR_HUMAN,
        TicketState.BLOCKED,
        TicketState.DONE,
        TicketState.TODO,
    ],
    TicketState.WAITING_FOR_HUMAN: [
        TicketState.IN_PROGRESS,
        TicketState.DONE,
    ],
    TicketState.BLOCKED: [TicketState.IN_PROGRESS, TicketState.DONE],
    TicketState.DONE: [TicketState.REOPENED],
    TicketState.REOPENED: [TicketState.IN_PROGRESS],
}

# States that only AI agents may set; humans cannot drag a card there.
_HUMAN_FORBIDDEN_TARGETS: set[TicketState] = {TicketState.WAITING_FOR_HUMAN}


@dataclass
class TicketRecord:
    """Persistent record tracking one ticket's lifecycle.

    Parameters
    ----------
    ticket_id : str
        Provider-specific ticket identifier (e.g. ``PROJ-42``, ``123``).
    provider : str
        Kanban provider name (``"github"``, ``"jira"``, etc.).
    state : TicketState
        Current lifecycle state.  Starts as ``TODO``; advances to ``READY``
        when a human assigns and readies the ticket.
    branch_name : str
        Git branch created for this ticket (``ticket/{provider}/{id}``).
    assignee : Optional[str]
        Username/login of the human who self-assigned the ticket.
    acceptance_criteria : str
        Markdown checklist of acceptance criteria (may be empty when ticket
        was created by a human without explicit AC).
    ac_hash : str
        SHA-256 hex digest of *acceptance_criteria* at last AI read; used
        to detect human edits.
    created_at : datetime
        When this record was first created by Marcus.
    updated_at : datetime
        Timestamp of the most recent state transition.
    history : List[Dict[str, Any]]
        Append-only log of ``{from, to, timestamp, reason}`` transitions.
    merged_at : Optional[datetime]
        Set when the branch is merged to main.
    dev_env_port : Optional[int]
        TCP port the hot-reload dev environment is running on, if active.
    ai_agent_id : Optional[str]
        Identifier of the AI agent currently working on this ticket.
    """

    ticket_id: str
    provider: str
    state: TicketState = TicketState.TODO
    branch_name: str = ""
    assignee: Optional[str] = None
    acceptance_criteria: str = ""
    ac_hash: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    history: List[Dict[str, Any]] = field(default_factory=list)
    merged_at: Optional[datetime] = None
    dev_env_port: Optional[int] = None
    ai_agent_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialise record to a JSON-compatible dictionary."""
        d = asdict(self)
        d["state"] = self.state.value
        d["created_at"] = self.created_at.isoformat()
        d["updated_at"] = self.updated_at.isoformat()
        d["merged_at"] = self.merged_at.isoformat() if self.merged_at else None
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TicketRecord":
        """Deserialise a record produced by :meth:`to_dict`."""
        d = dict(d)
        d["state"] = TicketState(d["state"])
        d["created_at"] = datetime.fromisoformat(d["created_at"])
        d["updated_at"] = datetime.fromisoformat(d["updated_at"])
        if d.get("merged_at"):
            d["merged_at"] = datetime.fromisoformat(d["merged_at"])
        return cls(**d)

    @property
    def key(self) -> str:
        """Unique storage key: ``{provider}:{ticket_id}``."""
        return f"{self.provider}:{self.ticket_id}"


class InvalidTransitionError(Exception):
    """Raised when a requested state transition is not permitted."""


class TicketLifecycleManager:
    """Creates, transitions, and persists ticket lifecycle records.

    All records are stored in a single JSON file so that Marcus can
    survive restarts without losing lifecycle state.

    Parameters
    ----------
    state_file : str
        Path to the JSON persistence file.  Defaults to
        ``./data/ticket_lifecycle.json``.
    """

    def __init__(self, state_file: str = _STATE_FILE_DEFAULT) -> None:
        """Initialise and load existing records from disk."""
        self._state_file = state_file
        self._records: Dict[str, TicketRecord] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_create(
        self,
        ticket_id: str,
        provider: str,
        *,
        branch_name: str = "",
        acceptance_criteria: str = "",
    ) -> TicketRecord:
        """Return the existing record or create a new TODO one.

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.
        branch_name : str
            Git branch name.  Generated from ticket_id if not supplied.
        acceptance_criteria : str
            Initial acceptance criteria markdown text.

        Returns
        -------
        TicketRecord
            The (possibly newly created) lifecycle record.
        """
        key = f"{provider}:{ticket_id}"
        if key not in self._records:
            if not branch_name:
                safe_id = ticket_id.replace("/", "-").replace(" ", "-").lower()
                branch_name = f"ticket/{provider}/{safe_id}"
            record = TicketRecord(
                ticket_id=ticket_id,
                provider=provider,
                branch_name=branch_name,
                acceptance_criteria=acceptance_criteria,
            )
            self._records[key] = record
            self._save()
            logger.info("Created lifecycle record for %s", key)
        return self._records[key]

    def get(self, ticket_id: str, provider: str) -> Optional[TicketRecord]:
        """Return the record for a ticket, or ``None`` if not tracked."""
        return self._records.get(f"{provider}:{ticket_id}")

    def transition(
        self,
        ticket_id: str,
        provider: str,
        to_state: TicketState,
        *,
        reason: str = "",
        assignee: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> TicketRecord:
        """Advance a ticket to a new state.

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.
        to_state : TicketState
            Target state.
        reason : str
            Human-readable reason for the transition (stored in history).
        assignee : Optional[str]
            Set or update the assignee (used on ASSIGNED transition).
        agent_id : Optional[str]
            Set or update the AI agent ID (used on IN_PROGRESS).

        Returns
        -------
        TicketRecord
            The updated record.

        Raises
        ------
        KeyError
            If the ticket is not tracked.
        InvalidTransitionError
            If the transition is not permitted from the current state.
        """
        key = f"{provider}:{ticket_id}"
        record = self._records.get(key)
        if record is None:
            raise KeyError(f"Ticket {key} is not tracked by lifecycle manager")

        allowed = _AI_TRANSITIONS.get(record.state, [])
        if to_state not in allowed:
            raise InvalidTransitionError(
                f"Cannot transition {key} from {record.state.value!r} "
                f"to {to_state.value!r}. Allowed targets: "
                f"{[s.value for s in allowed]}"
            )

        from_state = record.state
        record.state = to_state
        record.updated_at = datetime.now(timezone.utc)

        if assignee is not None:
            record.assignee = assignee
        if agent_id is not None:
            record.ai_agent_id = agent_id

        record.history.append(
            {
                "from": from_state.value,
                "to": to_state.value,
                "timestamp": record.updated_at.isoformat(),
                "reason": reason,
            }
        )

        self._save()
        logger.info(
            "Ticket %s transitioned %s → %s  reason=%r",
            key,
            from_state.value,
            to_state.value,
            reason,
        )
        return record

    def update_acceptance_criteria(
        self,
        ticket_id: str,
        provider: str,
        new_ac: str,
        new_ac_hash: str,
    ) -> TicketRecord:
        """Update the stored acceptance criteria (called when human edits AC).

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.
        new_ac : str
            Full updated acceptance criteria text.
        new_ac_hash : str
            SHA-256 hex digest of *new_ac*.

        Returns
        -------
        TicketRecord
            The updated record.
        """
        key = f"{provider}:{ticket_id}"
        record = self._records[key]
        record.acceptance_criteria = new_ac
        record.ac_hash = new_ac_hash
        record.updated_at = datetime.now(timezone.utc)
        self._save()
        return record

    def set_merged(
        self,
        ticket_id: str,
        provider: str,
        merged_at: Optional[datetime] = None,
    ) -> TicketRecord:
        """Record that the ticket's branch was merged to main.

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.
        merged_at : Optional[datetime]
            Merge timestamp; defaults to now.

        Returns
        -------
        TicketRecord
            The updated record.
        """
        key = f"{provider}:{ticket_id}"
        record = self._records[key]
        record.merged_at = merged_at or datetime.now(timezone.utc)
        self._save()
        return record

    def set_dev_env_port(
        self,
        ticket_id: str,
        provider: str,
        port: Optional[int],
    ) -> TicketRecord:
        """Store the port on which the ticket's dev environment is running.

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.
        port : Optional[int]
            Port number, or ``None`` to clear.

        Returns
        -------
        TicketRecord
            The updated record.
        """
        key = f"{provider}:{ticket_id}"
        record = self._records[key]
        record.dev_env_port = port
        self._save()
        return record

    def set_assignee(
        self,
        ticket_id: str,
        provider: str,
        assignee: str,
    ) -> TicketRecord:
        """Record the human assignee without triggering a state transition.

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.
        assignee : str
            Username of the human who claimed the ticket.

        Returns
        -------
        TicketRecord
            The updated record.

        Raises
        ------
        KeyError
            If the ticket is not tracked.
        """
        key = f"{provider}:{ticket_id}"
        record = self._records.get(key)
        if record is None:
            raise KeyError(f"Ticket {key} is not tracked by lifecycle manager")
        record.assignee = assignee
        record.updated_at = datetime.now(timezone.utc)
        self._save()
        return record

    def human_transition(
        self,
        ticket_id: str,
        provider: str,
        to_state: TicketState,
        *,
        reason: str = "",
        assignee: Optional[str] = None,
    ) -> TicketRecord:
        """Advance a ticket to a new state as initiated by a human actor.

        Humans may move a ticket to any state **except**
        ``WAITING_FOR_HUMAN`` (that state is set only by AI agents to
        signal "I finished; please review").

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.
        to_state : TicketState
            Target state.
        reason : str
            Human-readable reason for the transition (stored in history).
        assignee : Optional[str]
            Set or update the assignee.

        Returns
        -------
        TicketRecord
            The updated record.

        Raises
        ------
        KeyError
            If the ticket is not tracked.
        InvalidTransitionError
            If *to_state* is ``WAITING_FOR_HUMAN``.
        """
        if to_state in _HUMAN_FORBIDDEN_TARGETS:
            raise InvalidTransitionError(
                f"Humans cannot set a ticket to {to_state.value!r}. "
                "That state is reserved for AI agents."
            )
        key = f"{provider}:{ticket_id}"
        record = self._records.get(key)
        if record is None:
            raise KeyError(f"Ticket {key} is not tracked by lifecycle manager")

        from_state = record.state
        record.state = to_state
        record.updated_at = datetime.now(timezone.utc)

        if assignee is not None:
            record.assignee = assignee

        record.history.append(
            {
                "from": from_state.value,
                "to": to_state.value,
                "timestamp": record.updated_at.isoformat(),
                "reason": reason,
                "actor": "human",
            }
        )

        self._save()
        logger.info(
            "Ticket %s human-transitioned %s → %s  reason=%r",
            key,
            from_state.value,
            to_state.value,
            reason,
        )
        return record

    def claim_ticket(
        self,
        ticket_id: str,
        provider: str,
        agent_id: str,
    ) -> bool:
        """Atomically claim a ticket for an AI agent.

        This is the anti-duplication gate: at most one AI agent holds a
        claim at any time.  A second call while another agent holds the
        claim returns ``False`` without modifying the record.  The claim
        is automatically released by :meth:`release_ticket`.

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.
        agent_id : str
            Identifier of the claiming AI agent.

        Returns
        -------
        bool
            ``True`` if the claim was acquired; ``False`` if already
            claimed by another agent.

        Raises
        ------
        KeyError
            If the ticket is not tracked.
        """
        key = f"{provider}:{ticket_id}"
        record = self._records.get(key)
        if record is None:
            raise KeyError(f"Ticket {key} is not tracked by lifecycle manager")

        if record.ai_agent_id is not None:
            logger.debug(
                "Ticket %s already claimed by %s; refused claim for %s",
                key,
                record.ai_agent_id,
                agent_id,
            )
            return False

        record.ai_agent_id = agent_id
        record.updated_at = datetime.now(timezone.utc)
        self._save()
        logger.info("Ticket %s claimed by agent %s", key, agent_id)
        return True

    def release_ticket(
        self,
        ticket_id: str,
        provider: str,
    ) -> TicketRecord:
        """Release the AI agent claim on a ticket.

        Clears ``ai_agent_id`` so a new agent can claim it.  Safe to
        call when the ticket is already unclaimed.

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.

        Returns
        -------
        TicketRecord
            The updated record.

        Raises
        ------
        KeyError
            If the ticket is not tracked.
        """
        key = f"{provider}:{ticket_id}"
        record = self._records.get(key)
        if record is None:
            raise KeyError(f"Ticket {key} is not tracked by lifecycle manager")

        prev_agent = record.ai_agent_id
        record.ai_agent_id = None
        record.updated_at = datetime.now(timezone.utc)
        self._save()
        if prev_agent:
            logger.info(
                "Ticket %s claim released (was held by %s)", key, prev_agent
            )
        return record

    def get_agent_ticket(self, agent_id: str) -> Optional[str]:
        """Return the ``ticket_id`` currently claimed by *agent_id*, or ``None``.

        Scans all records for the first one whose ``ai_agent_id`` matches.
        Returns ``None`` if the agent holds no claim.

        Parameters
        ----------
        agent_id : str
            The AI agent identifier to look up.

        Returns
        -------
        Optional[str]
            The ticket identifier, or ``None``.
        """
        for record in self._records.values():
            if record.ai_agent_id == agent_id:
                return record.ticket_id
        return None

    def get_available_tickets(self) -> List[TicketRecord]:
        """Return tickets that are workable, human-assigned, and unclaimed by AI.

        A ticket is *available* when:

        * State is ``READY`` or ``IN_PROGRESS``.
        * ``ai_agent_id`` is ``None`` (no AI holds a claim).
        * ``assignee`` is set and non-empty (a human owner is present).

        Returns
        -------
        List[TicketRecord]
            All matching records (unsorted; caller decides ordering).
        """
        return [
            r
            for r in self._records.values()
            if r.state in (TicketState.READY, TicketState.IN_PROGRESS)
            and r.ai_agent_id is None
            and r.assignee not in (None, "", "0")
        ]

    def all_records(self) -> List[TicketRecord]:
        """Return all tracked ticket records."""
        return list(self._records.values())

    def in_state(self, state: TicketState) -> List[TicketRecord]:
        """Return all records currently in *state*."""
        return [r for r in self._records.values() if r.state == state]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load records from the JSON state file."""
        if not os.path.exists(self._state_file):
            return
        try:
            with open(self._state_file) as fh:
                raw: Dict[str, Any] = json.load(fh)
            for key, d in raw.items():
                self._records[key] = TicketRecord.from_dict(d)
            logger.debug(
                "Loaded %d ticket records from %s", len(self._records), self._state_file
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load ticket lifecycle state: %s", exc)

    def _save(self) -> None:
        """Persist all records to the JSON state file."""
        os.makedirs(os.path.dirname(self._state_file) or ".", exist_ok=True)
        tmp = self._state_file + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(
                    {k: v.to_dict() for k, v in self._records.items()},
                    fh,
                    indent=2,
                )
            os.replace(tmp, self._state_file)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to save ticket lifecycle state: %s", exc)
