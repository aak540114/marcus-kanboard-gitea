"""
ProjectCloneWorkflow — "Clone this project" full baseline replication.

Creates a brand-new Kanboard project under a human-supplied name and
replicates a baseline project's entire visible state into it: every
ticket (title, description, column/status, labels, dependency/relation
links), the project description document, gate/verify/decompose and
project-access settings, and the underlying git repository (every
branch, under its original name, via a mirror clone).

Reuses existing singletons rather than constructing parallel instances
that could drift out of sync — the same instances
:class:`~src.workflows.human_gated_workflow.HumanGatedWorkflow` and the
``/api/*`` routes already use for the SAME kanban client, lifecycle
manager, and settings managers (see
:func:`src.marcus_mcp.server._get_gate_settings_mgr`'s docstring for why
that matters).

Error handling philosophy: best-effort, no rollback. If one step fails
(e.g. one ticket's link recreation errors), the failure is recorded in
:attr:`CloneResult.warnings` and the clone continues with the remaining
tickets/steps rather than deleting the partially-created project — a
partially-successful clone may still be useful, and automatic deletion
of a project a human just asked to create is a destructive action this
codebase avoids without explicit confirmation (see CLAUDE.md's
DATABASE_SAFETY section).

Classes
-------
CloneResult
    Outcome of a :meth:`ProjectCloneWorkflow.clone_project` run.
ProjectCloneWorkflow
    Orchestrates the end-to-end clone.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Set

from src.core.models import TaskStatus
from src.core.ticket_lifecycle import InvalidTransitionError, TicketState
from src.integrations.providers.kanboard_kanban import resolve_link_type

logger = logging.getLogger(__name__)

#: Kanboard column name Marcus uses for each task status — mirrors
#: MARCUS_DEFAULT_COLUMNS in kanboard_kanban.py. Keyed by TaskStatus (the
#: enum a Task's .status field actually carries) — NOT TicketState, a
#: separate enum from ticket_lifecycle.py with the same member names but
#: a different identity, so a TicketState-keyed dict would silently never
#: match here. A brand-new project created via createProject() starts
#: with Kanboard's own stock columns; move_task_to_column()'s existing
#: reconciliation escalation (see its _resolve_column_for_move docstring)
#: auto-runs ensure_columns() the first time a named column isn't found,
#: so no separate provisioning call is needed here.
_STATUS_TO_COLUMN: Dict[TaskStatus, str] = {
    TaskStatus.TODO: "Todo",
    TaskStatus.READY: "Ready",
    TaskStatus.IN_PROGRESS: "In Progress",
    TaskStatus.BLOCKED: "Blocked",
    TaskStatus.WAITING_FOR_HUMAN: "Waiting for Human",
    TaskStatus.DONE: "Done",
}

#: Legal AI-transition hops (mirrors TicketLifecycleManager._AI_TRANSITIONS'
#: TODO->READY->IN_PROGRESS->{target} shape) used to walk a freshly created
#: lifecycle record to the same state as its baseline ticket. Generalizes
#: HumanGatedWorkflow._park_in_waiting_for_human's single-target walk to
#: any of the non-terminal targets a baseline ticket can be found in.
_STATE_PATH: Dict[TicketState, List[TicketState]] = {
    TicketState.READY: [TicketState.READY],
    TicketState.IN_PROGRESS: [TicketState.READY, TicketState.IN_PROGRESS],
    TicketState.WAITING_FOR_HUMAN: [
        TicketState.READY,
        TicketState.IN_PROGRESS,
        TicketState.WAITING_FOR_HUMAN,
    ],
    TicketState.BLOCKED: [
        TicketState.READY,
        TicketState.IN_PROGRESS,
        TicketState.BLOCKED,
    ],
    TicketState.DONE: [
        TicketState.READY,
        TicketState.IN_PROGRESS,
        TicketState.DONE,
    ],
}


@dataclass
class CloneResult:
    """Outcome of a :meth:`ProjectCloneWorkflow.clone_project` run.

    Parameters
    ----------
    new_project_id : int
        The newly created Kanboard project's id.
    ticket_id_map : Dict[str, str]
        Baseline ticket id -> new (cloned) ticket id, in baseline
        creation order.
    warnings : List[str]
        Non-fatal issues hit along the way. The clone still completed as
        far as possible around each one — see the module docstring's
        error-handling philosophy.
    """

    new_project_id: int
    ticket_id_map: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


class ProjectCloneWorkflow:
    """Orchestrates cloning a baseline Kanboard project under a new name.

    Parameters
    ----------
    kanban : Any
        Connected ``KanboardKanban`` instance (the shared
        ``server.kanban_client``).
    lifecycle : Any
        Shared ``TicketLifecycleManager`` (``human_gated_workflow._lifecycle``).
    human_gated_workflow : Any
        Shared ``HumanGatedWorkflow`` — used only for
        :meth:`~src.workflows.human_gated_workflow.HumanGatedWorkflow.
        branch_manager_for_repo`, to seed a cloned in-flight ticket's
        branch without constructing a second, divergent BranchManager
        cache.
    project_sync : Any
        Shared ``ProjectSyncWorkflow``.
    gate_settings : Any
        Shared ``GateSettingManager``.
    project_access : Any
        Shared ``ProjectAccessSettingManager``.
    project_description : Any
        Shared ``ProjectDescriptionManager``.
    provider : str
        Kanban provider name tickets are tracked under in the lifecycle
        manager. Defaults to ``"kanboard"`` — the only provider this
        feature (and the board UI it's triggered from) supports.
    """

    def __init__(
        self,
        kanban: Any,
        lifecycle: Any,
        human_gated_workflow: Any,
        project_sync: Any,
        gate_settings: Any,
        project_access: Any,
        project_description: Any,
        provider: str = "kanboard",
    ) -> None:
        self._kanban = kanban
        self._lifecycle = lifecycle
        self._human_gated = human_gated_workflow
        self._project_sync = project_sync
        self._gate_settings = gate_settings
        self._project_access = project_access
        self._project_description = project_description
        self._provider = provider

    async def clone_project(self, baseline_project_id: int, new_name: str) -> CloneResult:
        """Create a new project and replicate *baseline_project_id* into it.

        Parameters
        ----------
        baseline_project_id : int
            Kanboard id of the project to clone.
        new_name : str
            Name for the new project (human-supplied).

        Returns
        -------
        CloneResult
            The new project's id, its ticket id mapping, and any
            non-fatal warnings from steps that partially failed.

        Raises
        ------
        Exception
            Only if the new project itself cannot be created — every
            step after that is best-effort (see module docstring).
        """
        warnings: List[str] = []

        new_project_id = await self._kanban.create_project(new_name)
        logger.info(
            "Cloning project %d -> new project %d (%s)",
            baseline_project_id,
            new_project_id,
            new_name,
        )

        await self._clone_repo(baseline_project_id, new_project_id, new_name, warnings)
        await self._clone_description(baseline_project_id, new_project_id, warnings)
        self._clone_settings(baseline_project_id, new_project_id)

        ticket_id_map = await self._clone_tickets(baseline_project_id, new_project_id, warnings)
        await self._clone_links(ticket_id_map, warnings)
        await self._seed_lifecycle(new_project_id, ticket_id_map, warnings)

        return CloneResult(
            new_project_id=new_project_id, ticket_id_map=ticket_id_map, warnings=warnings
        )

    # ------------------------------------------------------------------
    # Individual clone steps
    # ------------------------------------------------------------------

    async def _clone_repo(
        self,
        baseline_project_id: int,
        new_project_id: int,
        new_name: str,
        warnings: List[str],
    ) -> None:
        """Mirror-clone the baseline project's git repo into a new one.

        Skips (with a warning) if the baseline project has no repo
        mapping yet — a project a human created but never triggered any
        git activity on. Failure to clone does not abort the rest of the
        clone (tickets/description/settings still get replicated).
        """
        baseline_mapping = self._project_sync.get_repo_for_project(baseline_project_id)
        if not baseline_mapping or not baseline_mapping.get("gitea_repo_url"):
            warnings.append(
                f"Baseline project {baseline_project_id} has no git repo — "
                "skipping repository clone."
            )
            return
        try:
            result = await self._project_sync.ensure_repo_from_source(
                new_project_id,
                new_name,
                baseline_mapping["gitea_repo_url"],
            )
        except Exception as exc:  # noqa: BLE001
            result = None
            warnings.append(f"Failed to clone git repository: {exc}")
            return
        if result is None:
            warnings.append("Failed to clone git repository (see server logs).")

    async def _clone_description(
        self, baseline_project_id: int, new_project_id: int, warnings: List[str]
    ) -> None:
        """Copy the baseline project's description document verbatim.

        Copies the baseline's ACTUAL provenance (:meth:`get_source`) onto
        the clone rather than resetting it to a fresh template — a
        human-locked baseline description (``SOURCE_HUMAN``, which blocks
        further auto-updates) stays locked on the clone too, matching
        "everything else exactly same."
        """
        try:
            text = self._project_description.get_description(baseline_project_id)
            if text is None:
                return  # baseline has no description yet — nothing to copy
            source = self._project_description.get_source(baseline_project_id)
            self._project_description.update_description(new_project_id, text, source=source)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Failed to copy project description: {exc}")

    def _clone_settings(self, baseline_project_id: int, new_project_id: int) -> None:
        """Copy gate mode, verify count, decompose-enabled, and project
        access settings. Each is copied only if the baseline has an
        EXPLICIT setting — an unconfigured baseline setting means the new
        project should also fall back to Marcus's hard default, not
        inherit an explicit copy of that default (which would then
        survive a future change to the hard default itself)."""
        gate = self._gate_settings.get_project_gate(baseline_project_id)
        if gate is not None:
            self._gate_settings.set_project_gate(new_project_id, gate)

        verify_count = self._gate_settings.get_project_verify_count(baseline_project_id)
        if verify_count is not None:
            self._gate_settings.set_project_verify_count(new_project_id, verify_count)

        decompose_enabled = self._gate_settings.get_project_decompose_enabled(
            baseline_project_id
        )
        if decompose_enabled is not None:
            self._gate_settings.set_project_decompose_enabled(
                new_project_id, decompose_enabled
            )

        access_enabled = self._project_access.get_project_enabled(baseline_project_id)
        if access_enabled is not None:
            self._project_access.set_project_enabled(new_project_id, access_enabled)

    async def _clone_tickets(
        self, baseline_project_id: int, new_project_id: int, warnings: List[str]
    ) -> Dict[str, str]:
        """Recreate every baseline ticket, preserving title, description,
        priority, column/status, assignee, and labels.

        Returns
        -------
        Dict[str, str]
            Baseline ticket id -> new ticket id, in baseline order (by
            numeric id ascending) — later steps (link recreation,
            lifecycle seeding) rely on this map.
        """
        baseline_tasks = await self._kanban.get_tasks_for_project(baseline_project_id)
        baseline_tasks.sort(key=lambda t: int(t.id))

        id_map: Dict[str, str] = {}
        for task in baseline_tasks:
            try:
                new_task = await self._kanban.create_task(
                    {
                        "project_id": new_project_id,
                        "name": task.name,
                        "description": task.description,
                        "priority": task.priority,
                        "estimated_hours": task.estimated_hours,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(
                    f"Failed to clone ticket {task.id} ({task.name!r}): {exc}"
                )
                continue

            id_map[task.id] = new_task.id

            column_name = _STATUS_TO_COLUMN.get(task.status, "Todo")
            try:
                await self._kanban.move_task_to_column(new_task.id, column_name)
            except Exception as exc:  # noqa: BLE001
                warnings.append(
                    f"Failed to move cloned ticket {new_task.id} to "
                    f"{column_name!r}: {exc}"
                )

            if task.labels:
                try:
                    await self._kanban.set_task_tags(
                        new_task.id, project_id=new_project_id, tags=list(task.labels)
                    )
                except Exception as exc:  # noqa: BLE001
                    warnings.append(
                        f"Failed to set tags on cloned ticket {new_task.id}: {exc}"
                    )

            if task.assigned_to:
                try:
                    await self._kanban.assign_task(new_task.id, task.assigned_to)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(
                        f"Failed to assign cloned ticket {new_task.id}: {exc}"
                    )

        return id_map

    async def _clone_links(self, id_map: Dict[str, str], warnings: List[str]) -> None:
        """Recreate dependency/relation links between cloned tickets.

        Only links whose BOTH endpoints were cloned (present in
        *id_map*) are recreated. Kanboard's ``getAllTaskLinks`` returns
        each link from both endpoints' perspective, so each unordered
        pair is created exactly once (skipping the pair once seen)
        rather than twice — matches ``create_task_link``'s own behavior
        of auto-creating the opposite-direction link on the other task.
        """
        if not id_map:
            return

        label_map: Dict[str, int] = {}
        try:
            label_map = await self._kanban.get_link_type_map()
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                f"Could not fetch link-type map, link types will fall back "
                f"to defaults: {exc}"
            )

        seen_pairs: Set[FrozenSet[str]] = set()
        for old_id, new_id in id_map.items():
            try:
                raw_links = await self._kanban.get_raw_task_links(old_id)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Failed to fetch links for ticket {old_id}: {exc}")
                continue

            for raw_link in raw_links:
                other_old_id = str(raw_link.get("task_id", ""))
                other_new_id = id_map.get(other_old_id)
                if other_new_id is None:
                    continue  # the linked ticket wasn't (successfully) cloned
                pair_key = frozenset({old_id, other_old_id})
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                link_type = resolve_link_type(raw_link, label_map)
                try:
                    await self._kanban.create_task_link(new_id, other_new_id, link_type)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(
                        f"Failed to recreate link between cloned tickets "
                        f"{new_id} and {other_new_id}: {exc}"
                    )

    async def _seed_lifecycle(
        self, new_project_id: int, id_map: Dict[str, str], warnings: List[str]
    ) -> None:
        """Walk each cloned ticket's lifecycle record to match its
        baseline ticket's current state, and seed a branch for any
        IN_PROGRESS clone.

        Only AI-initiated transitions (:meth:`TicketLifecycleManager.
        transition`) are used — never ``human_transition``, which
        explicitly forbids ``WAITING_FOR_HUMAN`` as a target. Once seeded
        at IN_PROGRESS with ``ai_agent_id`` left unset,
        ``HumanGatedWorkflow``'s existing unmodified "first-sight
        recovery" polling logic picks the ticket back up on its own — no
        new pickup code needed here (see module docstring).
        """
        new_repo_path: Optional[str] = None
        mapping = self._project_sync.get_repo_for_project(new_project_id)
        if mapping:
            new_repo_path = mapping.get("local_repo_path")

        for old_id, new_id in id_map.items():
            old_record = self._lifecycle.get(old_id, self._provider)
            if old_record is None:
                continue  # never touched by AI — the clone starts untouched too

            path = _STATE_PATH.get(old_record.state)
            if path is None:
                continue  # TODO / DONE-terminal / REOPENED — nothing to walk to

            new_record = self._lifecycle.get_or_create(new_id, self._provider)
            for hop in path:
                try:
                    self._lifecycle.transition(
                        new_id,
                        self._provider,
                        hop,
                        reason=f"Cloned from ticket {old_id}",
                    )
                except (InvalidTransitionError, KeyError) as exc:
                    warnings.append(
                        f"Could not fully seed lifecycle state for cloned "
                        f"ticket {new_id}: {exc}"
                    )
                    break

            if old_record.state == TicketState.IN_PROGRESS:
                if not new_repo_path:
                    warnings.append(
                        f"Cloned ticket {new_id} was in progress on its "
                        "baseline, but the new project has no git repo — "
                        "skipping branch seed."
                    )
                    continue
                try:
                    branch_mgr = self._human_gated.branch_manager_for_repo(new_repo_path)
                    await branch_mgr.create_branch(
                        new_record.branch_name, from_branch=old_record.branch_name
                    )
                except Exception as exc:  # noqa: BLE001
                    warnings.append(
                        f"Failed to seed branch for cloned ticket {new_id}: {exc}"
                    )
