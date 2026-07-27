"""
Unit tests for BranchManager's failure-path git hygiene.

All git subprocess calls are mocked via BranchManager._git — no real git
repository or subprocess is involved. These tests pin the behavior that a
FAILED multi-step git sequence must leave the shared working tree clean:
a conflicted `git merge` (or a conflicted `git pull` inside merge_to_main)
plants MERGE_HEAD in the repo, and without an explicit `git merge --abort`
every subsequent git operation for every other ticket fails with
"you have not concluded your merge".
"""

from unittest.mock import AsyncMock

import pytest

from src.core.git_branch_manager import BranchManager, BranchManagerConfig


def _mgr() -> BranchManager:
    """BranchManager with a throwaway repo path (never actually used)."""
    return BranchManager(BranchManagerConfig(repo_path="/tmp/fake-repo"))


def _calls(git_mock) -> list:
    """Return the list of git argv tuples issued via the mocked _git."""
    return [c.args for c in git_mock.call_args_list]


class TestMergeToMainAbortsOnFailure:
    """merge_to_main must clean up a failed merge, mirroring rebase_on_main."""

    @pytest.mark.asyncio
    async def test_failed_merge_runs_merge_abort(self):
        """A conflicted `git merge` is followed by `git merge --abort`."""
        mgr = _mgr()

        async def fake_git(*args):
            if args[0] == "merge" and "--abort" not in args:
                return (1, "", "CONFLICT (content): merge conflict in app.py")
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        ok = await mgr.merge_to_main("ticket/kanboard/7")

        assert ok is False
        assert ("merge", "--abort") in _calls(mgr._git)

    @pytest.mark.asyncio
    async def test_failed_pull_aborts_merge_state_and_fails(self):
        """A conflicted `git pull` (which also plants MERGE_HEAD) aborts and
        returns False instead of merging against a stale/conflicted main."""
        mgr = _mgr()

        async def fake_git(*args):
            if args[0] == "pull":
                return (1, "", "CONFLICT: Merge conflict in app.py")
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        ok = await mgr.merge_to_main("ticket/kanboard/7")

        assert ok is False
        assert ("merge", "--abort") in _calls(mgr._git)
        # The ticket merge itself must never have been attempted.
        assert not any(
            c[0] == "merge" and "ticket/kanboard/7" in c for c in _calls(mgr._git)
        )

    @pytest.mark.asyncio
    async def test_successful_merge_does_not_abort(self):
        """The happy path issues no merge --abort."""
        mgr = _mgr()
        mgr._git = AsyncMock(return_value=(0, "", ""))

        ok = await mgr.merge_to_main("ticket/kanboard/7", delete_after=False)

        assert ok is True
        assert ("merge", "--abort") not in _calls(mgr._git)


