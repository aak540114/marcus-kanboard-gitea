"""
Per-project ticket-movement statistics.

Tracks how many tickets move into the ``done`` and ``waiting_for_human``
Kanboard columns, per hour, per project — fed by the ``ticket.status_changed``
event bus subscription wired in ``src/marcus_mcp/server.py``
(``_track_project_stats``). Backs the ``/project-stats`` board-header page.

Tracking for a given project+column starts the first time a ticket is
ever moved there — there is no backfill, and hours with no movement are
simply absent from the stored data (see :meth:`ProjectStatsManager.
get_hourly_stats`), which is exactly "skip empty hours" for free.

Settings are persisted as a JSON file at::

    <data_dir>/project_stats.json

Schema::

    {
      "projects": {
        "7": {
          "done": {"2026-08-13T14:00": 3, "2026-08-13T16:00": 1},
          "waiting_for_human": {"2026-08-13T15:00": 2}
        }
      },
      "last_status": {"7:42": "done", "7:43": "in_progress"},
      "loc": {"7": 4820}
    }

``last_status`` is a ``"<project_id>:<ticket_id>"`` -> last-seen-status
map, used only to deduplicate repeated delivery of the same real-world
transition (see :meth:`ProjectStatsManager.record_status_change`'s
docstring) — it is never displayed. Keyed by project as well as ticket
so a ticket relocated to a different project via Kanboard's native
"move to another project" action is tracked as a fresh transition there.

``loc`` is each project's most recently computed total line count on its
``main`` branch (see :meth:`ProjectStatsManager.refresh_loc_count`), kept
up to date by ``server.py``'s ``_track_project_stats`` subscriber calling
it every time a ticket is freshly counted as moved to Done.

Classes
-------
ProjectStatsManager
    Records and queries per-project, per-hour ticket-movement counts.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = Path(os.getcwd()) / "data"

#: Kanboard column statuses this module tracks movement INTO. Matches
#: TaskStatus's values for DONE and WAITING_FOR_HUMAN.
TRACKED_STATUSES = ("done", "waiting_for_human")

#: Bucket key format — one bucket per hour, UTC.
_HOUR_FORMAT = "%Y-%m-%dT%H:00"

#: Git's well-known empty-tree object hash — identical in every git
#: repository. Diffing any commit against it makes every one of that
#: commit's tracked lines show up as an "insertion", which is exactly a
#: total line count — a lightweight substitute for a dedicated `cloc`
#: tool (none is available in this environment). Binary files are
#: reported separately by git ("Bin 0 -> N bytes") and are NOT counted
#: towards insertions, so this undercounts binary-heavy repos in exactly
#: the way a line-count metric should.
_EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

#: Bounded so a stalled network fetch can't hang the ticket.status_changed
#: handler indefinitely — matches this codebase's established
#: _GIT_CMD_TIMEOUT pattern (src/core/git_branch_manager.py).
_GIT_CMD_TIMEOUT = 30

_INSERTIONS_RE = re.compile(r"(\d+) insertion")


class ProjectStatsManager:
    """Records and queries per-project, per-hour ticket-movement counts.

    Parameters
    ----------
    data_dir : Optional[Path]
        Directory that contains ``project_stats.json``. Defaults to
        ``./data/`` relative to the Marcus working directory.
    """

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self._path = (data_dir or _DEFAULT_DATA_DIR) / "project_stats.json"
        self._data: Dict[str, Any] = self._load()
        # Serializes record_status_change end-to-end: the same ticket's
        # status-change event can arrive twice in close succession (the
        # poll path and the webhook path racing each other — see that
        # method's docstring), and this file is shared across every
        # project, so a read-modify-write-save without a lock could lose
        # an update if two calls interleave. Matches
        # ProjectSyncWorkflow.__init__'s equivalent instance-wide lock.
        self._lock = asyncio.Lock()

    async def record_status_change(
        self, project_id: int, ticket_id: str, new_status: str, when: datetime
    ) -> bool:
        """Record a ticket's column move, incrementing its hour's bucket
        if *new_status* is tracked.

        Deduplicates repeated delivery of the SAME transition: every
        webhook-signalled move re-fires once on the next BoardWatcher
        poll (both publish ``ticket.status_changed``), which would
        otherwise double-count every real move. Detected via
        ``last_status`` — the ticket's previously recorded status, keyed
        by ``"<project_id>:<ticket_id>"`` (NOT bare ``ticket_id``) — NOT
        via the event's own ``old_status`` field, which the webhook path
        never populates.

        The project_id is part of the key so that Kanboard's native "move
        task to another project" action (which keeps the same numeric
        task id) is correctly counted as a genuinely NEW transition for
        the destination project, rather than being swallowed as a
        duplicate because the ticket's last recorded status anywhere
        already happened to match. Without this, a ticket moved to Done
        in project A, then later relocated into project B and dropped
        straight into ITS Done column, would silently never be counted
        for project B (its last_status entry already said "done").

        Parameters
        ----------
        project_id : int
            Kanboard project the ticket belongs to.
        ticket_id : str
            Kanboard ticket id.
        new_status : str
            The status/column the ticket just moved to (a ``TaskStatus``
            value string, e.g. ``"done"``).
        when : datetime
            When the move happened (should be tz-aware UTC — the caller
            passes ``Event.timestamp``, which always is).

        Returns
        -------
        bool
            ``True`` if this call incremented an hourly bucket (a new,
            tracked transition); ``False`` for a duplicate delivery or an
            untracked status.
        """
        async with self._lock:
            last_status = self._data.setdefault("last_status", {})
            key = f"{project_id}:{ticket_id}"
            if last_status.get(key) == new_status:
                return False

            last_status[key] = new_status

            if new_status not in TRACKED_STATUSES:
                self._save()
                return False

            bucket = when.astimezone(timezone.utc).strftime(_HOUR_FORMAT)
            projects = self._data.setdefault("projects", {})
            project_entry = projects.setdefault(str(project_id), {})
            column_buckets = project_entry.setdefault(new_status, {})
            column_buckets[bucket] = column_buckets.get(bucket, 0) + 1
            self._save()
            return True

    def get_hourly_stats(self, project_id: int, status: str) -> List[Dict[str, Any]]:
        """Return this project's hourly counts for *status*, sorted
        chronologically, with hours that had no movement simply absent.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.
        status : str
            One of :data:`TRACKED_STATUSES`.

        Returns
        -------
        List[Dict[str, Any]]
            ``[{"hour": "2026-08-13T14:00", "count": 3}, ...]``, ordered
            oldest first. Empty if nothing has ever been recorded.
        """
        project_entry = (self._data.get("projects") or {}).get(str(project_id), {})
        buckets = project_entry.get(status) or {}
        return [
            {"hour": hour, "count": count} for hour, count in sorted(buckets.items())
        ]

    def get_last_hour_count(
        self, project_id: int, status: str, now: Optional[datetime] = None
    ) -> int:
        """Return the count for the CURRENT hour bucket, defaulting to 0.

        Unlike :meth:`get_hourly_stats` (which omits empty hours), this
        always returns a number — 0 is a meaningful, expected answer to
        "how many tickets moved to Done in the last hour".

        Parameters
        ----------
        project_id : int
            Kanboard project ID.
        status : str
            One of :data:`TRACKED_STATUSES`.
        now : Optional[datetime]
            Defaults to the current UTC time.

        Returns
        -------
        int
            The current hour's count, or 0.
        """
        now = now or datetime.now(timezone.utc)
        bucket = now.astimezone(timezone.utc).strftime(_HOUR_FORMAT)
        project_entry = (self._data.get("projects") or {}).get(str(project_id), {})
        return int((project_entry.get(status) or {}).get(bucket, 0))

    # ------------------------------------------------------------------
    # Lines of code (main branch)
    # ------------------------------------------------------------------

    async def refresh_loc_count(
        self, project_id: int, repo_path: str, branch: str = "main"
    ) -> Optional[int]:
        """Recompute and store a project's total line count on *branch*.

        Uses ``git diff --shortstat <empty-tree> origin/<branch>`` —
        never touches the working tree or HEAD of *repo_path*, so it's
        safe to call against the SAME shared local clone
        :class:`~src.core.git_branch_manager.BranchManager` uses for
        ticket branches, even while an agent has some other branch
        checked out there. Fetches ``origin/<branch>`` first so the
        count reflects the latest pushed commit, not whatever this local
        clone happened to have cached.

        Best-effort: any git failure (network down, branch doesn't
        exist, repo_path isn't a git repo) is logged and returns
        ``None`` without raising or touching the stored value — a
        transient failure just means the next Done-move retries it.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.
        repo_path : str
            Local clone path (a project's ``local_repo_path`` from
            :class:`~src.workflows.project_sync_workflow.ProjectSyncWorkflow`).
        branch : str
            Remote branch to measure. Defaults to ``"main"``, matching
            every other main-branch convention in this codebase
            (``BranchManagerConfig.main_branch``).

        Returns
        -------
        Optional[int]
            The computed line count, or ``None`` on failure.
        """
        try:
            rc, _out, err = await self._run_git(
                ["fetch", "origin", branch], cwd=repo_path
            )
            if rc != 0:
                logger.warning(
                    "Could not fetch %s for project %d LOC count: %s",
                    branch,
                    project_id,
                    err.strip(),
                )
                return None

            rc, out, err = await self._run_git(
                ["diff", "--shortstat", _EMPTY_TREE_SHA, f"origin/{branch}"],
                cwd=repo_path,
            )
            if rc != 0:
                logger.warning(
                    "Could not diff origin/%s for project %d LOC count: %s",
                    branch,
                    project_id,
                    err.strip(),
                )
                return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not compute LOC count for project %d: %s", project_id, exc
            )
            return None

        match = _INSERTIONS_RE.search(out)
        count = int(match.group(1)) if match else 0

        async with self._lock:
            loc = self._data.setdefault("loc", {})
            loc[str(project_id)] = count
            self._save()
        return count

    def get_loc_count(self, project_id: int) -> Optional[int]:
        """Return the last computed line count for a project, or ``None``
        if :meth:`refresh_loc_count` has never succeeded for it.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.

        Returns
        -------
        Optional[int]
            The stored line count, or ``None``.
        """
        val = (self._data.get("loc") or {}).get(str(project_id))
        return int(val) if val is not None else None

    async def _run_git(self, args: List[str], cwd: str) -> Tuple[int, str, str]:
        """Run a bounded git subprocess, returning (returncode, stdout, stderr).

        Never raises on a non-zero exit — callers inspect the returncode
        themselves (unlike ``gitea_manager._run_git``, which raises,
        because those callers all treat any failure as fatal; here a
        "no such branch" is an expected, handled case).
        """
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=_GIT_CMD_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return (
            proc.returncode or 0,
            stdout_bytes.decode(errors="replace"),
            stderr_bytes.decode(errors="replace"),
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        """Load stats from disk; return an empty structure on any error.

        Fails safe: a missing or corrupt file yields no history rather
        than raising — losing accumulated stats is far less harmful than
        crashing ticket-status-change handling.
        """
        if not self._path.exists():
            return {"projects": {}, "last_status": {}, "loc": {}}
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return {"projects": {}, "last_status": {}, "loc": {}}
            data.setdefault("projects", {})
            data.setdefault("last_status", {})
            data.setdefault("loc", {})
            return data
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read project_stats.json: %s", exc)
            return {"projects": {}, "last_status": {}, "loc": {}}

    def _save(self) -> None:
        """Persist stats to disk.

        Writes to a temp file and ``os.replace``s it into place (matching
        ``ProjectSyncWorkflow._save_mapping``'s pattern) rather than
        writing ``self._path`` directly, so a process killed mid-write
        never leaves a truncated file that ``_load`` would have to
        discard wholesale on the next run.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = f"{self._path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
            os.replace(tmp_path, self._path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not save project_stats.json: %s", exc)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
