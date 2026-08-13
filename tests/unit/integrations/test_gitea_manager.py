"""
Unit tests for src/integrations/gitea_manager.py

Every test mocks httpx.AsyncClient (or a pre-built AsyncMock client) — no
real network or git calls are made.
"""

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.integrations.gitea_manager import (
    GiteaManager,
    _auth_clone_url,
    _slugify,
    public_authenticated_clone_url,
    public_branch_web_url,
    public_repo_web_url,
)


def _mock_response(json_data, status_code: int = 200) -> MagicMock:
    """Build a mock httpx.Response with a working raise_for_status()."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_data)
    if status_code >= 400:
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                str(status_code), request=MagicMock(), response=resp
            )
        )
    else:
        resp.raise_for_status = MagicMock()
    return resp


class TestSlugify:
    """_slugify converts human names to URL-safe repo path slugs."""

    def test_lowercases_and_hyphenates(self):
        assert _slugify("My Shopping Cart!") == "my-shopping-cart"

    def test_strips_leading_trailing_hyphens(self):
        assert _slugify("  --Weird Name--  ") == "weird-name"

    def test_collapses_repeated_separators(self):
        assert _slugify("a___b   c") == "a-b-c"


class TestAuthCloneUrl:
    """_auth_clone_url embeds the real Gitea username (not a placeholder)."""

    def test_http_embeds_username_and_token(self):
        url = _auth_clone_url("http://localhost:3000/root/app.git", "root", "tok123")
        assert url == "http://root:tok123@localhost:3000/root/app.git"

    def test_https_embeds_username_and_token(self):
        url = _auth_clone_url(
            "https://git.example.com/alice/app.git", "alice", "tok456"
        )
        assert url == "https://alice:tok456@git.example.com/alice/app.git"

    def test_uses_token_owner_username_even_for_org_repo(self):
        """A repo under an org still authenticates as the token's own user."""
        url = _auth_clone_url(
            "http://localhost:3000/myteam/app.git", "alice", "tok456"
        )
        assert url.startswith("http://alice:tok456@")

    def test_unknown_scheme_passthrough(self):
        url = _auth_clone_url("git@localhost:root/app.git", "root", "tok")
        assert url == "git@localhost:root/app.git"


class TestPublicUrlHelpers:
    """Rehosting Marcus-internal Gitea URLs to browser-facing ones."""

    def test_repo_web_url_rehosts_and_strips_git(self):
        """Internal clone URL → browser repo URL (public host, no .git)."""
        assert (
            public_repo_web_url(
                "http://gitea:3000/root/shopping-cart.git",
                "http://localhost:3000",
            )
            == "http://localhost:3000/root/shopping-cart"
        )

    def test_repo_web_url_honors_https_public_base(self):
        """An HTTPS public base (real domain) is preserved."""
        assert (
            public_repo_web_url(
                "http://gitea:3000/root/app.git", "https://git.example.com/"
            )
            == "https://git.example.com/root/app"
        )

    def test_branch_web_url_points_at_the_branch(self):
        """Branch URL is repo + /src/branch/<branch> on the public host."""
        assert (
            public_branch_web_url(
                "http://gitea:3000/root/app.git",
                "http://localhost:3000",
                "ticket/kanboard/42",
            )
            == "http://localhost:3000/root/app/src/branch/ticket/kanboard/42"
        )

    def test_branch_web_url_falls_back_to_repo_when_branch_empty(self):
        """No branch → repo root URL (no dangling /src/branch/)."""
        assert (
            public_branch_web_url(
                "http://gitea:3000/root/app.git", "http://localhost:3000", ""
            )
            == "http://localhost:3000/root/app"
        )

    def test_authenticated_clone_url_embeds_creds_on_public_host(self):
        """Clone URL is rehosted to the public host with creds embedded."""
        assert (
            public_authenticated_clone_url(
                "http://gitea:3000/root/app.git",
                "http://localhost:3000",
                "root",
                "tok123",
            )
            == "http://root:tok123@localhost:3000/root/app.git"
        )

    def test_authenticated_clone_url_without_token_is_plain(self):
        """Empty token → plain rehosted URL, no credentials embedded."""
        assert (
            public_authenticated_clone_url(
                "http://gitea:3000/root/app.git",
                "http://localhost:3000",
                "root",
                "",
            )
            == "http://localhost:3000/root/app.git"
        )


