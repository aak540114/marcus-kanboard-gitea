"""Gitea REST API wrapper for repository management.

Creates and manages Gitea repositories for Kanboard projects.
Uses the Gitea REST API v1 with a Personal Access Token.

Gitea was chosen over GitLab CE for this deployment because it is a single
lightweight Go binary — it boots in seconds on ~200-500 MB of RAM, versus
GitLab CE's multi-minute boot and 4+ GB RAM requirement.  That makes local
demos and small VPS deployments far more practical.

Configuration
-------------
``gitea_url``
    Base URL, e.g. ``http://localhost:3000``.
``gitea_token``
    Personal Access Token with ``write:repository`` and ``read:user`` scopes.
``gitea_namespace``
    Optional organisation name. Defaults to the token owner's own account.

Push authentication
--------------------
Unlike GitLab, which accepts the fixed placeholder username ``oauth2`` for
any Personal Access Token, Gitea's Git-over-HTTP auth requires the *actual*
username of the token's owner as the HTTP Basic username (the token itself
is the password).  This manager resolves and caches that username in
``connect()`` and uses it — not the namespace, which may be a different
organisation — when building authenticated push URLs.
"""

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class GiteaManager:
    """Create and configure Gitea repositories for new Kanboard projects.

    Parameters
    ----------
    gitea_url : str
        Gitea base URL (no trailing slash).
    token : str
        Personal Access Token.
    namespace : Optional[str]
        Gitea username or organisation to create repos under.
        If None, repos are created under the authenticated user.
    """

    def __init__(
        self,
        gitea_url: str,
        token: str,
        namespace: Optional[str] = None,
    ) -> None:
        """Initialise the manager (no network calls)."""
        self._base = gitea_url.rstrip("/")
        self._token = token
        self._namespace = namespace
        self._username: Optional[str] = None
        self._headers = {"Authorization": f"token {token}"}
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Open the HTTP client and verify the token is valid.

        Also resolves the token owner's username, which is required later
        for building authenticated push URLs (Gitea has no GitLab-style
        ``oauth2`` username placeholder).

        Returns
        -------
        bool
            True if the token works.
        """
        self._client = httpx.AsyncClient(timeout=30.0, headers=self._headers)
        try:
            r = await self._client.get(f"{self._base}/api/v1/user")
            r.raise_for_status()
            user = r.json()
            self._username = user.get("login")
            if self._namespace is None:
                self._namespace = self._username
            logger.info(
                "GiteaManager connected as %s (namespace %s) on %s",
                self._username,
                self._namespace,
                self._base,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("GiteaManager connect failed: %s", exc)
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
            True if the repository exists under the configured namespace.
        """
        if self._client is None:
            raise RuntimeError("GiteaManager not connected — call connect() first")
        owner = self._namespace
        r = await self._client.get(f"{self._base}/api/v1/repos/{owner}/{slug}")
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True

    async def create_repo(self, name: str, description: str = "") -> str:
        """Create a new Gitea repository.

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
            ``http://localhost:3000/root/shopping-cart.git``.

        Raises
        ------
        RuntimeError
            If the repository cannot be created.
        """
        if self._client is None:
            raise RuntimeError("GiteaManager not connected — call connect() first")

        slug = _slugify(name)
        # connect() guarantees _namespace is resolved (falls back to the
        # token owner's username); the "or ''" only narrows the Optional
        # for mypy — the RuntimeError above fires first when unconnected.
        owner = self._namespace or ""

        if await self.repo_exists(slug):
            logger.info("Gitea repo %r already exists — skipping creation", slug)
            return self._clone_url(owner, slug)

        payload: Dict[str, Any] = {
            "name": slug,
            "description": description,
            "private": True,
            "auto_init": False,
        }

        # Repos can only be created under the authenticated user's own
        # account or an organisation they belong to — Gitea addresses each
        # via a different endpoint (no numeric namespace ID, unlike GitLab).
        if owner and owner != self._username:
            create_url = f"{self._base}/api/v1/orgs/{owner}/repos"
        else:
            create_url = f"{self._base}/api/v1/user/repos"

        r = await self._client.post(create_url, json=payload)
        r.raise_for_status()
        clone_url = self._clone_url(owner, slug)
        logger.info("Created Gitea repo %s", clone_url)
        return clone_url

    def _clone_url(self, owner: str, slug: str) -> str:
        """Build the HTTP clone URL from Marcus's own configured Gitea URL.

        Deliberately NOT the ``clone_url`` field from Gitea's API response:
        Gitea composes that from its browser-facing ``ROOT_URL`` config
        (``http://localhost:3000/`` in ``docker-compose.yml``) no matter
        what address the API caller used — with the default
        ``PUBLIC_URL_DETECTION = legacy`` there is no request-host
        fallback. In Docker mode Marcus reaches Gitea at
        ``http://gitea:3000``, so a ROOT_URL-derived clone URL points at
        ``localhost:3000`` *inside the marcus container*, where nothing
        listens — the initial ``git push`` in ``init_with_readme()`` would
        get connection-refused and the project would never receive a repo
        mapping. Deriving from ``self._base`` (the address Marcus itself is
        configured to reach Gitea on) is correct in every run mode.

        Parameters
        ----------
        owner : str
            Repository owner (user or organisation namespace).
        slug : str
            URL-safe repository name.

        Returns
        -------
        str
            e.g. ``http://gitea:3000/root/shopping-cart.git``.
        """
        return f"{self._base}/{owner}/{slug}.git"

    async def create_webhook(self, slug: str, target_url: str, secret: str) -> bool:
        """Create a push webhook on a repo, idempotently.

        Used to get instant "branch was updated" notifications (for the
        hot-reload dev environment refresh) instead of polling. Gitea signs
        each delivery with an HMAC-SHA256 of the raw body using ``secret``,
        sent as the ``X-Gitea-Signature`` header — the receiving endpoint
        must verify it with the same secret.

        Parameters
        ----------
        slug : str
            URL-safe repository name (e.g. ``shopping-cart``).
        target_url : str
            URL Gitea should POST push events to.
        secret : str
            Shared HMAC secret for signing deliveries.

        Returns
        -------
        bool
            ``True`` if a new webhook was created; ``False`` if one already
            existed pointing at the same ``target_url`` (no-op, safe to call
            on every repo-provisioning run).

        Raises
        ------
        RuntimeError
            If not connected.
        """
        if self._client is None:
            raise RuntimeError("GiteaManager not connected — call connect() first")
        owner = self._namespace

        r = await self._client.get(f"{self._base}/api/v1/repos/{owner}/{slug}/hooks")
        r.raise_for_status()
        for hook in r.json():
            if hook.get("config", {}).get("url") == target_url:
                logger.info(
                    "Gitea webhook for %s already points at %s — skipping",
                    slug,
                    target_url,
                )
                return False

        payload: Dict[str, Any] = {
            "type": "gitea",
            "config": {
                "url": target_url,
                "content_type": "json",
                "secret": secret,
            },
            "events": ["push"],
            "active": True,
        }
        r = await self._client.post(
            f"{self._base}/api/v1/repos/{owner}/{slug}/hooks", json=payload
        )
        r.raise_for_status()
        logger.info("Created Gitea push webhook for %s -> %s", slug, target_url)
        return True

    async def init_with_readme(self, clone_url: str, local_path: str) -> None:
        """Initialise a local repo with a README and push to Gitea.

        Parameters
        ----------
        clone_url : str
            Gitea HTTP clone URL for the repo.
        local_path : str
            Local directory to initialise (created if absent).

        Raises
        ------
        RuntimeError
            If any git command fails.
        """
        os.makedirs(local_path, exist_ok=True)

        # Embed the token owner's username + token in the push URL so no
        # interactive auth is needed for Marcus's own pushes or the AI
        # agent's later `git push origin <branch>` calls.
        push_url = _auth_clone_url(clone_url, self._username or "", self._token)

        cmds = [
            ["git", "init", "-b", "main"],
            ["git", "config", "user.email", "marcus@localhost"],
            ["git", "config", "user.name", "Marcus"],
        ]
        for cmd in cmds:
            await _run_git(cmd, cwd=local_path)

        readme = os.path.join(local_path, "README.md")
        if not os.path.exists(readme):
            project_name = os.path.basename(local_path).replace("-", " ").title()
            with open(readme, "w") as f:
                f.write(f"# {project_name}\n\nManaged by Marcus.\n")

        await _run_git(["git", "add", "README.md"], cwd=local_path)

        # Idempotency matters here: ensure_repo() deliberately does not
        # persist a mapping on failure and RETRIES this whole method on the
        # next lookup. A first attempt that committed but died on the
        # network push leaves a directory where a naive re-run fails
        # forever — `git commit` exits non-zero with "nothing to commit"
        # and `git remote add` with "remote origin already exists" — which
        # turned ONE transient failure into permanently broken repo
        # provisioning for that project. So: commit only when something is
        # staged, and update the remote's URL when it already exists
        # (which also picks up a rotated GITEA_TOKEN).
        try:
            # Exits 0 when the staged tree is clean, non-zero when there
            # are staged changes to commit.
            await _run_git(
                ["git", "diff", "--cached", "--quiet"], cwd=local_path
            )
            has_staged_changes = False
        except RuntimeError:
            has_staged_changes = True

        if has_staged_changes:
            await _run_git(
                ["git", "commit", "-m", "init: initial commit from Marcus"],
                cwd=local_path,
            )

        try:
            await _run_git(
                ["git", "remote", "add", "origin", push_url], cwd=local_path
            )
        except RuntimeError:
            await _run_git(
                ["git", "remote", "set-url", "origin", push_url],
                cwd=local_path,
            )

        await _run_git(
            ["git", "push", "-u", "origin", "main"], cwd=local_path
        )
        logger.info("Pushed initial commit to %s", clone_url)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    """Convert a project name to a URL-safe Gitea repo slug.

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


def _auth_clone_url(clone_url: str, username: str, token: str) -> str:
    """Embed a PAT into an HTTP clone URL for password-free git push.

    Gitea requires the *real* username of the token's owner as the HTTP
    Basic username — there is no GitLab-style ``oauth2`` placeholder that
    works regardless of who owns the token.

    Parameters
    ----------
    clone_url : str
        Plain clone URL, e.g. ``http://localhost:3000/root/repo.git``.
    username : str
        Gitea username that owns the token.
    token : str
        Gitea Personal Access Token.

    Returns
    -------
    str
        Authenticated URL, e.g.
        ``http://root:<token>@localhost:3000/root/repo.git``.
    """
    if clone_url.startswith("http://"):
        rest = clone_url[len("http://"):]
        return f"http://{username}:{token}@{rest}"
    if clone_url.startswith("https://"):
        rest = clone_url[len("https://"):]
        return f"https://{username}:{token}@{rest}"
    return clone_url


async def _run_git(args: List[str], cwd: str) -> None:
    """Run a git command asynchronously, raising RuntimeError on non-zero exit.

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
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        # Redact any embedded credentials (http://user:TOKEN@host) before logging.
        safe_args = [re.sub(r"://[^:@/]+:[^@]*@", "://***:***@", a) for a in args]
        raise RuntimeError(
            f"git command failed: {' '.join(safe_args)}\n"
            f"stdout: {stdout_bytes.decode()}\nstderr: {stderr_bytes.decode()}"
        )
