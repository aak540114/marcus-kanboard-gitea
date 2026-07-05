"""GitLab REST API wrapper for repository management.

Creates and manages GitLab repositories for Kanboard projects.
Uses the GitLab REST API v4 with a Personal Access Token.

Configuration
-------------
``gitlab_url``
    Base URL, e.g. ``http://localhost:8929``.
``gitlab_token``
    Personal Access Token with ``api`` and ``write_repository`` scopes.
``gitlab_namespace``
    Optional group/user namespace. Defaults to the token owner.
"""

import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class GitLabManager:
    """Create and configure GitLab repositories for new Kanboard projects.

    Parameters
    ----------
    gitlab_url : str
        GitLab base URL (no trailing slash).
    token : str
        Personal Access Token.
    namespace : Optional[str]
        GitLab username or group to create repos under.
        If None, repos are created under the authenticated user.
    """

    def __init__(
        self,
        gitlab_url: str,
        token: str,
        namespace: Optional[str] = None,
    ) -> None:
        """Initialise the manager (no network calls)."""
        self._base = gitlab_url.rstrip("/")
        self._token = token
        self._namespace = namespace
        self._headers = {"PRIVATE-TOKEN": token}
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Open the HTTP client and verify the token is valid.

        Returns
        -------
        bool
            True if the token works.
        """
        self._client = httpx.AsyncClient(timeout=30.0, headers=self._headers)
        try:
            r = await self._client.get(f"{self._base}/api/v4/user")
            r.raise_for_status()
            user = r.json()
            if self._namespace is None:
                self._namespace = user.get("username", "root")
            logger.info(
                "GitLabManager connected as %s on %s",
                self._namespace,
                self._base,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("GitLabManager connect failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Repository operations
    # ------------------------------------------------------------------

    async def repo_exists(self, slug: str) -> bool:
        """Check whether a repository with the given slug already exists.

        Parameters
        ----------
        slug : str
            URL-safe repository name (e.g. ``shopping-cart``).

        Returns
        -------
        bool
            True if an exact match is found.
        """
        if self._client is None:
            raise RuntimeError("GitLabManager not connected — call connect() first")
        r = await self._client.get(
            f"{self._base}/api/v4/projects",
            params={"search": slug, "owned": True},
        )
        r.raise_for_status()
        projects = r.json()
        return any(p.get("path") == slug for p in projects)

    async def create_repo(self, name: str, description: str = "") -> str:
        """Create a new GitLab repository.

        Parameters
        ----------
        name : str
            Human-readable project name (will be slugified for the path).
        description : str
            Optional project description.

        Returns
        -------
        str
            HTTP clone URL, e.g.
            ``http://localhost:8929/root/shopping-cart.git``.

        Raises
        ------
        RuntimeError
            If the repository cannot be created.
        """
        if self._client is None:
            raise RuntimeError("GitLabManager not connected — call connect() first")

        slug = _slugify(name)

        if await self.repo_exists(slug):
            logger.info("GitLab repo %r already exists — skipping creation", slug)
            r = await self._client.get(
                f"{self._base}/api/v4/projects/{self._namespace}%2F{slug}"
            )
            r.raise_for_status()
            return str(r.json()["http_url_to_repo"])

        payload: Dict[str, Any] = {
            "name": name,
            "path": slug,
            "description": description,
            "visibility": "private",
            "initialize_with_readme": False,
        }
        if self._namespace:
            # Try to resolve namespace_id (handles groups correctly)
            ns_id = await self._resolve_namespace_id(self._namespace)
            if ns_id:
                payload["namespace_id"] = ns_id

        r = await self._client.post(
            f"{self._base}/api/v4/projects", json=payload
        )
        r.raise_for_status()
        clone_url: str = r.json()["http_url_to_repo"]
        logger.info("Created GitLab repo %s", clone_url)
        return clone_url

    async def init_with_readme(self, clone_url: str, local_path: str) -> None:
        """Initialise a local repo with a README and push to GitLab.

        Parameters
        ----------
        clone_url : str
            GitLab HTTP clone URL for the repo.
        local_path : str
            Local directory to initialise (created if absent).

        Raises
        ------
        RuntimeError
            If any git command fails.
        """
        os.makedirs(local_path, exist_ok=True)

        # Embed token in the push URL so no interactive auth is needed.
        push_url = _auth_clone_url(clone_url, self._token)

        cmds = [
            ["git", "init", "-b", "main"],
            ["git", "config", "user.email", "marcus@localhost"],
            ["git", "config", "user.name", "Marcus"],
        ]
        for cmd in cmds:
            _run_git(cmd, cwd=local_path)

        readme = os.path.join(local_path, "README.md")
        if not os.path.exists(readme):
            project_name = os.path.basename(local_path).replace("-", " ").title()
            with open(readme, "w") as f:
                f.write(f"# {project_name}\n\nManaged by Marcus.\n")

        _run_git(["git", "add", "README.md"], cwd=local_path)
        _run_git(
            ["git", "commit", "-m", "init: initial commit from Marcus"],
            cwd=local_path,
        )
        _run_git(
            ["git", "remote", "add", "origin", push_url], cwd=local_path
        )
        _run_git(
            ["git", "push", "-u", "origin", "main"], cwd=local_path
        )
        logger.info("Pushed initial commit to %s", clone_url)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _resolve_namespace_id(self, namespace: str) -> Optional[int]:
        """Look up the numeric namespace ID for a username or group path.

        Parameters
        ----------
        namespace : str
            Username or group path.

        Returns
        -------
        Optional[int]
            Numeric ID or None if not found.
        """
        if self._client is None:
            return None
        try:
            r = await self._client.get(
                f"{self._base}/api/v4/namespaces",
                params={"search": namespace},
            )
            r.raise_for_status()
            for ns in r.json():
                if ns.get("path") == namespace or ns.get("name") == namespace:
                    return int(ns["id"])
        except Exception:  # noqa: BLE001
            pass
        return None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    """Convert a project name to a URL-safe GitLab path slug.

    Parameters
    ----------
    name : str
        Human-readable name (e.g. ``"My Shopping Cart!"``)

    Returns
    -------
    str
        Lowercase slug with hyphens (e.g. ``"my-shopping-cart"``)
    """
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def _auth_clone_url(clone_url: str, token: str) -> str:
    """Embed a PAT into an HTTP clone URL for password-free git push.

    Parameters
    ----------
    clone_url : str
        Plain clone URL, e.g. ``http://localhost:8929/root/repo.git``.
    token : str
        GitLab Personal Access Token.

    Returns
    -------
    str
        Authenticated URL, e.g.
        ``http://oauth2:<token>@localhost:8929/root/repo.git``.
    """
    if clone_url.startswith("http://"):
        rest = clone_url[len("http://"):]
        return f"http://oauth2:{token}@{rest}"
    if clone_url.startswith("https://"):
        rest = clone_url[len("https://"):]
        return f"https://oauth2:{token}@{rest}"
    return clone_url


def _run_git(args: List[str], cwd: str) -> None:
    """Run a git command, raising RuntimeError on non-zero exit.

    Parameters
    ----------
    args : list
        Command and arguments (e.g. ``["git", "init"]``).
    cwd : str
        Working directory.

    Raises
    ------
    RuntimeError
        If the command exits with a non-zero code.
    """
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True  # noqa: S603
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git command failed: {' '.join(args)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
