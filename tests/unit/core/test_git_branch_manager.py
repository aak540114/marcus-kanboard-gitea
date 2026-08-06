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

import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.git_branch_manager import (
    _GIT_CMD_TIMEOUT,
    BranchManager,
    BranchManagerConfig,
)


def _mgr() -> BranchManager:
    """BranchManager with a throwaway repo path (never actually used)."""
    return BranchManager(BranchManagerConfig(repo_path="/tmp/fake-repo"))


def _calls(git_mock) -> list:
    """Return the list of git argv tuples issued via the mocked _git."""
    return [c.args for c in git_mock.call_args_list]


class TestGitCommandTimeout:
    """Regression: BranchManager._git ran subprocess.run with no timeout=,
    so an unresponsive remote (network partition, stalled TCP negotiation,
    a credential prompt on a misconfigured remote) could block the shared
    executor thread forever — the exact hazard dev_environment.py already
    guards its docker calls against, just never applied to git."""

    @pytest.mark.asyncio
    async def test_timeout_expired_returns_a_failure_tuple_not_a_hang(self):
        """A timed-out git call must resolve to (nonzero, "", stderr),
        matching _git's normal contract, instead of raising uncaught or
        blocking indefinitely."""
        mgr = _mgr()

        with patch(
            "src.core.git_branch_manager.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git push", timeout=60),
        ):
            returncode, stdout, stderr = await mgr._git("push", "origin", "main")

        assert returncode != 0
        assert "timed out" in stderr

    @pytest.mark.asyncio
    async def test_subprocess_run_is_called_with_a_timeout(self):
        """Every git subprocess call must pass timeout= — the bug was its
        total absence, not a wrong value."""
        mgr = _mgr()
        fake_result = MagicMock(returncode=0, stdout="", stderr="")

        with patch(
            "src.core.git_branch_manager.subprocess.run",
            return_value=fake_result,
        ) as mock_run:
            await mgr._git("status")

        assert mock_run.call_args.kwargs.get("timeout") == _GIT_CMD_TIMEOUT


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

    @pytest.mark.asyncio
    async def test_delete_after_true_deletes_local_branch_but_keeps_remote(self):
        """An explicit opt-in cleans up the LOCAL branch — Marcus's own
        throwaway working copy — but the REMOTE branch must survive
        either way: it holds the agent's actual commit history, and
        Marcus's job is to merge a ticket into main, not manage the
        user's remote repo housekeeping for them."""
        mgr = _mgr()
        mgr._git = AsyncMock(return_value=(0, "", ""))

        ok = await mgr.merge_to_main("ticket/kanboard/7", delete_after=True)

        assert ok is True
        calls = _calls(mgr._git)
        assert ("branch", "-d", "ticket/kanboard/7") in calls
        assert not any(
            c[0] == "push" and "--delete" in c for c in calls
        )

    @pytest.mark.asyncio
    async def test_delete_after_defaults_to_false(self):
        """Regression: a ticket's branch must not vanish from Marcus's own
        clone just because its merge succeeded (or, previously, even when
        it failed with a conflict — the old True default combined with a
        caller passing it unconditionally was the reported symptom).
        delete_after now defaults to False; nothing is deleted unless a
        caller explicitly opts in."""
        mgr = _mgr()
        mgr._git = AsyncMock(return_value=(0, "", ""))

        ok = await mgr.merge_to_main("ticket/kanboard/7")

        assert ok is True
        assert ("branch", "-d", "ticket/kanboard/7") not in _calls(mgr._git)

    @pytest.mark.asyncio
    async def test_delete_after_false_keeps_the_local_branch_too(self):
        """delete_after=False (now also the default) opts out of the
        local cleanup."""
        mgr = _mgr()
        mgr._git = AsyncMock(return_value=(0, "", ""))

        ok = await mgr.merge_to_main("ticket/kanboard/7", delete_after=False)

        assert ok is True
        assert ("branch", "-d", "ticket/kanboard/7") not in _calls(mgr._git)


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
    async def test_transient_fetch_error_is_logged_not_silently_swallowed(
        self, caplog
    ):
        """A non-zero fetch exit means EITHER 'no such branch on the
        remote' (the common, expected case) OR a transient failure
        (network blip, auth hiccup) — git's own error text is the only
        way to tell them apart. Previously the fetch's stderr was
        discarded entirely and both cases fell through to 'create fresh'
        with no log trail explaining why the resume path wasn't taken.
        A transient error must be visible in the logs."""
        import logging

        mgr = _mgr()

        async def fake_git(*args):
            if args[0] == "fetch" and args[-1] == "ticket/kanboard/7":
                return (128, "", "fatal: unable to access 'origin': timed out")
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        with caplog.at_level(logging.WARNING):
            ok = await mgr.create_branch("ticket/kanboard/7")

        assert ok is True  # still falls through to create-fresh
        assert any(
            "timed out" in record.message or "timed out" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_branch_not_found_on_remote_does_not_warn(self, caplog):
        """The everyday case — a genuinely new ticket whose branch simply
        doesn't exist yet on the remote — must NOT be logged as a warning;
        only real errors should be surfaced that way."""
        import logging

        mgr = _mgr()

        async def fake_git(*args):
            if args[0] == "fetch" and args[-1] == "ticket/kanboard/8":
                return (128, "", "fatal: couldn't find remote ref ticket/kanboard/8")
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        with caplog.at_level(logging.WARNING):
            ok = await mgr.create_branch("ticket/kanboard/8")

        assert ok is True
        assert not any(record.levelno >= logging.WARNING for record in caplog.records)

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


class TestRebaseOnMainRecreatesWithForce:
    """When a ticket is reopened after its branch was already merged (and
    thus deleted locally), rebase_on_main must recreate it with
    force=True — create_branch's own docstring names exactly this caller
    as the intended use case for that flag, since the point is to start
    fresh rather than resume whatever the remote might still have under
    the old branch name."""

    @pytest.mark.asyncio
    async def test_recreate_passes_force_true(self):
        mgr = _mgr()
        calls_seen = []

        async def fake_git(*args):
            calls_seen.append(args)
            if args[0] == "checkout" and args[1] == "ticket/kanboard/9":
                return (1, "", "error: pathspec did not match")  # deleted locally
            if args[0] == "rebase":
                return (0, "", "")
            if args[0] == "push":
                return (0, "", "")
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        ok = await mgr.rebase_on_main("ticket/kanboard/9")

        assert ok is True
        # force=True cuts fresh from origin/main — it must NOT take the
        # non-force resume path (checkout -B branch_name FETCH_HEAD),
        # which would silently keep whatever old history the remote might
        # still have under this branch name instead of starting over.
        assert (
            "checkout",
            "-B",
            "ticket/kanboard/9",
            "origin/main",
        ) in calls_seen
        assert not any(
            c[0] == "checkout" and c[-1] == "FETCH_HEAD" for c in calls_seen
        )


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


class TestCheckoutConflictRecovery:
    """Regression: create_branch/merge_to_main/rebase_on_main all call
    `git checkout`, which unconditionally refuses ("you have not
    concluded your merge") while a merge/rebase from a PREVIOUS ticket
    is left unresolved in this SHARED clone — even when checking out a
    totally unrelated branch. Reported symptom: create_branch failed
    with a misleading "check repository permissions" error for a branch
    that demonstrably existed on the remote, right after another
    ticket's merge hit a conflict."""

    @pytest.mark.asyncio
    async def test_create_branch_recovers_from_leftover_merge_state(self):
        """create_branch's resume-path checkout, blocked by a leftover
        MERGE_HEAD, aborts it and retries instead of failing outright."""
        mgr = _mgr()
        checkout_attempts = []

        async def fake_git(*args):
            if args[0] == "checkout":
                checkout_attempts.append(args)
                if len(checkout_attempts) == 1:
                    return (
                        1,
                        "",
                        "error: you have not concluded your merge "
                        "(MERGE_HEAD exists).",
                    )
                return (0, "", "")
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        ok = await mgr.create_branch("ticket/kanboard/51")

        assert ok is True
        assert len(checkout_attempts) == 2  # blocked, then recovered
        assert ("merge", "--abort") in _calls(mgr._git)

    @pytest.mark.asyncio
    async def test_merge_to_main_recovers_from_leftover_merge_state(self):
        """merge_to_main's initial `checkout main`, blocked by a leftover
        MERGE_HEAD from a DIFFERENT ticket's failed merge sharing this
        clone, aborts it and retries instead of failing outright."""
        mgr = _mgr()
        checkout_attempts = []

        async def fake_git(*args):
            if args[0] == "checkout" and args[1] == "main":
                checkout_attempts.append(args)
                if len(checkout_attempts) == 1:
                    return (1, "", "you have not concluded your merge")
                return (0, "", "")
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        ok = await mgr.merge_to_main("ticket/kanboard/7")

        assert ok is True
        assert len(checkout_attempts) == 2

    @pytest.mark.asyncio
    async def test_checkout_recovers_from_dirty_tracked_files(self):
        """Production symptom: a leftover dirty `index.html` blocks every
        subsequent ticket's checkout with 'local changes ... would be
        overwritten by checkout'. Discard via reset --hard + clean -fd
        and retry once."""
        mgr = _mgr()
        checkout_attempts = []

        async def fake_git(*args):
            if args[0] == "checkout":
                checkout_attempts.append(args)
                if len(checkout_attempts) == 1:
                    return (
                        1,
                        "",
                        "error: Your local changes to the following files "
                        "would be overwritten by checkout:\n\tindex.html\n"
                        "Please commit your changes or stash them before "
                        "you switch branches.\nAborting",
                    )
                return (0, "", "")
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        rc, _, _ = await mgr._checkout_with_conflict_recovery(
            "-B", "ticket/kanboard/49", "FETCH_HEAD"
        )

        assert rc == 0
        assert len(checkout_attempts) == 2
        calls = _calls(mgr._git)
        assert ("reset", "--hard", "HEAD") in calls
        assert ("clean", "-fd") in calls

    @pytest.mark.asyncio
    async def test_checkout_recovers_from_dirty_untracked_files(self):
        """Sibling git message for untracked (not just tracked) files
        must also be recognized and recovered the same way."""
        mgr = _mgr()
        checkout_attempts = []

        async def fake_git(*args):
            if args[0] == "checkout":
                checkout_attempts.append(args)
                if len(checkout_attempts) == 1:
                    return (
                        1,
                        "",
                        "error: The following untracked working tree "
                        "files would be overwritten by checkout:\n"
                        "\tscratch.tmp\nPlease move or remove them before "
                        "you switch branches.\nAborting",
                    )
                return (0, "", "")
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        rc, _, _ = await mgr._checkout_with_conflict_recovery("main")

        assert rc == 0
        assert len(checkout_attempts) == 2
        assert ("clean", "-fd") in _calls(mgr._git)

    @pytest.mark.asyncio
    async def test_create_branch_recovers_from_dirty_working_tree(self):
        """End-to-end regression for the reported incident: create_branch's
        resume-path checkout (-B branch FETCH_HEAD), blocked by a dirty
        index.html, discards it and retries instead of failing every
        subsequent ticket."""
        mgr = _mgr()
        checkout_attempts = []

        async def fake_git(*args):
            if args[0] == "fetch" and args[-1] == "ticket/kanboard/49":
                return (0, "", "")  # branch exists on remote
            if args[0] == "checkout":
                checkout_attempts.append(args)
                if len(checkout_attempts) == 1:
                    return (
                        1,
                        "",
                        "error: Your local changes to the following files "
                        "would be overwritten by checkout:\n\tindex.html\n"
                        "Please commit your changes or stash them before "
                        "you switch branches.\nAborting",
                    )
                return (0, "", "")
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        ok = await mgr.create_branch("ticket/kanboard/49")

        assert ok is True
        assert len(checkout_attempts) == 2
        calls = _calls(mgr._git)
        assert ("reset", "--hard", "HEAD") in calls
        assert ("clean", "-fd") in calls

    @pytest.mark.asyncio
    async def test_unrelated_checkout_failure_is_not_retried(self):
        """A checkout failure with no merge/rebase/dirty-tree signature
        (e.g. a genuinely missing branch) is returned as-is, not
        endlessly retried or misdiagnosed as leftover conflict state."""
        mgr = _mgr()
        mgr._git = AsyncMock(
            return_value=(1, "", "pathspec 'nope' did not match any file(s)")
        )

        rc, _, err = await mgr._checkout_with_conflict_recovery("nope")

        assert rc == 1
        assert ("merge", "--abort") not in _calls(mgr._git)
        assert ("rebase", "--abort") not in _calls(mgr._git)
        assert ("reset", "--hard", "HEAD") not in _calls(mgr._git)
        assert ("clean", "-fd") not in _calls(mgr._git)


class TestMergeToMainDefensiveReset:
    """merge_to_main's `checkout main` is a same-branch NO-OP (and so
    cannot fail / cannot trigger _checkout_with_conflict_recovery) when
    the shared clone is already sitting on main between tickets — the
    common steady state. Mirrors task.py's _merge_agent_branch_to_main
    (Bug #651) defensive-reset precedent, placed unconditionally before
    `git pull` to close that gap."""

    @pytest.mark.asyncio
    async def test_reset_and_clean_run_after_checkout_before_pull(self):
        mgr = _mgr()
        mgr._git = AsyncMock(return_value=(0, "", ""))

        ok = await mgr.merge_to_main("ticket/kanboard/7")

        assert ok is True
        calls = _calls(mgr._git)
        checkout_idx = calls.index(("checkout", "main"))
        reset_idx = calls.index(("reset", "--hard", "HEAD"))
        clean_idx = calls.index(("clean", "-fd"))
        pull_idx = next(i for i, c in enumerate(calls) if c[0] == "pull")
        assert checkout_idx < reset_idx < clean_idx < pull_idx

    @pytest.mark.asyncio
    async def test_reset_and_clean_are_unconditional_not_error_triggered(self):
        """Issued every time, not only after a detected checkout/pull
        failure — a plain happy-path merge must still show them."""
        mgr = _mgr()
        mgr._git = AsyncMock(return_value=(0, "", ""))

        await mgr.merge_to_main("ticket/kanboard/7")

        calls = _calls(mgr._git)
        assert ("reset", "--hard", "HEAD") in calls
        assert ("clean", "-fd") in calls


class TestSharedCloneLock:
    """Regression: BranchManager instances are shared across every ticket
    of a project (HumanGatedWorkflow._branch_for_ticket caches one per
    repo path) — without serializing access, two tickets' git operations
    running concurrently could interleave checkouts/merges against the
    same working tree and corrupt it for both."""

    @pytest.mark.asyncio
    async def test_create_branch_and_merge_to_main_do_not_interleave(self):
        """Two concurrent calls against the same manager never have their
        underlying git commands interleaved — one fully completes (all
        its _git calls contiguous) before the other's first call."""
        import asyncio

        mgr = _mgr()
        order = []

        async def fake_git(*args):
            order.append(("start", args))
            await asyncio.sleep(0)  # yield, so a race WOULD interleave here
            order.append(("end", args))
            if args[0] == "fetch":
                return (1, "", "couldn't find remote ref")
            return (0, "", "")

        mgr._git = AsyncMock(side_effect=fake_git)

        await asyncio.gather(
            mgr.create_branch("ticket/kanboard/1"),
            mgr.merge_to_main("ticket/kanboard/2"),
        )

        # For every ("start", X) there must be a matching ("end", X)
        # immediately reachable before a DIFFERENT call's "start" — i.e.
        # no call starts while another is still mid-flight. Verify by
        # checking every "start" is immediately followed (ignoring other
        # complete start/end pairs) by its own "end" before any other
        # call's "start" appears while it's outstanding.
        outstanding = 0
        for phase, _ in order:
            if phase == "start":
                assert outstanding == 0, (
                    "a git call started while another was still in "
                    "flight — the shared-clone lock did not serialize them"
                )
                outstanding += 1
            else:
                outstanding -= 1
