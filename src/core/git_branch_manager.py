"""
Per-ticket git branch management.

Every ticket processed by Marcus gets its own git branch.  This module
handles creation, pushing, merging, and rebasing of those branches so
that human reviewers can inspect exactly what changes belong to each
ticket before they accept it.

Branch naming convention
------------------------
``ticket/{provider}/{safe_ticket_id}``

Examples
--------
- ``ticket/jira/proj-42``
- ``ticket/github/123``
- ``ticket/kanboard/7``

After a human accepts (closes) the ticket the branch is merged into the
configured main branch (default: ``main``).  If the ticket is later
reopened the old branch is rebased on the latest main so work can
continue cleanly.

Classes
-------
BranchManagerConfig
    Configuration dataclass.
BranchManager
    Async-friendly wrapper around git subprocess calls.
"""

import asyncio
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class BranchManagerConfig:
    """Configuration for BranchManager.

    Parameters
    ----------
    repo_path : str
        Absolute path to the git repository root.  Defaults to the
        current working directory.
    main_branch : str
        Name of the integration / main branch.  Defaults to ``"main"``.
    remote : str
        Remote name.  Defaults to ``"origin"``.
    git_user_name : str
        ``user.name`` used for merge commits.  Falls back to env var
        ``GIT_AUTHOR_NAME`` then the system git config.
    git_user_email : str
        ``user.email`` used for merge commits.
    push_on_create : bool
        Whether to push new branches to the remote immediately.
        Defaults to ``True``.
    """

    repo_path: str = field(default_factory=os.getcwd)
    main_branch: str = "main"
    remote: str = "origin"
    git_user_name: str = field(
        default_factory=lambda: os.getenv("GIT_AUTHOR_NAME", "Marcus Agent")
    )
    git_user_email: str = field(
        default_factory=lambda: os.getenv("GIT_AUTHOR_EMAIL", "marcus@local")
    )
    push_on_create: bool = True


