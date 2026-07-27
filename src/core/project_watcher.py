"""ProjectWatcher — polls Kanboard for new projects and emits events.

Polls Kanboard's ``getAllProjects`` RPC method on a configurable interval.
When a project is seen for the first time (i.e. not in the persisted set of
known project IDs), a ``project.created`` event is published on the Marcus
Events bus.

Known project IDs are persisted to a JSON file on disk so the watcher
correctly skips already-known projects across Marcus restarts.

Usage
-----
::

    watcher = ProjectWatcher(
        kanboard_url="http://localhost:8080/jsonrpc.php",
        api_token="your-token",
        events=events,
        poll_interval=60.0,
        state_path="./data/known_projects.json",
    )
    await watcher.start()
    # ... later ...
    await watcher.stop()
"""

import asyncio
import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Set

import httpx

from src.core.events import Events

logger = logging.getLogger(__name__)


class ProjectWatcher:
    """Poll Kanboard for new projects and emit ``project.created`` events.

    Parameters
    ----------
    kanboard_url : str
        Full URL to the JSON-RPC endpoint.
    api_token : str
        Kanboard API token (password for the ``jsonrpc`` user).
    events : Events
        Marcus event bus to publish on.
    poll_interval : float
        Seconds between polls. Default 60.
    state_path : str
        Path to the JSON file storing known project IDs.
        Created automatically if absent.
    """

    def __init__(
        self,
        kanboard_url: str,
        api_token: str,
        events: Events,
        poll_interval: float = 60.0,
        state_path: str = "./data/known_projects.json",
        is_provisioned: Optional[Callable[[int], bool]] = None,
    ) -> None:
        """Initialise the watcher.

        Parameters
        ----------
        is_provisioned : Optional[Callable[[int], bool]]
            Predicate returning ``True`` when a project already has its
            downstream resource (a Gitea repo mapping). When supplied, it —
            not the persisted "known" set — decides whether to emit:
            ``project.created`` is re-emitted every poll until the project
            is provisioned, so a FAILED creation (e.g. a bad Gitea token
            scope returning 403) is retried instead of being skipped
            forever after the first sighting. Without it, the legacy
            emit-once-per-id behaviour applies.
        """
        self._rpc_url = kanboard_url
        self._auth = httpx.BasicAuth("jsonrpc", api_token)
        self._events = events
        self._poll_interval = poll_interval
        self._state_path = state_path
        self._is_provisioned = is_provisioned

        self._known_ids: Set[int] = self._load_known_ids()
        self._task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._running = False
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the polling loop as a background asyncio task."""
        if self._running:
            return
        self._running = True
        self._client = httpx.AsyncClient(timeout=30.0)
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "ProjectWatcher started (interval=%.0fs, state=%s)",
            self._poll_interval,
            self._state_path,
        )

    async def stop(self) -> None:
        """Stop the polling loop and close the HTTP client."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("ProjectWatcher stopped")

    # ------------------------------------------------------------------
    # Core polling
    # ------------------------------------------------------------------

    async def poll_once(self) -> None:
        """Execute a single poll of Kanboard's ``getAllProjects``.

        Emits ``project.created`` for any project ID not seen before,
        then persists the updated known-ID set.
        """
        projects = await self._fetch_projects()
        if projects is None:
            return

        for project in projects:
            pid = int(project.get("id", 0))
            if not pid:
                continue
            if self._needs_emit(pid):
                await self._emit_project_created(project)
            self._known_ids.add(pid)

        self._save_known_ids()

    def _needs_emit(self, pid: int) -> bool:
        """Whether ``project.created`` should be emitted for *pid* now.

        With an ``is_provisioned`` predicate, emit whenever the project is
        NOT yet provisioned — so a failed downstream creation is retried
        each poll until it succeeds. Without one, fall back to
        emit-once-per-id via the persisted known set.
        """
        if self._is_provisioned is not None:
            return not self._is_provisioned(pid)
        return pid not in self._known_ids

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Infinite loop; calls :meth:`poll_once` then sleeps."""
        while self._running:
            try:
                await self.poll_once()
            except Exception as exc:  # noqa: BLE001
                logger.error("ProjectWatcher poll error: %s", exc)
            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break

    async def _fetch_projects(self) -> Optional[List[Dict[str, Any]]]:
        """Call Kanboard's ``getAllProjects`` RPC.

        Returns
        -------
        Optional[List[Dict[str, Any]]]
            List of project dicts, or None on error.
        """
        if self._client is None:
            return None
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": "getAllProjects",
            "id": 1,
            "params": {},
        }
        try:
            r = await self._client.post(
                self._rpc_url, json=payload, auth=self._auth
            )
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                logger.error("Kanboard RPC error: %s", body["error"])
                return None
            return body.get("result") or []
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to fetch Kanboard projects: %s", exc)
            return None

    async def notify_project(self, project_id: int) -> bool:
        """Provision a single project immediately, out of band with the poll.

        This is the push counterpart to :meth:`poll_once`. The Kanboard
        plugin calls Marcus the instant a project page loads, so Marcus can
        create the Gitea repo (and reconcile columns) right away instead of
        waiting for the next background poll. It fetches just that one
        project and emits ``project.created`` only when the project still
        :meth:`_needs_emit` — so repeated pokes for an already-provisioned
        project are cheap no-ops, and an un-provisioned one is retried.

        Parameters
        ----------
        project_id : int
            The Kanboard project id to provision now. Non-positive ids are
            rejected without any RPC call.

        Returns
        -------
        bool
            ``True`` if a ``project.created`` event was emitted for this
            call, ``False`` otherwise (already provisioned, unknown id, no
            HTTP client yet, or the project could not be fetched).
        """
        if project_id <= 0:
            return False

        project = await self._fetch_project(project_id)
        if project is None:
            return False

        pid = int(project.get("id", 0))
        if not pid:
            return False

        emitted = False
        if self._needs_emit(pid):
            await self._emit_project_created(project)
            emitted = True
        self._known_ids.add(pid)
        self._save_known_ids()
        return emitted

    async def _fetch_project(self, project_id: int) -> Optional[Dict[str, Any]]:
        """Call Kanboard's ``getProjectById`` RPC for a single project.

        Parameters
        ----------
        project_id : int
            The Kanboard project id to look up.

        Returns
        -------
        Optional[Dict[str, Any]]
            The project dict, or ``None`` on any error, when the HTTP
            client is not running, or when Kanboard reports no such
            project (``getProjectById`` returns ``null``/``false``).
        """
        if self._client is None:
            return None
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": "getProjectById",
            "id": 1,
            "params": {"project_id": project_id},
        }
        try:
            r = await self._client.post(
                self._rpc_url, json=payload, auth=self._auth
            )
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                logger.error("Kanboard RPC error: %s", body["error"])
                return None
            result = body.get("result")
            # getProjectById returns null/false for an unknown id.
            if not isinstance(result, dict):
                return None
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to fetch Kanboard project %d: %s", project_id, exc
            )
            return None

    async def _emit_project_created(self, project: Dict[str, Any]) -> None:
        """Publish a ``project.created`` event.

        Parameters
        ----------
        project : Dict[str, Any]
            Raw Kanboard project dict.
        """
        pid = int(project.get("id", 0))
        name = project.get("name", "")
        description = project.get("description", "")
        logger.info("New Kanboard project detected: %d — %s", pid, name)
        await self._events.publish(
            "project.created",
            source="project_watcher",
            data={
                "kanboard_project_id": pid,
                "project_name": name,
                "project_description": description,
            },
        )

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_known_ids(self) -> Set[int]:
        """Load known project IDs from disk.

        Returns
        -------
        Set[int]
            Previously seen project IDs (empty set if file absent).
        """
        if not os.path.exists(self._state_path):
            return set()
        try:
            with open(self._state_path) as f:
                data = json.load(f)
            return set(int(x) for x in data.get("known_ids", []))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load project watcher state: %s", exc)
            return set()

    def _save_known_ids(self) -> None:
        """Persist known project IDs to disk atomically.

        Writes to a temp file and ``os.replace``s it into place so a crash
        or OOM-kill mid-write can never leave a truncated file. A truncated
        file parses as empty on the next load, which — in the legacy
        (no-``is_provisioned``) path — makes the watcher re-emit
        ``project.created`` for every project on the board, re-triggering all
        subscribers (repo creation, column reconciliation).
        """
        os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"known_ids": sorted(self._known_ids)}, f)
            os.replace(tmp, self._state_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not save project watcher state: %s", exc)