class TestMergeFetchesAgentBranch:
    """merge_to_main must merge the AGENT's pushed commits, not the stale
    local branch. With the self-clone design the agent's work lives on the
    remote branch; this clone's local ticket branch is empty."""

    @pytest.mark.asyncio
    async def test_fetches_branch_and_merges_fetch_head(self):
        """A successful fetch → merge FETCH_HEAD (the remote agent commits)."""
        mgr = _mgr()
        mgr._git = AsyncMock(return_value=(0, "", ""))

        ok = await mgr.merge_to_main("ticket/kanboard/3", delete_after=False)

        assert ok is True
        calls = _calls(mgr._git)
        # Fetched the ticket branch before merging.
        assert any(
            c[0] == "fetch" and "ticket/kanboard/3" in c for c in calls
        )
        # Merged the fetched remote tip, not the stale local branch.
        assert any(
            c[0] == "merge" and "FETCH_HEAD" in c for c in calls
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_local_branch_when_remote_absent(self):
        """If the remote branch can't be fetched, merge the local ref."""
        mgr = _mgr()

        async def fake_git(*args):
            if args[0] == "fetch" and args[-1] == "ticket/kanboard/3":
                return (1, "", "couldn't find remote ref")
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        ok = await mgr.merge_to_main("ticket/kanboard/3", delete_after=False)

        assert ok is True
        assert any(
            c[0] == "merge" and "ticket/kanboard/3" in c
            for c in _calls(mgr._git)
        )


def _fake_git_no_remote_branch(overrides=None):
    """Build a fake_git where `fetch origin <branch>` fails (branch absent
    on the remote) — the common baseline for the "genuinely new ticket"
    tests below, which must never trip the resume path.

    Parameters
    ----------
    overrides : Optional[Callable[[tuple], Optional[tuple]]]
        Called with each call's args first; if it returns a non-None
        result, that result is used instead of the default.
    """

    async def fake_git(*args):
        if overrides is not None:
            result = overrides(args)
            if result is not None:
                return result
        if args[0] == "fetch" and args[-1] == "ticket/kanboard/7":
            return (1, "", "couldn't find remote ref")
        if args[0] == "show-ref":
            return (1, "", "")
        return (0, "", "")

    return fake_git


class TestCreateBranchPublishesToRemote:
    """create_branch must reliably PUBLISH the branch to the remote (Gitea).

    The old code cut the branch locally and pushed with the result discarded,
    and early-returned without pushing when the branch already existed
    locally. Either path could leave a local-only branch: the agent then
    couldn't `git checkout origin/<branch>`, worked on a local-only branch,
    and its commits never reached Gitea.
    """

    @pytest.mark.asyncio
    async def test_new_branch_is_pushed_to_remote(self):
        """A freshly created branch is pushed with -u to the remote."""
        mgr = _mgr()
        mgr._git = AsyncMock(side_effect=_fake_git_no_remote_branch())

        ok = await mgr.create_branch("ticket/kanboard/7")

        assert ok is True
        calls = _calls(mgr._git)
        assert ("push", "-u", "origin", "ticket/kanboard/7") in calls

    @pytest.mark.asyncio
    async def test_push_failure_propagates_as_false(self):
        """If the push fails, create_branch returns False (not a silent True)."""
        mgr = _mgr()

        def overrides(args):
            if args[0] == "push":
                return (1, "", "denied")
            return None

        mgr._git = AsyncMock(side_effect=_fake_git_no_remote_branch(overrides))

        ok = await mgr.create_branch("ticket/kanboard/7")

        assert ok is False

    @pytest.mark.asyncio
    async def test_existing_local_branch_is_still_pushed(self):
        """A branch that already exists LOCALLY (and NOT yet on the remote)
        is still pushed (a prior run may have created it without a
        successful push)."""
        mgr = _mgr()

        def overrides(args):
            if args[0] == "show-ref":
                return (0, "", "")  # branch already present locally
            return None

        mgr._git = AsyncMock(side_effect=_fake_git_no_remote_branch(overrides))

        ok = await mgr.create_branch("ticket/kanboard/7")

        assert ok is True
        calls = _calls(mgr._git)
        # No checkout -b (it already exists) but it IS pushed.
        assert not any(c[0] == "checkout" for c in calls)
        assert ("push", "-u", "origin", "ticket/kanboard/7") in calls

    @pytest.mark.asyncio
    async def test_push_disabled_skips_push(self):
        """push_on_create=False keeps the old local-only behaviour."""
        mgr = BranchManager(
            BranchManagerConfig(repo_path="/tmp/fake-repo", push_on_create=False)
        )
        mgr._git = AsyncMock(side_effect=_fake_git_no_remote_branch())

        ok = await mgr.create_branch("ticket/kanboard/7")

        assert ok is True
        assert not any(c[0] == "push" for c in _calls(mgr._git))


class TestCreateBranchResumesFromRemote:
    """A ticket branch that already exists on the remote — from a PRIOR
    session, an agent's earlier commits, or simply because Marcus's own
    local clone is not guaranteed to be persistent (e.g. recreated on a
    redeploy) — must be RESUMED, not silently overwritten.

    Regression: create_branch only ever checked LOCAL branch existence. When
    the local clone didn't have the branch (however that happened) it always
    cut a fresh branch from main and then tried to push it — which Git
    rejects as a non-fast-forward whenever the remote already has commits
    the fresh local branch doesn't, i.e. exactly "fails because it already
    exists". The fix: always check the REMOTE for this exact branch first,
    and resume it (checkout -B <branch> FETCH_HEAD) instead of creating
    fresh, whenever the remote already has it.
    """

    @pytest.mark.asyncio
    async def test_resumes_when_remote_branch_exists_and_local_absent(self):
        """Remote has the branch (prior agent commits); local clone does
        not. The branch is checked out from FETCH_HEAD, NOT created fresh
        from main — no non-fast-forward push."""
        mgr = _mgr()

        async def fake_git(*args):
            if args[0] == "fetch" and args[-1] == "ticket/kanboard/7":
                return (0, "", "")  # remote HAS this branch
            if args[0] == "show-ref":
                return (1, "", "")  # not present in Marcus's local clone
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        ok = await mgr.create_branch("ticket/kanboard/7")

        assert ok is True
        calls = _calls(mgr._git)
        # Resumed from the remote tip...
        assert ("checkout", "-B", "ticket/kanboard/7", "FETCH_HEAD") in calls
        # ...never cut fresh from main.
        assert not any(
            c[0] == "checkout" and "origin/main" in c for c in calls
        )

    @pytest.mark.asyncio
    async def test_resumes_even_when_local_branch_already_exists(self):
        """Remote has NEWER commits than Marcus's own (stale) local branch —
        resuming from the remote (not the stale local ref) is what makes the
        follow-up push provably a no-op instead of a non-fast-forward
        rejection."""
        mgr = _mgr()

        async def fake_git(*args):
            if args[0] == "fetch" and args[-1] == "ticket/kanboard/7":
                return (0, "", "")
            if args[0] == "show-ref":
                return (0, "", "")  # ALSO present locally (but stale)
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        ok = await mgr.create_branch("ticket/kanboard/7")

        assert ok is True
        calls = _calls(mgr._git)
        assert ("checkout", "-B", "ticket/kanboard/7", "FETCH_HEAD") in calls

    @pytest.mark.asyncio
    async def test_resume_checkout_failure_returns_false(self):
        """A failed checkout of the fetched remote tip is a hard failure,
        not silently swallowed."""
        mgr = _mgr()

        async def fake_git(*args):
            if args[0] == "fetch" and args[-1] == "ticket/kanboard/7":
                return (0, "", "")
            if args[0] == "checkout":
                return (1, "", "error: pathspec conflict")
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        ok = await mgr.create_branch("ticket/kanboard/7")

        assert ok is False

    @pytest.mark.asyncio
    async def test_resume_still_verifies_push(self):
        """The resumed branch still goes through the same push step (a
        provable no-op, since local now exactly matches FETCH_HEAD) — one
        code path, not a special-cased early return."""
        mgr = _mgr()

        async def fake_git(*args):
            if args[0] == "fetch" and args[-1] == "ticket/kanboard/7":
                return (0, "", "")
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        ok = await mgr.create_branch("ticket/kanboard/7")

        assert ok is True
        assert ("push", "-u", "origin", "ticket/kanboard/7") in _calls(mgr._git)

    @pytest.mark.asyncio
    async def test_force_bypasses_resume_and_creates_fresh(self):
        """force=True means 'start over' — it must NOT resume the remote
        branch, even if one exists (this is what rebase_on_main/other
        explicit-recreate callers rely on)."""
        mgr = _mgr()
        calls_seen = []

        async def fake_git(*args):
            calls_seen.append(args)
            if args[0] == "fetch" and args[-1] == "ticket/kanboard/7":
                return (0, "", "")  # remote has it — must be ignored
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        ok = await mgr.create_branch("ticket/kanboard/7", force=True)

        assert ok is True
        # No resume fetch of the ticket branch itself, and no FETCH_HEAD
        # checkout — force always cuts fresh from base.
        assert not any(
            c[0] == "fetch" and c[-1] == "ticket/kanboard/7" for c in calls_seen
        )
        assert ("checkout", "-B", "ticket/kanboard/7", "origin/main") in calls_seen


class TestSyncBranch:
    """sync_branch makes the local branch ref match the remote's latest, so a
    downstream clone (the preview container) sees the pushed work."""

    @pytest.mark.asyncio
    async def test_fetches_and_moves_local_ref(self):
        mgr = _mgr()
        mgr._git = AsyncMock(return_value=(0, "", ""))

        ok = await mgr.sync_branch("ticket/kanboard/7")

        assert ok is True
        calls = _calls(mgr._git)
        assert ("fetch", "origin", "ticket/kanboard/7") in calls
        # Local branch ref moved to the freshly fetched commit.
        assert ("branch", "-f", "ticket/kanboard/7", "FETCH_HEAD") in calls

    @pytest.mark.asyncio
    async def test_returns_false_when_remote_fetch_fails(self):
        mgr = _mgr()

        async def fake_git(*args):
            if args[0] == "fetch":
                return (1, "", "couldn't find remote ref")
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)
        assert await mgr.sync_branch("ticket/kanboard/7") is False

    @pytest.mark.asyncio
    async def test_falls_back_to_update_ref_when_branch_checked_out(self):
        mgr = _mgr()

        async def fake_git(*args):
            if args[0] == "branch":
                return (1, "", "cannot force update the current branch")
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)
        ok = await mgr.sync_branch("ticket/kanboard/7")
        assert ok is True
        calls = _calls(mgr._git)
        assert ("update-ref", "refs/heads/ticket/kanboard/7", "FETCH_HEAD") in calls
