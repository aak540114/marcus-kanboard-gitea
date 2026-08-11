"""ProjectSyncWorkflow — syncs Kanboard projects to Gitea repositories.

Subscribes to the ``project.created`` event emitted by ``ProjectWatcher``
and reacts by:

1. Creating a corresponding Gitea repository (slugified from the project name).
2. Initialising it with a README and pushing the first commit.
3. Persisting the Kanboard-project → Gitea-repo mapping to
   ``./data/project_repos.json`` so that ``HumanGatedWorkflow`` can look
   up the correct git remote when creating ticket branches.

Usage
-----
::

    sync = ProjectSyncWorkflow(
        gitea_manager=gitea_mgr,
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
        "gitea_repo_url": "http://localhost:3000/root/shopping-cart.git",
        "local_repo_path": "./repos/shopping-cart"
      }
    }
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

from src.core.events import Events
from src.integrations.gitea_manager import GiteaManager, _slugify

logger = logging.getLogger(__name__)


class ProjectSyncWorkflow:
    """Sync Kanboard projects to Gitea repositories.

    Parameters
    ----------
    gitea_manager : GiteaManager
        Connected Gitea manager instance.
    events : Events
        Marcus event bus.
    repos_path : str
        Path to the project-repo mapping JSON file.
    local_repos_base : str
        Directory under which local git clones are created.
    """

    def __init__(
        self,
        gitea_manager: GiteaManager,
        events: Events,
        repos_path: str = "./data/project_repos.json",
        local_repos_base: str = "./repos",
        webhook_target_url: Optional[str] = None,
        webhook_secret: Optional[str] = None,
    ) -> None:
        """Initialise the workflow.

        Parameters
        ----------
        webhook_target_url : Optional[str]
            URL Gitea should POST push events to (Marcus's
            ``/webhooks/gitea`` endpoint). When set together with
            ``webhook_secret``, every repo provisioned by ``ensure_repo``
            automatically gets a push webhook — no manual setup required.
        webhook_secret : Optional[str]
            Shared HMAC secret for signing webhook deliveries.
        """
        self._gitea = gitea_manager
        self._events = events
        self._repos_path = repos_path
        self._local_repos_base = local_repos_base
        self._webhook_target_url = webhook_target_url
        self._webhook_secret = webhook_secret
        self._mapping: Dict[str, Dict[str, Any]] = self._load_mapping()
        # Serializes ensure_repo end-to-end — see that method's docstring
        # for the races this closes (duplicate repo creation for the same
        # project, and cross-project slug-collision disambiguation racing
        # two projects' first-time provisioning). Repo provisioning is a
        # rare, one-time-per-project event, not a hot path, so full
        # serialization across projects is an acceptable trade for
        # correctness here — matches BranchManager's equivalent
        # per-instance lock fully serializing its own git operations.
        self._lock = asyncio.Lock()

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
        """Handle a new Kanboard project by creating a Gitea repo.

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
        await self.ensure_repo(pid, name, description)

    # ------------------------------------------------------------------
    # On-demand provisioning
    # ------------------------------------------------------------------

    async def ensure_repo(
        self, project_id: int, project_name: str, description: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Return (creating if necessary) the Gitea repo for a project.

        Idempotent — safe to call every time a ticket's project is looked
        up, not just from the ``project.created`` event handler (which
        nothing in Marcus currently publishes). If a mapping already
        exists it is returned as-is without any network calls.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.
        project_name : str
            Human-readable project name (slugified for the Gitea repo).
        description : str
            Optional project description, used as the Gitea repo
            description on first creation.

        Returns
        -------
        Optional[Dict[str, Any]]
            The project's repo mapping, or None if repo creation failed.

        Notes
        -----
        The whole method runs under ``self._lock``, serializing all calls
        on this instance. Two real races this closes:

        1. Two concurrent first-time calls for the SAME project (e.g. two
           agents' first ``get_work_context`` landing near-simultaneously)
           could both see ``key not in self._mapping``, both call
           ``create_repo``/``init_with_readme`` — either producing two
           competing writes (one caller's ``raise_for_status()`` surfacing
           a spurious failure for a repo that really was created), or, if
           the second caller's create lands after the first's, both
           running ``git init``/``add``/``commit``/``push`` concurrently
           against the SAME ``local_path`` working directory.
        2. Two concurrent first-time calls for DIFFERENT projects whose
           names slugify to the same string (e.g. "My App" and "my-app!")
           both read ``self._mapping`` for ``taken_slugs`` before either
           had written its entry, so both compute the identical
           un-disambiguated slug and can end up cross-wired onto one
           Gitea repo and one local clone — exactly what the
           ``taken_slugs`` check below exists to prevent, but which is
           itself racy without this lock.

        Repo provisioning is a rare, one-time-per-project event, not a
        hot path, so serializing across DIFFERENT projects too (not just
        same-project calls) is an acceptable trade for closing both races
        with one simple lock.
        """
        async with self._lock:
            key = f"kanboard:{project_id}"
            if key in self._mapping:
                cached = self._mapping[key]
                if not cached.get("webhook_created"):
                    # The repo may have been provisioned before
                    # GITEA_WEBHOOK_TOKEN was set (or before Marcus was
                    # restarted with it), or a prior attempt may have failed —
                    # retry on every lookup until it's confirmed, rather than
                    # leaving this project permanently without a webhook.
                    # Prefer the stored slug: for disambiguated or empty-name
                    # repos, re-slugifying the project name yields the WRONG
                    # repo. Fall back to name-derived for pre-repo_slug files.
                    slug = cached.get("repo_slug") or _slugify(
                        cached.get("kanboard_project_name") or project_name
                    )
                    cached["webhook_created"] = await self._ensure_webhook(slug)
                    self._save_mapping()
                logger.debug(
                    "Project %d already mapped — skipping repo creation", project_id
                )
                return dict(cached)

            slug = _slugify(project_name)
            # Disambiguate before creating: create_repo() treats "repo already
            # exists" as "already provisioned" and returns the existing repo's
            # URL, so a slug collision with a DIFFERENT project ("My App" vs
            # "my app!") would silently cross-wire both projects into one repo
            # and one local clone — both projects' ticket branches merging
            # into one main, with no error anywhere. An all-symbol name
            # slugifies to "" and would permanently fail provisioning instead.
            taken_slugs = {
                os.path.basename(m.get("local_repo_path", ""))
                for k, m in self._mapping.items()
                if k != key
            }
            if not slug:
                slug = f"project-{project_id}"
            elif slug in taken_slugs:
                logger.warning(
                    "Project %d (%r) slugifies to %r, already used by another "
                    "project — disambiguating to %r",
                    project_id,
                    project_name,
                    slug,
                    f"{slug}-p{project_id}",
                )
                slug = f"{slug}-p{project_id}"
            local_path = os.path.join(self._local_repos_base, slug)

            try:
                # Pass the (possibly disambiguated) slug, not the raw name —
                # create_repo slugifies its argument, and _slugify is
                # idempotent on an already-slugified string.
                clone_url = await self._gitea.create_repo(slug, description)
                await self._gitea.init_with_readme(clone_url, local_path)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to create Gitea repo for project %d (%s): %s",
                    project_id,
                    project_name,
                    exc,
                )
                return None

            webhook_created = await self._ensure_webhook(slug)

            self._mapping[key] = {
                "kanboard_project_id": project_id,
                "kanboard_project_name": project_name,
                "repo_slug": slug,
                "gitea_repo_url": clone_url,
                "local_repo_path": local_path,
                "webhook_created": webhook_created,
            }
            self._save_mapping()
            logger.info(
                "Project %d (%s) → Gitea %s (local: %s)",
                project_id,
                project_name,
                clone_url,
                local_path,
            )
            return dict(self._mapping[key])

    async def _ensure_webhook(self, slug: str) -> bool:
        """Create the push webhook for a repo, if configured.

        Failures are logged and swallowed — a missing webhook degrades
        the dev-environment refresh from instant to never (until the next
        successful retry), but must not block repo creation or ticket work.

        Parameters
        ----------
        slug : str
            URL-safe repository name.

        Returns
        -------
        bool
            ``True`` if a webhook now exists (created, already present,
            or webhook creation isn't configured — nothing to retry).
            ``False`` only on an actual failure, so ``ensure_repo`` can
            retry on the next lookup instead of giving up permanently.
        """
        if not self._webhook_target_url or not self._webhook_secret:
            return True  # not configured — nothing to retry
        try:
            await self._gitea.create_webhook(
                slug, self._webhook_target_url, self._webhook_secret
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to create Gitea webhook for %s: %s", slug, exc)
            return False

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
            Dict with ``gitea_repo_url`` and ``local_repo_path``, or None.
        """
        return self._mapping.get(f"kanboard:{kanboard_project_id}")

    def get_project_id_for_repo_name(self, repo_name: str) -> Optional[int]:
        """Reverse-lookup: Gitea repo name/slug -> kanboard_project_id.

        Used by the Gitea webhook receiver to resolve which project's
        main branch was pushed to, so a same-named "main" branch across
        different projects' repos doesn't cross-refresh the wrong
        project's preview — unlike ticket branches, whose
        ``ticket/<provider>/<id>`` names are globally unique, "main" is
        not.

        Iterates :meth:`all_mappings` — no persisted reverse index; the
        mapping file is small (one entry per project), so a linear scan
        on each webhook delivery is cheap and avoids a second on-disk
        index to keep in sync.

        Parameters
        ----------
        repo_name : str
            The Gitea repo's name/slug, e.g. from a push webhook
            payload's ``repository.name`` field.

        Returns
        -------
        Optional[int]
            The mapped Kanboard project id, or ``None`` if no mapping
            matches.
        """
        for mapping in self._mapping.values():
            # Prefer the stored slug — for disambiguated or empty-name
            # repos, re-slugifying the project name yields the WRONG slug.
            # Fall back to name-derived for pre-repo_slug mapping files
            # (same fallback ensure_repo() uses for the identical reason).
            slug = mapping.get("repo_slug") or _slugify(
                mapping.get("kanboard_project_name") or ""
            )
            if slug == repo_name:
                pid = mapping.get("kanboard_project_id")
                return int(pid) if pid is not None else None
        return None

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
        """Persist the project-repo mapping to disk.

        Writes to a temp file and ``os.replace``s it into place (matching
        ``TicketLifecycleManager._save``'s pattern) rather than writing
        ``self._repos_path`` directly. A direct write left a truncated,
        invalid file behind if the process was killed mid-write (OOM-kill,
        a container restart under time pressure, disk-full raising inside
        ``json.dump``) — ``_load_mapping`` would then silently discard the
        ENTIRE mapping history to ``{}`` on the next load (caught by its
        broad except, logged only as a warning), including every prior
        slug-disambiguation decision. ``os.replace`` is atomic on both
        POSIX and Windows, so a reader never observes a partially-written
        file.
        """
        os.makedirs(os.path.dirname(self._repos_path) or ".", exist_ok=True)
        tmp_path = f"{self._repos_path}.tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(self._mapping, f, indent=2)
            os.replace(tmp_path, self._repos_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not save project repos mapping: %s", exc)
            # Clean up the (possibly partially-written) temp file so a
            # failed save doesn't leave garbage behind or get confused
            # for a real save on a future run.
            try:
                os.remove(tmp_path)
            except OSError:
                pass
