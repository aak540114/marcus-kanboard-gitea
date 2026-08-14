"""
Per-project and per-ticket gate-mode and AI-verify settings.

Gate mode controls whether human approval is required at key workflow
checkpoints (``human``) or whether the AI works autonomously from ready to
done without pausing for review (``ai``).

AI-verify count (only applies when gate is ``ai``) controls how many
sequential LLM review rounds run before the branch is allowed to merge.
When set to N, the workflow runs N verification rounds with agent fix
cycles between them.  A count of 0 disables verification entirely.

Decompose-enabled (project-level only — see :meth:`GateSettingManager.
get_effective_decompose_enabled`) controls whether Marcus is allowed to
automatically split a large ticket into linked sub-tickets
(:meth:`~src.workflows.human_gated_workflow.HumanGatedWorkflow.
decompose_ticket`) at all, for every ticket in that project. Defaults to
``True`` (enabled) — matching decomposition's existing behavior before
this setting existed.

Precedence for gate and verify_count (highest to lowest):
1. Per-ticket setting
2. Per-project setting
3. Hard default (``"human"`` for gate; ``0`` for verify_count)

decompose_enabled has no per-ticket layer — it is a project-wide switch
("stop splitting tickets in this project at all"), not a per-ticket
override.

Settings are persisted as a JSON file at::

    <data_dir>/gate_settings.json

Schema::

    {
      "projects": {
        "1": {"gate": "human"},
        "2": {"gate": "ai", "verify_count": 2, "decompose_enabled": false}
      },
      "tickets":  {"42": {"gate": "ai"}, "99": {"gate": null, "verify_count": 0}}
    }

A ticket entry of ``null`` for a key means "reset to project default" — the
manager stores nothing for that ticket and effective resolution falls back to
the project.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Literal, Optional, cast

logger = logging.getLogger(__name__)

GateMode = Literal["human", "ai"]
_DEFAULT_GATE: GateMode = "human"
_DEFAULT_VERIFY_COUNT: int = 0
_DEFAULT_DECOMPOSE_ENABLED: bool = True
_DEFAULT_DATA_DIR = Path(os.getcwd()) / "data"


class GateSettingManager:
    """Reads and writes per-project / per-ticket gate-mode and verify settings.

    Parameters
    ----------
    data_dir : Optional[Path]
        Directory that contains ``gate_settings.json``.  Defaults to
        ``./data/`` relative to the Marcus working directory.
    """

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self._path = (data_dir or _DEFAULT_DATA_DIR) / "gate_settings.json"
        self._data: Dict[str, Any] = self._load()

    # ------------------------------------------------------------------
    # Gate mode — read
    # ------------------------------------------------------------------

    def get_project_gate(self, project_id: int) -> Optional[GateMode]:
        """Return the gate set for a project, or ``None`` if not set.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.

        Returns
        -------
        Optional[GateMode]
            ``"human"`` or ``"ai"``, or ``None`` when no project setting
            has been stored.
        """
        val = self._project_entry(project_id).get("gate")
        return val if val in ("human", "ai") else None

    def get_ticket_gate(self, ticket_id: str) -> Optional[GateMode]:
        """Return the gate set for a specific ticket, or ``None`` if not set.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID.

        Returns
        -------
        Optional[GateMode]
            ``"human"`` or ``"ai"``, or ``None`` when the ticket inherits
            from its project.
        """
        val = self._ticket_entry(ticket_id).get("gate")
        return val if val in ("human", "ai") else None

    def get_effective_gate(self, ticket_id: str, project_id: int) -> GateMode:
        """Return the resolved gate mode for a ticket.

        Resolution order: ticket → project → ``"human"``.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID.
        project_id : int
            Kanboard project ID the ticket belongs to.

        Returns
        -------
        GateMode
            ``"human"`` or ``"ai"`` — never ``None``.
        """
        ticket_gate = self.get_ticket_gate(ticket_id)
        if ticket_gate is not None:
            return ticket_gate
        project_gate = self.get_project_gate(project_id)
        if project_gate is not None:
            return project_gate
        return _DEFAULT_GATE

    # ------------------------------------------------------------------
    # AI verify count — read
    # ------------------------------------------------------------------

    def get_project_verify_count(self, project_id: int) -> Optional[int]:
        """Return the number of AI verification rounds configured for a project.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.

        Returns
        -------
        Optional[int]
            Non-negative integer count, or ``None`` when no setting has been
            stored.
        """
        val = self._project_entry(project_id).get("verify_count")
        if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            return int(val)
        return None

    def get_ticket_verify_count(self, ticket_id: str) -> Optional[int]:
        """Return the number of AI verification rounds configured for a ticket.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID.

        Returns
        -------
        Optional[int]
            Non-negative integer count, or ``None`` when the ticket inherits
            from its project.
        """
        val = self._ticket_entry(ticket_id).get("verify_count")
        if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            return int(val)
        return None

    def get_effective_verify_count(self, ticket_id: str, project_id: int) -> int:
        """Return the resolved number of AI verification rounds for a ticket.

        Resolution order: ticket → project → ``0``.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID.
        project_id : int
            Kanboard project ID the ticket belongs to.

        Returns
        -------
        int
            Number of required verification rounds.  ``0`` means disabled.
        """
        ticket_count = self.get_ticket_verify_count(ticket_id)
        if ticket_count is not None:
            return ticket_count
        project_count = self.get_project_verify_count(project_id)
        if project_count is not None:
            return project_count
        return _DEFAULT_VERIFY_COUNT

    # ------------------------------------------------------------------
    # Decompose-enabled — read
    # ------------------------------------------------------------------

    def get_project_decompose_enabled(self, project_id: int) -> Optional[bool]:
        """Return whether decomposition is enabled for a project, or
        ``None`` if not explicitly set.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.

        Returns
        -------
        Optional[bool]
            ``True``/``False``, or ``None`` when no project setting has
            been stored (resolves to the default via
            :meth:`get_effective_decompose_enabled`).
        """
        val = self._project_entry(project_id).get("decompose_enabled")
        return val if isinstance(val, bool) else None

    def get_effective_decompose_enabled(self, project_id: int) -> bool:
        """Return whether Marcus may auto-decompose tickets in this project.

        No per-ticket layer — this is a project-wide switch. Resolution:
        project setting → ``True`` (decomposition is enabled by default,
        matching behavior before this setting existed).

        Parameters
        ----------
        project_id : int
            Kanboard project ID.

        Returns
        -------
        bool
            ``True`` unless a human has explicitly disabled decomposition
            for this project.
        """
        val = self.get_project_decompose_enabled(project_id)
        return _DEFAULT_DECOMPOSE_ENABLED if val is None else val

    # ------------------------------------------------------------------
    # Gate mode — write
    # ------------------------------------------------------------------

    def set_project_gate(self, project_id: int, gate: GateMode) -> None:
        """Persist the gate mode for a project.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.
        gate : GateMode
            ``"human"`` or ``"ai"``.
        """
        self._project_entry(project_id, create=True)["gate"] = gate
        self._save()
        logger.info("Set project %d gate to %r", project_id, gate)

    def set_ticket_gate(self, ticket_id: str, gate: Optional[GateMode]) -> None:
        """Persist (or clear) the gate mode for a specific ticket.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID.
        gate : Optional[GateMode]
            ``"human"`` or ``"ai"`` to override; ``None`` to reset to the
            project-level setting.
        """
        entry = self._ticket_entry(ticket_id, create=True)
        if gate is None:
            entry.pop("gate", None)
            if not entry:
                self._data.get("tickets", {}).pop(str(ticket_id), None)
        else:
            entry["gate"] = gate
        self._save()
        logger.info("Set ticket %s gate to %r", ticket_id, gate)

    # ------------------------------------------------------------------
    # AI verify count — write
    # ------------------------------------------------------------------

    def set_project_verify_count(self, project_id: int, count: int) -> None:
        """Persist the AI-verify round count for a project.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.
        count : int
            Number of verification rounds (0 = disabled).

        Raises
        ------
        ValueError
            If ``count`` is negative.
        """
        if count < 0:
            raise ValueError(f"verify_count must be >= 0, got {count}")
        self._project_entry(project_id, create=True)["verify_count"] = count
        self._save()
        logger.info("Set project %d verify_count to %d", project_id, count)

    def set_ticket_verify_count(self, ticket_id: str, count: Optional[int]) -> None:
        """Persist (or clear) the AI-verify round count for a specific ticket.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID.
        count : Optional[int]
            Non-negative integer to override; ``None`` to reset to the
            project-level setting.

        Raises
        ------
        ValueError
            If ``count`` is negative.
        """
        if count is not None and count < 0:
            raise ValueError(f"verify_count must be >= 0 or None, got {count}")
        entry = self._ticket_entry(ticket_id, create=True)
        if count is None:
            entry.pop("verify_count", None)
            if not entry:
                self._data.get("tickets", {}).pop(str(ticket_id), None)
        else:
            entry["verify_count"] = count
        self._save()
        logger.info("Set ticket %s verify_count to %r", ticket_id, count)

    # ------------------------------------------------------------------
    # Decompose-enabled — write
    # ------------------------------------------------------------------

    def set_project_decompose_enabled(self, project_id: int, enabled: bool) -> None:
        """Persist whether Marcus may auto-decompose tickets in a project.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.
        enabled : bool
            ``False`` stops Marcus from splitting any ticket in this
            project into sub-tickets, whether triggered automatically
            (a large ready ticket) or via the ``@marcus decompose``
            comment command — both go through
            :meth:`~src.workflows.human_gated_workflow.HumanGatedWorkflow.
            decompose_ticket`, which checks this setting.
        """
        self._project_entry(project_id, create=True)["decompose_enabled"] = enabled
        self._save()
        logger.info("Set project %d decompose_enabled to %r", project_id, enabled)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _project_entry(self, project_id: int, *, create: bool = False) -> Dict[str, Any]:
        """Return (and optionally create) the dict for a project."""
        key = str(project_id)
        projects = self._data.setdefault("projects", {})
        if key not in projects:
            if create:
                projects[key] = {}
            else:
                return {}
        entry = projects[key]
        # Migrate old string-only format: "ai" → {"gate": "ai"}
        if isinstance(entry, str):
            projects[key] = {"gate": entry}
            self._save()  # persist so the migration doesn't re-run every restart
        # Migrate old bool verify → int verify_count
        entry = projects[key]
        if "verify" in entry and "verify_count" not in entry:
            entry["verify_count"] = 1 if entry.pop("verify") else 0
            self._save()
        return cast(Dict[str, Any], projects[key])

    def _ticket_entry(self, ticket_id: str, *, create: bool = False) -> Dict[str, Any]:
        """Return (and optionally create) the dict for a ticket."""
        key = str(ticket_id)
        tickets = self._data.setdefault("tickets", {})
        if key not in tickets:
            if create:
                tickets[key] = {}
            else:
                return {}
        entry = tickets[key]
        # Migrate old string-only format: "ai" → {"gate": "ai"}
        if isinstance(entry, str):
            tickets[key] = {"gate": entry}
            self._save()  # persist so the migration doesn't re-run every restart
        # Migrate old bool verify → int verify_count
        entry = tickets[key]
        if "verify" in entry and "verify_count" not in entry:
            entry["verify_count"] = 1 if entry.pop("verify") else 0
            self._save()
        return cast(Dict[str, Any], tickets[key])

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        """Load settings from disk; return an empty structure on missing file."""
        if not self._path.exists():
            return {"projects": {}, "tickets": {}}
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return {"projects": {}, "tickets": {}}
            data.setdefault("projects", {})
            data.setdefault("tickets", {})
            return data
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read gate_settings.json: %s", exc)
            return {"projects": {}, "tickets": {}}

    def _save(self) -> None:
        """Write settings to disk.

        Writes to a temp file and ``os.replace``s it into place (matching
        ``ProjectSyncWorkflow._save_mapping``'s pattern) rather than
        writing ``self._path`` directly. This one file holds EVERY
        project's gate/verify/decompose settings, so a process killed
        mid-write (or a `json.dump` failure partway through, e.g. an
        unexpectedly non-serializable value) used to leave a
        truncated/invalid file behind — `_load`'s broad except-and-reset
        would then silently revert every project's settings to defaults
        on the next load, not just the one being written. `os.replace`
        is atomic on both POSIX and Windows, so a reader never observes
        a partially-written file.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = f"{self._path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
            os.replace(tmp_path, self._path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not write gate_settings.json: %s", exc)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
