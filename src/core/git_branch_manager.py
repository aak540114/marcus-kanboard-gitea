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

#: Without this, a git subprocess (push/fetch/pull against an unreachable
#: or slow-to-respond remote) can block the executor thread indefinitely —
#: same hazard dev_environment.py's docker calls already guard against
#: (see _DOCKER_CMD_TIMEOUT there), just never applied here. That thread
#: is never returned to the shared default ThreadPoolExecutor, so enough
#: concurrent stuck git calls (e.g. several tickets merging while the
#: remote is degraded) can starve unrelated run_in_executor work across
#: Marcus, not just git operations.
_GIT_CMD_TIMEOUT = 60


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
        # One instance is shared by every ticket of a project (see
        # HumanGatedWorkflow._branch_for_ticket's docstring) — they all
        # operate on the SAME local clone/working directory. Without this,
        # two tickets' git operations running concurrently (a common case:
        # multiple agents, or a webhook and a poll cycle, touching
        # different tickets of the same project around the same time) can
        # interleave checkouts/merges against that one shared working
        # tree, corrupting it for both. Symptom seen in production: ticket
        # A's merge conflict leaves the clone mid-merge (MERGE_HEAD set)
        # just as ticket B's create_branch tries to checkout its own
        # branch — checkout unconditionally refuses ("you have not
        # concluded your merge") no matter which branch it targets, so B
        # fails with a misleading "check repository permissions" error
        # even though B's branch is completely fine on the remote.
        # Held only around the three entry points that mutate the working
        # tree/index (create_branch, merge_to_main, rebase_on_main) — see
        # each method's *_unlocked twin, used for internal calls between
        # them so a lock holder never re-acquires its own lock.
        self._lock = asyncio.Lock()

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

    async def _checkout_with_conflict_recovery(
        self, *checkout_args: str
    ) -> Tuple[int, str, str]:
        """Run ``git checkout <checkout_args>``, self-healing leftover
        state from a PREVIOUS ticket's failure in this SHARED clone (see
        the ``_lock`` comment in ``__init__``) instead of failing outright
        on a mess that has nothing to do with the current ticket.

        ``git checkout`` unconditionally refuses — regardless of which
        branch it's switching to, even with ``-B`` — in two situations:

        1. A merge or rebase is left unresolved (``"you have not
           concluded your merge"``). ``merge_to_main``'s own ``git merge
           --abort`` on a genuine conflict is not always guaranteed to
           leave the clone spotless (and a rarer failure path — a
           successful local merge whose subsequent push then fails —
           leaves no abort at all).
        2. The working tree has uncommitted local changes that would be
           overwritten (``"would be overwritten by checkout"`` — git
           emits this for both tracked-file and untracked-file variants).
           This clone is fully disposable: no legitimate uncommitted work
           should ever live here (an agent's real work lives in commits
           pushed to the remote from its OWN separate clone — see
           ``get_work_context``), so whatever dirtied it is discarded
           unconditionally. Mirrors the identical, already-proven-safe
           precedent in ``src/marcus_mcp/tools/task.py``'s
           ``_merge_agent_branch_to_main`` (Bug #651).

        One retry after cleaning either of these up recovers the CURRENT
        ticket's operation without needing to fully diagnose the prior
        one's failure.

        Parameters
        ----------
        *checkout_args : str
            Arguments passed to ``git checkout`` (e.g. ``"-B",
            branch_name, "FETCH_HEAD"``).

        Returns
        -------
        Tuple[int, str, str]
            ``(returncode, stdout, stderr)`` of the (possibly retried)
            checkout.
        """
        rc, out, err = await self._git("checkout", *checkout_args)
        if rc == 0:
            return rc, out, err

        lower = err.lower()
        if "you have not concluded your merge" in lower or "merge_head" in lower:
            logger.warning(
                "git checkout blocked by a leftover in-progress merge in "
                "the shared clone (likely from a previous ticket's "
                "failure) — aborting it and retrying: %s",
                err.strip(),
            )
            await self._git("merge", "--abort")
            return await self._git("checkout", *checkout_args)
        if "rebase" in lower and "in progress" in lower:
            logger.warning(
                "git checkout blocked by a leftover in-progress rebase in "
                "the shared clone — aborting it and retrying: %s",
                err.strip(),
            )
            await self._git("rebase", "--abort")
            return await self._git("checkout", *checkout_args)
        if "would be overwritten by checkout" in lower:
            logger.warning(
                "git checkout blocked by uncommitted local changes left in "
                "the shared clone (this clone is disposable — no "
                "legitimate uncommitted work should ever live here) — "
                "discarding them and retrying: %s",
                err.strip(),
            )
            await self._git("reset", "--hard", "HEAD")
            await self._git("clean", "-fd")
            return await self._git("checkout", *checkout_args)
        return rc, out, err

    async def create_branch(
        self,
        branch_name: str,
        *,
        from_branch: Optional[str] = None,
        force: bool = False,
    ) -> bool:
        """Create a ticket's branch, resuming it if it already exists.

        Acquires this manager's shared-clone lock — see the ``_lock``
        comment in ``__init__``. Internal callers already holding it
        (:meth:`rebase_on_main`) must call :meth:`_create_branch_unlocked`
        directly instead, to avoid deadlocking on re-acquisition.

        A ticket is not necessarily "brand new" just because Marcus's OWN
        local git clone doesn't have its branch: an agent's prior commits
        live on the REMOTE, and this clone is not guaranteed to be
        persistent (e.g. it can be recreated on every container redeploy).
        Assuming "not local = never worked on" and cutting a fresh branch
        from ``from_branch`` would silently discard that history, and the
        follow-up push would then be rejected as a non-fast-forward the
        instant the remote has anything Marcus's fresh local branch
        doesn't — surfacing as "branch already exists" / a failed ticket
        claim, even though the fix is simply to resume what is already
        there.

        So (unless ``force=True``) the REMOTE is checked first: if it
        already has ``branch_name``, the local branch is reset to match it
        (``git checkout -B branch_name FETCH_HEAD``) and returned as
        resumed — existing commits are kept, not overwritten. Only when the
        remote genuinely has no such branch does this fall back to the
        original behaviour: reuse the local branch if Marcus's own clone
        already has it, otherwise cut a fresh one from ``from_branch``.

        Parameters
        ----------
        branch_name : str
            Name for the branch.
        from_branch : Optional[str]
            Starting point when no prior work exists anywhere; defaults to
            ``config.main_branch``.
        force : bool
            Ignore any existing remote/local branch and always cut a fresh
            one from ``from_branch`` — used by callers that explicitly want
            to start over (e.g. after an already-merged ticket is reopened).

        Returns
        -------
        bool
            ``True`` if the branch was created, resumed, or already existed.
        """
        async with self._lock:
            return await self._create_branch_unlocked(
                branch_name, from_branch=from_branch, force=force
            )

    async def _create_branch_unlocked(
        self,
        branch_name: str,
        *,
        from_branch: Optional[str] = None,
        force: bool = False,
    ) -> bool:
        """Body of :meth:`create_branch`, without acquiring the lock.

        Only call directly when the caller already holds ``self._lock``
        (:meth:`rebase_on_main`'s unlocked body) — every other caller must
        go through the public, lock-acquiring :meth:`create_branch`.
        """
        base = from_branch or self.config.main_branch

        if not force:
            fetch_rc, _, fetch_stderr = await self._git(
                "fetch", self.config.remote, branch_name
            )
            if fetch_rc != 0:
                # A non-zero exit here is normal (and expected) when the
                # branch simply doesn't exist yet on the remote — git says
                # so via "couldn't find remote ref". Anything else (a
                # network blip, an auth hiccup) is a real, worth-surfacing
                # error that would otherwise silently fall through to
                # "create fresh" with no trace of why the resume path was
                # skipped.
                if "couldn't find remote ref" in fetch_stderr:
                    logger.debug(
                        "No existing branch %s on %s (creating fresh)",
                        branch_name,
                        self.config.remote,
                    )
                else:
                    logger.warning(
                        "Could not check %s for branch %s (falling back to "
                        "local/fresh): %s",
                        self.config.remote,
                        branch_name,
                        fetch_stderr.strip(),
                    )
            if fetch_rc == 0:
                rc, _, stderr = await self._checkout_with_conflict_recovery(
                    "-B", branch_name, "FETCH_HEAD"
                )
                if rc != 0:
                    logger.error(
                        "Branch %s exists on %s but checkout failed: %s",
                        branch_name,
                        self.config.remote,
                        stderr,
                    )
                    return False
                logger.info(
                    "Branch %s already exists on %s — resuming prior work "
                    "instead of creating fresh from %s",
                    branch_name,
                    self.config.remote,
                    base,
                )
                return await self._publish(branch_name, force=force)

        already_local = await self.branch_exists(branch_name)
        if not already_local or force:
            # No prior work anywhere (or force=True): cut the branch fresh.
            await self._git("fetch", self.config.remote, base)
            checkout = "-B" if force else "-b"
            rc, _, stderr = await self._checkout_with_conflict_recovery(
                checkout, branch_name, f"{self.config.remote}/{base}"
            )
            if rc != 0:
                logger.error("Failed to create branch %s: %s", branch_name, stderr)
                return False
            logger.info(
                "Created branch %s from %s/%s", branch_name, self.config.remote, base
            )
        else:
            logger.info("Branch %s already exists locally", branch_name)

        return await self._publish(branch_name, force=force)

    async def _publish(self, branch_name: str, *, force: bool = False) -> bool:
        """Push *branch_name* to the remote when ``push_on_create`` is set.

        Shared tail of :meth:`create_branch`'s resume and create paths, so
        every branch — whichever path produced it — is reliably published
        the same way (a resumed branch's push is a provable no-op, since the
        local ref now exactly matches what was just fetched).

        Two deliberate properties, both fixing a "Marcus never created the
        branch on Gitea" symptom the old code had:
        1. Pushes even when the branch already existed LOCALLY. A prior run
           could have created it locally but failed to push; skipping the
           push here would mean it's never retried, so the branch would
           live only in Marcus's clone forever.
        2. PROPAGATES a push failure as ``False`` instead of discarding the
           result. A silently-dropped push left a local-only branch while
           callers proceeded as if it were on the remote — the agent then
           couldn't ``git checkout origin/<branch>``, worked on a
           local-only branch, and its own pushes had no upstream, so its
           commits never reached Gitea.

        Parameters
        ----------
        branch_name : str
            Local branch to publish.
        force : bool
            Passed through to :meth:`push` as ``--force-with-lease``.

        Returns
        -------
        bool
            ``True`` if published (or ``push_on_create`` is disabled).
        """
        if self.config.push_on_create:
            pushed = await self.push(branch_name, force=force)
            if not pushed:
                logger.error(
                    "Branch %s created locally but push to %s failed — the "
                    "branch is NOT on the remote",
                    branch_name,
                    self.config.remote,
                )
                return False

        return True

    async def sync_branch(self, branch_name: str) -> bool:
        """Fetch *branch_name* from the remote and move the local ref to it.

        Makes this repo's LOCAL ``branch_name`` match the remote's latest —
        without checking it out — so a downstream consumer that clones this
        repo (the per-ticket preview container clones Marcus's working repo)
        sees the newest pushed work. This is how Marcus makes a "preview"
        reflect the AI agent's committed-and-pushed remote branch rather than
        whatever stale state its local clone happened to hold.

        Parameters
        ----------
        branch_name : str
            Branch to sync from the remote.

        Returns
        -------
        bool
            ``True`` if the branch was fetched and the local ref updated.
            ``False`` if the remote fetch failed (e.g. the branch isn't on
            the remote yet).
        """
        rc, _, stderr = await self._git("fetch", self.config.remote, branch_name)
        if rc != 0:
            logger.warning(
                "Could not fetch %s from %s: %s",
                branch_name,
                self.config.remote,
                stderr,
            )
            return False
        # Point the local branch at what we just fetched (FETCH_HEAD), without
        # checking it out. `branch -f` is refused if the branch is currently
        # checked out — Marcus's working clone normally sits on main, so this
        # succeeds; on the rare exception we fall back to update-ref.
        rc, _, _ = await self._git("branch", "-f", branch_name, "FETCH_HEAD")
        if rc != 0:
            await self._git(
                "update-ref", f"refs/heads/{branch_name}", "FETCH_HEAD"
            )
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
        delete_after: bool = False,
    ) -> bool:
        """Merge *branch_name* into the main branch.

        Acquires this manager's shared-clone lock — see the ``_lock``
        comment in ``__init__``.

        Steps:
        1. Checkout main.
        2. Pull latest main from remote.
        3. Merge *branch_name* with a descriptive commit.
        4. Push main.
        5. Optionally delete the LOCAL copy of the ticket branch.

        Parameters
        ----------
        branch_name : str
            Ticket branch to merge.
        commit_message : Optional[str]
            Merge commit message.  Defaults to a templated message.
        delete_after : bool
            Delete the LOCAL copy of the branch after a successful merge.
            Default ``False`` — even though this clone's local branch is
            Marcus's own throwaway working copy (never the agent's actual
            history, which only ever lives on the REMOTE, untouched by
            this flag either way), a ticket's branch is kept around
            locally too unless a caller explicitly opts into cleaning it
            up, so nothing about a completed ticket disappears from
            Marcus's own clone by default.

        Returns
        -------
        bool
            ``True`` on success.
        """
        async with self._lock:
            return await self._merge_to_main_unlocked(
                branch_name, commit_message=commit_message, delete_after=delete_after
            )

    async def _merge_to_main_unlocked(
        self,
        branch_name: str,
        *,
        commit_message: Optional[str] = None,
        delete_after: bool = False,
    ) -> bool:
        """Body of :meth:`merge_to_main`, without acquiring the lock.

        Only call directly when the caller already holds ``self._lock``.
        Every current caller goes through the public :meth:`merge_to_main`
        instead — kept symmetric with :meth:`_create_branch_unlocked` for
        any future internal caller.
        """
        main = self.config.main_branch
        msg = commit_message or f"merge: {branch_name} (ticket accepted)"

        # Checkout main.
        rc, _, err = await self._checkout_with_conflict_recovery(main)
        if rc != 0:
            logger.error("Cannot checkout %s: %s", main, err)
            return False

        # Defensive reset — mirrors src/marcus_mcp/tools/task.py's
        # _merge_agent_branch_to_main precedent (Bug #651). `checkout
        # main` above is a same-branch NO-OP whenever this shared clone
        # is already sitting on main between tickets (the common steady
        # state, and a fresh clone's default state right after a Marcus
        # restart), so it cannot fail and cannot trigger
        # _checkout_with_conflict_recovery's dirty-tree healing even when
        # the tree IS dirty — that would otherwise only surface later as
        # a `git pull` failure ("would be overwritten by merge"),
        # reproducing the same cascade one ticket downstream. This clone
        # is fully disposable; discarding unconditionally here, before
        # `pull` ever runs, is safe by design (mirrors task.py's
        # identical reasoning: merging only cares about committed state).
        await self._git("reset", "--hard", "HEAD")
        await self._git("clean", "-fd")

        # Pull latest. A conflicted pull plants MERGE_HEAD exactly like a
        # conflicted merge does — abort and fail rather than attempting the
        # ticket merge on top of a conflicted or stale main.
        rc, _, err = await self._git("pull", self.config.remote, main)
        if rc != 0:
            logger.error(
                "Pull of %s/%s failed before merging %s: %s",
                self.config.remote,
                main,
                branch_name,
                err,
            )
            await self._git("merge", "--abort")
            return False

        # Fetch the ticket branch from the remote before merging. The coding
        # agent works in its OWN clone (it self-clones — see get_work_context)
        # and pushes commits to the REMOTE branch; this clone's local
        # ``branch_name`` is the empty branch Marcus created at start time and
        # has NONE of the agent's work. Merging it would land nothing on main
        # ("dragged to Done but nothing merged"). Merge the fetched remote tip
        # instead; fall back to the local branch only if the remote ref is
        # genuinely absent (offline, or a local-only test flow).
        merge_ref = branch_name
        rc, _, err = await self._git("fetch", self.config.remote, branch_name)
        if rc == 0:
            merge_ref = "FETCH_HEAD"
        else:
            logger.warning(
                "Could not fetch %s/%s before merge; merging local ref: %s",
                self.config.remote,
                branch_name,
                err,
            )

        # Merge. On failure, ALWAYS abort: a conflicted merge leaves
        # MERGE_HEAD and a conflicted index in this shared working tree,
        # and every subsequent git operation for every other ticket then
        # fails with "you have not concluded your merge" until someone
        # manually aborts. Mirrors the abort rebase_on_main already does.
        rc, _, err = await self._git("merge", "--no-ff", merge_ref, "-m", msg)
        if rc != 0:
            logger.error("Merge failed for %s → %s: %s", branch_name, main, err)
            await self._git("merge", "--abort")
            return False

        # Push main.
        rc, _, err = await self._git("push", self.config.remote, main)
        if rc != 0:
            logger.error("Push of %s failed after merge: %s", main, err)
            return False

        logger.info("Merged %s → %s", branch_name, main)

        if delete_after:
            await self._delete_local_branch(branch_name)

        return True

    async def rebase_on_main(self, branch_name: str) -> bool:
        """Rebase *branch_name* on the latest main branch.

        Acquires this manager's shared-clone lock — see the ``_lock``
        comment in ``__init__``.

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
        async with self._lock:
            return await self._rebase_on_main_unlocked(branch_name)

    async def _rebase_on_main_unlocked(self, branch_name: str) -> bool:
        """Body of :meth:`rebase_on_main`, without acquiring the lock."""
        main = self.config.main_branch
        remote = self.config.remote

        # Fetch latest from remote.
        await self._git("fetch", remote, main)

        # Checkout the ticket branch.
        rc, _, err = await self._checkout_with_conflict_recovery(branch_name)
        if rc != 0:
            # Branch may have been deleted after merge — recreate it. Calls
            # the UNLOCKED body directly: this method already holds
            # self._lock, and it is not re-entrant.
            logger.info(
                "Branch %s not found locally, recreating from %s/%s",
                branch_name,
                remote,
                main,
            )
            ok = await self._create_branch_unlocked(branch_name, force=True)
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

    async def get_branch_diff(
        self, branch_name: str, *, base_branch: Optional[str] = None
    ) -> str:
        """Return the unified diff for all changes on *branch_name* vs *base_branch*.

        Uses ``git diff <base>...<branch>`` (three-dot notation) so only
        commits unique to *branch_name* are included.

        Parameters
        ----------
        branch_name : str
            Ticket branch to diff.
        base_branch : Optional[str]
            Comparison base; defaults to ``config.main_branch``.

        Returns
        -------
        str
            Unified diff text.  Empty string when there are no changes.
        """
        base = base_branch or self.config.main_branch
        await self._git("fetch", self.config.remote, base)
        # Also fetch the ticket branch: the agent's commits live on the
        # REMOTE branch (it self-clones), not this clone's stale local branch.
        # Diff the fetched remote tip so AI Verify sees the agent's real work.
        branch_ref = branch_name
        rc, _, _ = await self._git("fetch", self.config.remote, branch_name)
        if rc == 0:
            branch_ref = "FETCH_HEAD"
        remote_base = f"{self.config.remote}/{base}"
        _, stdout, _ = await self._git("diff", f"{remote_base}...{branch_ref}")
        return stdout

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
        # Fetch the ticket branch so the commit list reflects the agent's
        # pushed work (its commits are on the remote, not this local clone).
        branch_ref = branch_name
        rc, _, _ = await self._git("fetch", self.config.remote, branch_name)
        if rc == 0:
            branch_ref = "FETCH_HEAD"
        _, stdout, _ = await self._git("log", "--oneline", f"{base}..{branch_ref}")
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
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=_GIT_CMD_TIMEOUT,
                ),
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "git %s timed out after %ds", " ".join(args), _GIT_CMD_TIMEOUT
            )
            return (
                1,
                "",
                f"git {' '.join(args)} timed out after {_GIT_CMD_TIMEOUT}s",
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

    async def _delete_local_branch(self, branch_name: str) -> None:
        """Delete the LOCAL copy of *branch_name* only (best-effort).

        The remote copy is deliberately left alone — it holds the agent's
        actual commit history, and Marcus merges tickets into main, it
        doesn't manage the user's remote repo housekeeping for them.
        """
        await self._git("branch", "-d", branch_name)
        logger.info("Deleted local branch %s (remote branch preserved)", branch_name)
