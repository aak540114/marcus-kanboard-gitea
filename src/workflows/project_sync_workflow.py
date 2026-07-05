"""ProjectSyncWorkflow — syncs Kanboard projects to GitLab repositories.

Subscribes to the ``project.created`` event emitted by ``ProjectWatcher``
and reacts by:

1. Creating a corresponding GitLab repository (slugified from the project name).
2. Initialising it with a README and pushing the first commit.
3. Persisting the Kanboard-project → GitLab-repo mapping to
   ``./data/project_repos.json`` so that ``HumanGatedWorkflow`` can look
   up the correct git remote when creating ticket branches.

Usage
-----
::

    sync = ProjectSyncWorkflow(
        gitlab_manager=gitlab_mgr,
        events=events,
        repos_path="./data/project_repos.json",
        local_repos_base="./repos",
    )
    sync.subscribe()          # wire up the event subscription
    # ProjectWatcher is started separately

Mapping file format (``project_repos.json``)
--------------------------------------------
::

    {
      "kanboard:1": {
        "kanboard_project_id": 1,
        "kanboard_project_name": "Shopping Cart",
        "gitlab_repo_url": "http://localhost:8929/root/shopping-cart.git",
        "local_repo_path": "./repos/shopping-cart"
      }
    }
"""

import json
import logging
import os
from typing import Any, Dict, Optional

from src.core.events import Events
from src.integrations.gitlab_manager import GitLabManager, _slugify

logger = logging.getLogger(__name__)


class ProjectSyncWorkflow:
    """Sync Kanboard projects to GitLab repositories.

    Parameters
    ----------
    gitlab_manager : GitLabManager
        Connected GitLab manager instance.
    events : Events
        Marcus event bus.
    repos_path : str
        Path to the project-repo mapping JSON file.
    local_repos_base : str
        Directory under which local git clones are created.
    """

    def __init__(
        self,
        gitlab_manager: GitLabManager,
        events: Events,
        repos_path: str = "./data/project_repos.json",
        local_repos_base: str = "./repos",
    ) -> None:
        """Initialise the workflow."""
        self._gitlab = gitlab_manager
        self._events = events
        self._repos_path = repos_path
        self._local_repos_base = local_repos_base
        self._mapping: Dict[str, Dict[str, Any]] = self._load_mapping()

    # ------------------------------------------------------------------
    # Event wiring
    # ------------------------------------------------------------------

    def subscribe(self) -> None:
        """Subscribe to ``project.created`` events."""
        self._events.subscribe("project.created", self._on_project_created)
        logger.info("ProjectSyncWorkflow subscribed to project.created")

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    async def _on_project_created(self, event: Any) -> None:
        """Handle a new Kanboard project by creating a GitLab repo.

        Parameters
        ----------
        event : Event
            Marcus event with ``data.kanboard_project_id``,
            ``data.project_name``, ``data.project_description``.
        """
        data = event.data
        pid = int(data.get("kanboard_project_id", 0))
        name = data.get("project_name", f"project-{pid}")
        description = data.get("project_description", "")

        key = f"kanboard:{pid}"
        if key in self._mapping:
            logger.debug("Project %d already mapped — skipping", pid)
            return

        slug = _slugify(name)
        local_path = os.path.join(self._local_repos_base, slug)

        try:
            clone_url = await self._gitlab.create_repo(name, description)
            await self._gitlab.init_with_readme(clone_url, local_path)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to create GitLab repo for project %d (%s): %s",
                pid,
                name,
                exc,
            )
            return

        self._mapping[key] = {
            "kanboard_project_id": pid,
            "kanboard_project_name": name,
            "gitlab_repo_url": clone_url,
            "local_repo_path": local_path,
        }
        self._save_mapping()
        logger.info(
            "Project %d (%s) → GitLab %s (local: %s)",
            pid,
            name,
            clone_url,
            local_path,
        )

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    def get_repo_for_project(
        self, kanboard_project_id: int
    ) -> Optional[Dict[str, Any]]:
        """Return the repo mapping for a Kanboard project.

        Parameters
        ----------
        kanboard_project_id : int
            Kanboard project ID.

        Returns
        -------
        Optional[Dict[str, Any]]
            Dict with ``gitlab_repo_url`` and ``local_repo_path``, or None.
        """
        return self._mapping.get(f"kanboard:{kanboard_project_id}")

    def all_mappings(self) -> Dict[str, Dict[str, Any]]:
        """Return all project → repo mappings.

        Returns
        -------
        Dict[str, Dict[str, Any]]
            Full mapping dict.
        """
        return {k: dict(v) for k, v in self._mapping.items()}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_mapping(self) -> Dict[str, Dict[str, Any]]:
        """Load the project-repo mapping from disk.

        Returns
        -------
        Dict[str, Dict[str, Any]]
            Persisted mapping (empty dict if file absent).
        """
        if not os.path.exists(self._repos_path):
            return {}
        try:
            with open(self._repos_path) as f:
                data: Dict[str, Dict[str, Any]] = json.load(f)
            return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load project repos mapping: %s", exc)
            return {}

    def _save_mapping(self) -> None:
        """Persist the project-repo mapping to disk."""
        os.makedirs(os.path.dirname(self._repos_path) or ".", exist_ok=True)
        try:
            with open(self._repos_path, "w") as f:
                json.dump(self._mapping, f, indent=2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not save project repos mapping: %s", exc)
