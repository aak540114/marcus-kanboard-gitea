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
from typing import Any, Dict, List, Optional, Set

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
    ) -> None:
        """Initialise the watcher."""
        self._rpc_url = kanboard_url
        self._auth = httpx.BasicAuth("jsonrpc", api_token)
        self._events = events
        self._poll_interval = poll_interval
        self._state_path = state_path

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
            if pid and pid not in self._known_ids:
                self._known_ids.add(pid)
                await self._emit_project_created(project)

        self._save_known_ids()

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
        """Persist known project IDs to disk."""
        os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
        try:
            with open(self._state_path, "w") as f:
                json.dump({"known_ids": sorted(self._known_ids)}, f)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not save project watcher state: %s", exc)