class BranchManager:
    """Manages per-ticket git branches.

    All git operations are run in a thread pool so they do not block the
    asyncio event loop.

    Parameters
    ----------
    config : BranchManagerConfig
        Configuration; uses defaults if not provided.
    """

    def __init__(self, config: Optional[BranchManagerConfig] = None) -> None:
        """Initialise with optional config."""
        self.config = config or BranchManagerConfig()

    # ------------------------------------------------------------------
    # Branch naming
    # ------------------------------------------------------------------

    @staticmethod
    def make_branch_name(provider: str, ticket_id: str) -> str:
        """Generate the canonical branch name for a ticket.

        Parameters
        ----------
        provider : str
            Kanban provider (e.g. ``"jira"``, ``"github"``).
        ticket_id : str
            Ticket identifier (e.g. ``"PROJ-42"``, ``"123"``).

        Returns
        -------
        str
            Branch name like ``ticket/jira/proj-42``.
        """
        safe = re.sub(r"[^a-zA-Z0-9._-]", "-", ticket_id).lower()
        safe = re.sub(r"-{2,}", "-", safe).strip("-")
        return f"ticket/{provider.lower()}/{safe}"

    # ------------------------------------------------------------------
    # Core git operations (async wrappers)
    # ------------------------------------------------------------------

    async def branch_exists(self, branch_name: str, *, remote: bool = False) -> bool:
        """Check whether a local or remote branch exists.

        Parameters
        ----------
        branch_name : str
            Branch name to check.
        remote : bool
            When ``True`` check the remote tracking branches.

        Returns
        -------
        bool
            ``True`` if the branch exists.
        """
        if remote:
            ref = f"refs/remotes/{self.config.remote}/{branch_name}"
        else:
            ref = f"refs/heads/{branch_name}"
        rc, _, _ = await self._git("show-ref", "--verify", "--quiet", ref)
        return rc == 0

    async def create_branch(
        self,
        branch_name: str,
        *,
        from_branch: Optional[str] = None,
        force: bool = False,
    ) -> bool:
        """Create a new local branch.

        Parameters
        ----------
        branch_name : str
            Name for the new branch.
        from_branch : Optional[str]
            Starting point; defaults to ``config.main_branch``.
        force : bool
            Overwrite an existing local branch if it exists.

        Returns
        -------
        bool
            ``True`` if the branch was created (or already existed).
        """
        base = from_branch or self.config.main_branch

        if await self.branch_exists(branch_name) and not force:
            logger.info("Branch %s already exists — skipping create", branch_name)
            return True

        # Ensure we have the latest main.
        await self._git("fetch", self.config.remote, base)
        args = ["checkout", "-b", branch_name, f"{self.config.remote}/{base}"]
        if force:
            args = ["checkout", "-B", branch_name, f"{self.config.remote}/{base}"]

        rc, _, stderr = await self._git(*args)
        if rc != 0:
            logger.error("Failed to create branch %s: %s", branch_name, stderr)
            return False

        logger.info(
            "Created branch %s from %s/%s", branch_name, self.config.remote, base
        )

        if self.config.push_on_create:
            await self.push(branch_name)

        return True

    async def push(self, branch_name: str, *, force: bool = False) -> bool:
        """Push *branch_name* to the configured remote.

        Parameters
        ----------
        branch_name : str
            Local branch to push.
        force : bool
            Pass ``--force-with-lease`` for a safe force-push.

        Returns
        -------
        bool
            ``True`` on success.
        """
        args: List[str] = ["push", "-u", self.config.remote, branch_name]
        if force:
            args.append("--force-with-lease")
        rc, _, stderr = await self._git(*args)
        if rc != 0:
            logger.error("Push failed for %s: %s", branch_name, stderr)
            return False
        return True

    async def merge_to_main(
        self,
        branch_name: str,
        *,
        commit_message: Optional[str] = None,
        delete_after: bool = True,
    ) -> bool:
        """Merge *branch_name* into the main branch.

        Steps:
        1. Checkout main.
        2. Pull latest main from remote.
        3. Merge *branch_name* with a descriptive commit.
        4. Push main.
        5. Optionally delete the ticket branch locally and remotely.

        Parameters
        ----------
        branch_name : str
            Ticket branch to merge.
        commit_message : Optional[str]
            Merge commit message.  Defaults to a templated message.
        delete_after : bool
            Delete the branch after a successful merge.  Default ``True``.

        Returns
        -------
        bool
            ``True`` on success.
        """
        main = self.config.main_branch
        msg = commit_message or f"merge: {branch_name} (ticket accepted)"

        # Checkout main.
        rc, _, err = await self._git("checkout", main)
        if rc != 0:
            logger.error("Cannot checkout %s: %s", main, err)
            return False

        # Pull latest.
        await self._git("pull", self.config.remote, main)

        # Merge.
        rc, _, err = await self._git("merge", "--no-ff", branch_name, "-m", msg)
        if rc != 0:
            logger.error("Merge failed for %s → %s: %s", branch_name, main, err)
            return False

        # Push main.
        rc, _, err = await self._git("push", self.config.remote, main)
        if rc != 0:
            logger.error("Push of %s failed after merge: %s", main, err)
            return False

        logger.info("Merged %s → %s", branch_name, main)

        if delete_after:
            await self._delete_branch(branch_name)

        return True

    async def rebase_on_main(self, branch_name: str) -> bool:
        """Rebase *branch_name* on the latest main branch.

        Used when a ticket is reopened after its branch was already
        merged — a new set of commits are expected on the rebased branch.

        Parameters
        ----------
        branch_name : str
            Ticket branch to rebase.

        Returns
        -------
        bool
            ``True`` on success.  ``False`` if there are conflicts that
            need manual resolution.
        """
        main = self.config.main_branch
        remote = self.config.remote

        # Fetch latest from remote.
        await self._git("fetch", remote, main)

        # Checkout the ticket branch.
        rc, _, err = await self._git("checkout", branch_name)
        if rc != 0:
            # Branch may have been deleted after merge — recreate it.
            logger.info(
                "Branch %s not found locally, recreating from %s/%s",
                branch_name,
                remote,
                main,
            )
            ok = await self.create_branch(branch_name)
            if not ok:
                return False
            await self._git("checkout", branch_name)

        # Rebase.
        rc, _, err = await self._git("rebase", f"{remote}/{main}")
        if rc != 0:
            logger.error(
                "Rebase of %s on %s/%s failed (conflicts?): %s",
                branch_name,
                remote,
                main,
                err,
            )
            # Abort the rebase to leave repo clean.
            await self._git("rebase", "--abort")
            return False

        # Force-push the rebased branch.
        await self.push(branch_name, force=True)
        logger.info("Rebased %s on %s/%s", branch_name, remote, main)
        return True

    async def current_branch(self) -> str:
        """Return the name of the currently checked-out branch."""
        _, stdout, _ = await self._git("rev-parse", "--abbrev-ref", "HEAD")
        return stdout.strip()

    async def get_branch_commits(
        self, branch_name: str, *, base_branch: Optional[str] = None
    ) -> List[str]:
        """Return one-line summaries of commits on *branch_name* not in *base_branch*.

        Parameters
        ----------
        branch_name : str
            Ticket branch.
        base_branch : Optional[str]
            Comparison base; defaults to ``config.main_branch``.

        Returns
        -------
        List[str]
            List of commit summary strings (``{hash} {message}``).
        """
        base = base_branch or self.config.main_branch
        _, stdout, _ = await self._git("log", "--oneline", f"{base}..{branch_name}")
        lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
        return lines

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _git(self, *args: str) -> Tuple[int, str, str]:
        """Run a git command in the repo and return (returncode, stdout, stderr)."""
        cmd = ["git", "-C", self.config.repo_path] + list(args)
        env = dict(os.environ)
        env["GIT_AUTHOR_NAME"] = self.config.git_user_name
        env["GIT_AUTHOR_EMAIL"] = self.config.git_user_email
        env["GIT_COMMITTER_NAME"] = self.config.git_user_name
        env["GIT_COMMITTER_EMAIL"] = self.config.git_user_email

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
            ),
        )
        if result.returncode != 0 and args[0] not in (
            "show-ref",  # returns 1 when ref not found — expected
            "rebase",  # returns 1 on conflicts — handled by caller
            "merge",  # handled by caller
        ):
            logger.debug(
                "git %s → rc=%d stderr=%r",
                " ".join(args),
                result.returncode,
                result.stderr[:200],
            )
        return result.returncode, result.stdout, result.stderr

    async def _delete_branch(self, branch_name: str) -> None:
        """Delete local and remote copies of *branch_name* (best-effort)."""
        # Local.
        await self._git("branch", "-d", branch_name)
        # Remote.
        await self._git("push", self.config.remote, "--delete", branch_name)
        logger.info("Deleted branch %s (local + remote)", branch_name)