class TestConnect:
    """connect() opens the client and resolves the token owner's username."""

    @pytest.mark.asyncio
    async def test_connect_success_sets_username_and_default_namespace(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response({"id": 1, "login": "root"})
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            mgr = GiteaManager("http://localhost:3000", "tok")
            ok = await mgr.connect()

        assert ok is True
        assert mgr._username == "root"
        assert mgr._namespace == "root"

    @pytest.mark.asyncio
    async def test_connect_preserves_explicit_namespace(self):
        """An explicit namespace (org) is not overwritten by the token owner."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response({"id": 1, "login": "alice"})
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            mgr = GiteaManager("http://localhost:3000", "tok", namespace="myteam")
            await mgr.connect()

        assert mgr._username == "alice"
        assert mgr._namespace == "myteam"

    @pytest.mark.asyncio
    async def test_connect_failure_returns_false(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("no route"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            mgr = GiteaManager("http://localhost:3000", "bad-token")
            ok = await mgr.connect()

        assert ok is False

    def test_constructor_builds_authorization_token_header(self):
        mgr = GiteaManager("http://localhost:3000", "secret-tok")
        assert mgr._headers == {"Authorization": "token secret-tok"}


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_closes_client(self):
        mgr = GiteaManager("http://localhost:3000", "tok")
        mock_client = AsyncMock()
        mgr._client = mock_client

        await mgr.disconnect()

        mock_client.aclose.assert_called_once()
        assert mgr._client is None

    @pytest.mark.asyncio
    async def test_disconnect_noop_when_never_connected(self):
        mgr = GiteaManager("http://localhost:3000", "tok")
        await mgr.disconnect()  # must not raise


class TestRepoExists:
    @pytest.mark.asyncio
    async def test_raises_if_not_connected(self):
        mgr = GiteaManager("http://localhost:3000", "tok")
        with pytest.raises(RuntimeError):
            await mgr.repo_exists("app")

    @pytest.mark.asyncio
    async def test_true_when_repo_found(self):
        mgr = GiteaManager("http://localhost:3000", "tok", namespace="root")
        mgr._client = AsyncMock()
        mgr._client.get = AsyncMock(
            return_value=_mock_response({"clone_url": "http://x/root/app.git"})
        )

        assert await mgr.repo_exists("app") is True

    @pytest.mark.asyncio
    async def test_false_on_404(self):
        mgr = GiteaManager("http://localhost:3000", "tok", namespace="root")
        mgr._client = AsyncMock()
        mgr._client.get = AsyncMock(
            return_value=_mock_response({"message": "not found"}, status_code=404)
        )

        assert await mgr.repo_exists("app") is False

    @pytest.mark.asyncio
    async def test_reraises_non_404_error(self):
        mgr = GiteaManager("http://localhost:3000", "tok", namespace="root")
        mgr._client = AsyncMock()
        mgr._client.get = AsyncMock(
            return_value=_mock_response({"message": "server error"}, status_code=500)
        )

        with pytest.raises(httpx.HTTPStatusError):
            await mgr.repo_exists("app")


class TestCreateRepo:
    @pytest.mark.asyncio
    async def test_raises_if_not_connected(self):
        mgr = GiteaManager("http://localhost:3000", "tok")
        with pytest.raises(RuntimeError):
            await mgr.create_repo("My App")

    @pytest.mark.asyncio
    async def test_creates_under_user_namespace_when_namespace_matches_username(self):
        mgr = GiteaManager("http://localhost:3000", "tok", namespace="root")
        mgr._username = "root"
        mgr._client = AsyncMock()
        mgr._client.get = AsyncMock(
            return_value=_mock_response({"message": "not found"}, status_code=404)
        )
        mgr._client.post = AsyncMock(
            return_value=_mock_response(
                {"clone_url": "http://localhost:3000/root/my-app.git"}
            )
        )

        url = await mgr.create_repo("My App", "desc")

        assert url == "http://localhost:3000/root/my-app.git"
        post_url = mgr._client.post.call_args.args[0]
        assert post_url == "http://localhost:3000/api/v1/user/repos"
        payload = mgr._client.post.call_args.kwargs["json"]
        # Gitea has no separate display-name/path fields like GitLab does —
        # "name" doubles as the URL path segment, so it must be a slug.
        assert payload["name"] == "my-app"
        assert payload["private"] is True
        assert payload["auto_init"] is False

    @pytest.mark.asyncio
    async def test_creates_under_org_namespace_when_namespace_differs_from_username(
        self,
    ):
        mgr = GiteaManager("http://localhost:3000", "tok", namespace="myteam")
        mgr._username = "alice"
        mgr._client = AsyncMock()
        mgr._client.get = AsyncMock(
            return_value=_mock_response({"message": "not found"}, status_code=404)
        )
        mgr._client.post = AsyncMock(
            return_value=_mock_response(
                {"clone_url": "http://localhost:3000/myteam/my-app.git"}
            )
        )

        url = await mgr.create_repo("My App")

        assert url == "http://localhost:3000/myteam/my-app.git"
        post_url = mgr._client.post.call_args.args[0]
        assert post_url == "http://localhost:3000/api/v1/orgs/myteam/repos"

    @pytest.mark.asyncio
    async def test_skips_creation_when_repo_already_exists(self):
        mgr = GiteaManager("http://localhost:3000", "tok", namespace="root")
        mgr._username = "root"
        mgr._client = AsyncMock()
        mgr._client.get = AsyncMock(
            return_value=_mock_response(
                {"clone_url": "http://localhost:3000/root/my-app.git"}
            )
        )
        mgr._client.post = AsyncMock()

        url = await mgr.create_repo("My App")

        assert url == "http://localhost:3000/root/my-app.git"
        mgr._client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_clone_url_derived_from_configured_base_not_root_url(self):
        """The returned clone URL must use Marcus's own GITEA_URL, not
        the server-reported clone_url.

        Gitea builds the API's clone_url from its browser-facing ROOT_URL
        config (http://localhost:3000/ in docker-compose.yml) regardless of
        the address the API caller used. In Docker mode Marcus reaches
        Gitea at http://gitea:3000 — pushing to a localhost:3000 clone_url
        from inside the marcus container hits nothing and the initial push
        fails, so the project never gets a repo mapping. The URL Marcus
        pushes to must therefore be derived from the URL Marcus itself is
        configured to reach Gitea on.
        """
        mgr = GiteaManager("http://gitea:3000", "tok", namespace="root")
        mgr._username = "root"
        mgr._client = AsyncMock()
        mgr._client.get = AsyncMock(
            return_value=_mock_response({"message": "not found"}, status_code=404)
        )
        # Server reports the ROOT_URL-based clone_url (browser-facing).
        mgr._client.post = AsyncMock(
            return_value=_mock_response(
                {"clone_url": "http://localhost:3000/root/my-app.git"}
            )
        )

        url = await mgr.create_repo("My App")

        assert url == "http://gitea:3000/root/my-app.git"

    @pytest.mark.asyncio
    async def test_existing_repo_clone_url_also_derived_from_base(self):
        """The already-exists path derives the URL the same way (and needs
        no second GET — existence was already confirmed)."""
        mgr = GiteaManager("http://gitea:3000", "tok", namespace="root")
        mgr._username = "root"
        mgr._client = AsyncMock()
        mgr._client.get = AsyncMock(
            return_value=_mock_response(
                {"clone_url": "http://localhost:3000/root/my-app.git"}
            )
        )
        mgr._client.post = AsyncMock()

        url = await mgr.create_repo("My App")

        assert url == "http://gitea:3000/root/my-app.git"
        mgr._client.post.assert_not_called()


class TestCreateWebhook:
    @pytest.mark.asyncio
    async def test_raises_if_not_connected(self):
        mgr = GiteaManager("http://localhost:3000", "tok")
        with pytest.raises(RuntimeError):
            await mgr.create_webhook("app", "http://marcus:4298/webhooks/gitea", "sekret")

    @pytest.mark.asyncio
    async def test_creates_webhook_when_none_exists(self):
        mgr = GiteaManager("http://localhost:3000", "tok", namespace="root")
        mgr._client = AsyncMock()
        mgr._client.get = AsyncMock(return_value=_mock_response([]))
        mgr._client.post = AsyncMock(return_value=_mock_response({"id": 1}))

        created = await mgr.create_webhook(
            "app", "http://marcus:4298/webhooks/gitea", "sekret"
        )

        assert created is True
        post_url = mgr._client.post.call_args.args[0]
        assert post_url == "http://localhost:3000/api/v1/repos/root/app/hooks"
        payload = mgr._client.post.call_args.kwargs["json"]
        assert payload["type"] == "gitea"
        assert payload["config"]["url"] == "http://marcus:4298/webhooks/gitea"
        assert payload["config"]["secret"] == "sekret"
        assert payload["events"] == ["push"]
        assert payload["active"] is True

    @pytest.mark.asyncio
    async def test_skips_creation_when_webhook_already_points_at_same_url(self):
        mgr = GiteaManager("http://localhost:3000", "tok", namespace="root")
        mgr._client = AsyncMock()
        mgr._client.get = AsyncMock(
            return_value=_mock_response(
                [{"id": 1, "config": {"url": "http://marcus:4298/webhooks/gitea"}}]
            )
        )
        mgr._client.post = AsyncMock()

        created = await mgr.create_webhook(
            "app", "http://marcus:4298/webhooks/gitea", "sekret"
        )

        assert created is False
        mgr._client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_when_existing_hooks_point_elsewhere(self):
        """A repo with an unrelated webhook must still get the Marcus one."""
        mgr = GiteaManager("http://localhost:3000", "tok", namespace="root")
        mgr._client = AsyncMock()
        mgr._client.get = AsyncMock(
            return_value=_mock_response(
                [{"id": 1, "config": {"url": "http://other-ci.example.com/hook"}}]
            )
        )
        mgr._client.post = AsyncMock(return_value=_mock_response({"id": 2}))

        created = await mgr.create_webhook(
            "app", "http://marcus:4298/webhooks/gitea", "sekret"
        )

        assert created is True
        mgr._client.post.assert_called_once()


class TestInitWithReadme:
    @pytest.mark.asyncio
    async def test_runs_git_commands_with_authenticated_push_url(self, tmp_path):
        mgr = GiteaManager("http://localhost:3000", "tok")
        mgr._username = "root"

        run_calls = []

        async def fake_run_git(args, cwd):
            run_calls.append(args)

        local_path = str(tmp_path / "my-app")
        with patch(
            "src.integrations.gitea_manager._run_git", side_effect=fake_run_git
        ):
            await mgr.init_with_readme(
                "http://localhost:3000/root/my-app.git", local_path
            )

        remote_add = next(c for c in run_calls if c[:2] == ["git", "remote"])
        assert remote_add[-1] == "http://root:tok@localhost:3000/root/my-app.git"
        assert ["git", "push", "-u", "origin", "main"] in run_calls

    @pytest.mark.asyncio
    async def test_creates_readme_when_absent(self, tmp_path):
        mgr = GiteaManager("http://localhost:3000", "tok")
        mgr._username = "root"

        local_path = str(tmp_path / "my-app")
        with patch("src.integrations.gitea_manager._run_git", new=AsyncMock()):
            await mgr.init_with_readme(
                "http://localhost:3000/root/my-app.git", local_path
            )

        readme = tmp_path / "my-app" / "README.md"
        assert readme.exists()
        assert "My App" in readme.read_text()

    @pytest.mark.asyncio
    async def test_fresh_init_commits_staged_readme(self, tmp_path):
        """A genuinely fresh init (staged changes present) commits them."""
        mgr = GiteaManager("http://localhost:3000", "tok")
        mgr._username = "root"

        run_calls = []

        async def fake_run_git(args, cwd):
            run_calls.append(args)
            if args[:4] == ["git", "diff", "--cached", "--quiet"]:
                # Non-zero exit = staged changes exist.
                raise RuntimeError("git diff --cached --quiet failed (rc 1)")

        local_path = str(tmp_path / "my-app")
        with patch(
            "src.integrations.gitea_manager._run_git", side_effect=fake_run_git
        ):
            await mgr.init_with_readme(
                "http://localhost:3000/root/my-app.git", local_path
            )

        assert any(c[:2] == ["git", "commit"] for c in run_calls)

    @pytest.mark.asyncio
    async def test_retry_over_partial_prior_attempt_succeeds(self, tmp_path):
        """Re-running over a half-completed earlier attempt must not raise.

        Regression: init_with_readme was not idempotent — after a first
        attempt that committed but failed on the network push, every retry
        died on `git commit` ("nothing to commit") and then `git remote
        add` ("remote origin already exists"), so ensure_repo() failed
        permanently for that project after ONE transient failure. A retry
        now skips the empty commit and updates the existing remote's URL
        instead of re-adding it, then reaches the push.
        """
        mgr = GiteaManager("http://localhost:3000", "tok")
        mgr._username = "root"

        run_calls = []

        async def fake_run_git(args, cwd):
            run_calls.append(args)
            # Clean staged tree (diff --cached exits 0 → no exception),
            # committed by the earlier attempt.
            if args[:3] == ["git", "remote", "add"]:
                raise RuntimeError("fatal: remote origin already exists.")

        local_path = str(tmp_path / "my-app")
        (tmp_path / "my-app").mkdir()
        (tmp_path / "my-app" / "README.md").write_text("# My App\n")

        with patch(
            "src.integrations.gitea_manager._run_git", side_effect=fake_run_git
        ):
            await mgr.init_with_readme(
                "http://localhost:3000/root/my-app.git", local_path
            )

        # No empty re-commit.
        assert not any(c[:2] == ["git", "commit"] for c in run_calls)
        # Existing remote gets its URL refreshed (also covers rotated tokens).
        set_url = next(c for c in run_calls if c[:3] == ["git", "remote", "set-url"])
        assert set_url[-1] == "http://root:tok@localhost:3000/root/my-app.git"
        # And the push is finally reached.
        assert ["git", "push", "-u", "origin", "main"] in run_calls


class TestMirrorClone:
    """Test mirror_clone() — used by the clone-project feature to
    replicate a baseline project's ENTIRE git history (every branch and
    tag, under their original names) into a brand-new Gitea repo."""

    @pytest.mark.asyncio
    async def test_mirror_clones_source_then_mirror_pushes_to_dest(self, tmp_path):
        mgr = GiteaManager("http://localhost:3000", "tok")
        mgr._username = "root"

        run_calls = []

        async def fake_run_git(args, cwd):
            run_calls.append((tuple(args), cwd))

        local_path = str(tmp_path / "cloned-app")
        with patch(
            "src.integrations.gitea_manager._run_git", side_effect=fake_run_git
        ):
            await mgr.mirror_clone(
                "http://localhost:3000/root/orig-app.git",
                "http://localhost:3000/root/cloned-app.git",
                local_path,
            )

        args_only = [c[0] for c in run_calls]
        mirror_clone_call = next(
            c for c in args_only if c[:3] == ("git", "clone", "--mirror")
        )
        assert mirror_clone_call[3] == "http://root:tok@localhost:3000/root/orig-app.git"

        mirror_push_call = next(
            c for c in args_only if c[:3] == ("git", "push", "--mirror")
        )
        assert (
            mirror_push_call[3]
            == "http://root:tok@localhost:3000/root/cloned-app.git"
        )
        # The mirror push must run inside the just-cloned bare mirror dir,
        # not the eventual working-tree local_path.
        push_cwd = next(cwd for (a, cwd) in run_calls if a[:3] == ("git", "push", "--mirror"))
        assert push_cwd == mirror_clone_call[4]

        # Order: mirror clone before mirror push, before the final
        # normal working-tree clone at local_path.
        working_clone_call = next(
            c
            for c in args_only
            if c[:2] == ("git", "clone") and c[2] != "--mirror"
        )
        assert working_clone_call[2] == (
            "http://root:tok@localhost:3000/root/cloned-app.git"
        )
        assert working_clone_call[3] == local_path
        assert args_only.index(mirror_clone_call) < args_only.index(mirror_push_call)
        assert args_only.index(mirror_push_call) < args_only.index(working_clone_call)

    @pytest.mark.asyncio
    async def test_configures_git_identity_on_working_clone(self, tmp_path):
        mgr = GiteaManager("http://localhost:3000", "tok")
        mgr._username = "root"
        run_calls = []

        async def fake_run_git(args, cwd):
            run_calls.append(tuple(args))

        local_path = str(tmp_path / "cloned-app")
        with patch(
            "src.integrations.gitea_manager._run_git", side_effect=fake_run_git
        ):
            await mgr.mirror_clone(
                "http://localhost:3000/root/orig-app.git",
                "http://localhost:3000/root/cloned-app.git",
                local_path,
            )

        assert ("git", "config", "user.email", "marcus@localhost") in run_calls
        assert ("git", "config", "user.name", "Marcus") in run_calls

    @pytest.mark.asyncio
    async def test_raises_when_mirror_clone_fails(self, tmp_path):
        mgr = GiteaManager("http://localhost:3000", "tok")
        mgr._username = "root"

        async def fake_run_git(args, cwd):
            if args[:3] == ["git", "clone", "--mirror"]:
                raise RuntimeError("fatal: repository not found")

        local_path = str(tmp_path / "cloned-app")
        with patch(
            "src.integrations.gitea_manager._run_git", side_effect=fake_run_git
        ):
            with pytest.raises(RuntimeError, match="repository not found"):
                await mgr.mirror_clone(
                    "http://localhost:3000/root/orig-app.git",
                    "http://localhost:3000/root/cloned-app.git",
                    local_path,
                )

    @pytest.mark.asyncio
    async def test_raises_when_mirror_push_fails(self, tmp_path):
        mgr = GiteaManager("http://localhost:3000", "tok")
        mgr._username = "root"

        async def fake_run_git(args, cwd):
            if args[:3] == ["git", "push", "--mirror"]:
                raise RuntimeError("fatal: repository not found")

        local_path = str(tmp_path / "cloned-app")
        with patch(
            "src.integrations.gitea_manager._run_git", side_effect=fake_run_git
        ):
            with pytest.raises(RuntimeError, match="repository not found"):
                await mgr.mirror_clone(
                    "http://localhost:3000/root/orig-app.git",
                    "http://localhost:3000/root/cloned-app.git",
                    local_path,
                )
