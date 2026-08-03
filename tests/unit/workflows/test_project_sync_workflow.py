"""
Unit tests for src/workflows/project_sync_workflow.py

ProjectSyncWorkflow reacts to ``project.created`` events by creating a Gitea
repo and persisting the Kanboard-project → Gitea-repo mapping.  All external
collaborators (GiteaManager, Events, disk I/O) are mocked or redirected to
tmp_path.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.workflows.project_sync_workflow import ProjectSyncWorkflow


def _make_event(pid: int, name: str, description: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        data={
            "kanboard_project_id": pid,
            "project_name": name,
            "project_description": description,
        }
    )


@pytest.fixture()
def gitea_mgr():
    mgr = MagicMock()
    mgr.create_repo = AsyncMock(return_value="http://localhost:3000/root/shopping-cart.git")
    mgr.init_with_readme = AsyncMock(return_value=None)
    return mgr


@pytest.fixture()
def workflow(tmp_path, gitea_mgr):
    events = MagicMock()
    events.subscribe = MagicMock()
    return ProjectSyncWorkflow(
        gitea_manager=gitea_mgr,
        events=events,
        repos_path=str(tmp_path / "project_repos.json"),
        local_repos_base=str(tmp_path / "repos"),
    )


class TestSubscribe:
    def test_subscribes_to_project_created(self, workflow):
        workflow.subscribe()
        workflow._events.subscribe.assert_called_once_with(
            "project.created", workflow._on_project_created
        )


class TestOnProjectCreated:
    @pytest.mark.asyncio
    async def test_creates_repo_and_persists_mapping(self, workflow, gitea_mgr):
        await workflow._on_project_created(_make_event(1, "Shopping Cart", "desc"))

        # ensure_repo passes the resolved (possibly disambiguated) slug —
        # create_repo slugifies its argument, so this is equivalent for
        # the non-colliding case and REQUIRED for the colliding one.
        gitea_mgr.create_repo.assert_called_once_with("shopping-cart", "desc")
        gitea_mgr.init_with_readme.assert_called_once()

        mapping = workflow.get_repo_for_project(1)
        assert mapping is not None
        assert mapping["gitea_repo_url"] == "http://localhost:3000/root/shopping-cart.git"
        assert mapping["kanboard_project_name"] == "Shopping Cart"
        assert mapping["local_repo_path"].endswith("shopping-cart")

    @pytest.mark.asyncio
    async def test_persists_mapping_to_disk(self, workflow, tmp_path):
        await workflow._on_project_created(_make_event(1, "Shopping Cart"))

        raw = json.loads((tmp_path / "project_repos.json").read_text())
        assert raw["kanboard:1"]["gitea_repo_url"] == (
            "http://localhost:3000/root/shopping-cart.git"
        )

    @pytest.mark.asyncio
    async def test_duplicate_project_created_is_skipped(self, workflow, gitea_mgr):
        await workflow._on_project_created(_make_event(1, "Shopping Cart"))
        gitea_mgr.create_repo.reset_mock()

        await workflow._on_project_created(_make_event(1, "Shopping Cart"))

        gitea_mgr.create_repo.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_repo_failure_does_not_persist_mapping(self, workflow, gitea_mgr):
        gitea_mgr.create_repo = AsyncMock(side_effect=RuntimeError("Gitea unreachable"))

        await workflow._on_project_created(_make_event(2, "Other Project"))

        assert workflow.get_repo_for_project(2) is None

    @pytest.mark.asyncio
    async def test_init_with_readme_failure_does_not_persist_mapping(
        self, workflow, gitea_mgr
    ):
        gitea_mgr.init_with_readme = AsyncMock(side_effect=RuntimeError("push failed"))

        await workflow._on_project_created(_make_event(3, "Third Project"))

        assert workflow.get_repo_for_project(3) is None


class TestEnsureRepo:
    @pytest.mark.asyncio
    async def test_creates_repo_and_returns_mapping(self, workflow, gitea_mgr):
        mapping = await workflow.ensure_repo(1, "Shopping Cart", "desc")

        # ensure_repo passes the resolved (possibly disambiguated) slug —
        # create_repo slugifies its argument, so this is equivalent for
        # the non-colliding case and REQUIRED for the colliding one.
        gitea_mgr.create_repo.assert_called_once_with("shopping-cart", "desc")
        assert mapping is not None
        assert mapping["gitea_repo_url"] == "http://localhost:3000/root/shopping-cart.git"

    @pytest.mark.asyncio
    async def test_second_call_is_idempotent(self, workflow, gitea_mgr):
        first = await workflow.ensure_repo(1, "Shopping Cart")
        gitea_mgr.create_repo.reset_mock()

        second = await workflow.ensure_repo(1, "Shopping Cart")

        gitea_mgr.create_repo.assert_not_called()
        assert second == first

    @pytest.mark.asyncio
    async def test_returns_none_on_create_repo_failure(self, workflow, gitea_mgr):
        gitea_mgr.create_repo = AsyncMock(side_effect=RuntimeError("Gitea unreachable"))

        result = await workflow.ensure_repo(2, "Other Project")

        assert result is None
        assert workflow.get_repo_for_project(2) is None

    @pytest.mark.asyncio
    async def test_on_project_created_delegates_to_ensure_repo(self, workflow, gitea_mgr):
        await workflow._on_project_created(_make_event(1, "Shopping Cart", "desc"))

        # ensure_repo passes the resolved (possibly disambiguated) slug —
        # create_repo slugifies its argument, so this is equivalent for
        # the non-colliding case and REQUIRED for the colliding one.
        gitea_mgr.create_repo.assert_called_once_with("shopping-cart", "desc")
        assert workflow.get_repo_for_project(1) is not None


class TestSlugDisambiguation:
    """Slug collisions and empty slugs must not cross-wire projects.

    create_repo() treats "repo already exists" as "already provisioned"
    and returns the existing repo's URL — so two Kanboard projects whose
    names slugify identically ("My App" / "my app!") were silently
    mapped to the SAME Gitea repo and the SAME local clone, merging both
    projects' tickets into one main. All-symbol names slugified to ""
    and permanently failed provisioning instead.
    """

    @pytest.mark.asyncio
    async def test_colliding_slug_gets_project_id_suffix(
        self, workflow, gitea_mgr
    ):
        """Second project with the same slug gets a disambiguated repo."""
        gitea_mgr.create_webhook = AsyncMock(return_value=True)
        await workflow.ensure_repo(1, "My App")
        await workflow.ensure_repo(2, "my app!")

        second_repo_name = gitea_mgr.create_repo.call_args_list[1].args[0]
        assert second_repo_name == "my-app-p2"
        first = workflow.get_repo_for_project(1)
        second = workflow.get_repo_for_project(2)
        assert first["local_repo_path"] != second["local_repo_path"]

    @pytest.mark.asyncio
    async def test_same_project_reensure_is_not_disambiguated(
        self, workflow, gitea_mgr
    ):
        """Re-ensuring the SAME project hits the cache, no new repo."""
        gitea_mgr.create_webhook = AsyncMock(return_value=True)
        await workflow.ensure_repo(1, "My App")
        await workflow.ensure_repo(1, "My App")
        assert gitea_mgr.create_repo.call_count == 1

    @pytest.mark.asyncio
    async def test_empty_slug_falls_back_to_project_id(
        self, workflow, gitea_mgr
    ):
        """An all-symbol name provisions under project-<id>, not ""."""
        gitea_mgr.create_webhook = AsyncMock(return_value=True)
        mapping = await workflow.ensure_repo(7, "Проект!!!")

        repo_name = gitea_mgr.create_repo.call_args.args[0]
        assert repo_name == "project-7"
        assert mapping is not None
        assert mapping["local_repo_path"].endswith("project-7")


class TestEnsureWebhook:
    @pytest.mark.asyncio
    async def test_no_webhook_call_when_not_configured(self, workflow, gitea_mgr):
        gitea_mgr.create_webhook = AsyncMock(return_value=True)

        await workflow.ensure_repo(1, "Shopping Cart")

        gitea_mgr.create_webhook.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_webhook_when_configured(self, tmp_path, gitea_mgr):
        gitea_mgr.create_webhook = AsyncMock(return_value=True)
        events = MagicMock()
        wf = ProjectSyncWorkflow(
            gitea_manager=gitea_mgr,
            events=events,
            repos_path=str(tmp_path / "project_repos.json"),
            local_repos_base=str(tmp_path / "repos"),
            webhook_target_url="http://marcus:8080/webhooks/gitea",
            webhook_secret="s3cret",
        )

        await wf.ensure_repo(1, "Shopping Cart")

        gitea_mgr.create_webhook.assert_called_once_with(
            "shopping-cart", "http://marcus:8080/webhooks/gitea", "s3cret"
        )

    @pytest.mark.asyncio
    async def test_webhook_failure_does_not_block_mapping_persistence(
        self, tmp_path, gitea_mgr
    ):
        gitea_mgr.create_webhook = AsyncMock(side_effect=RuntimeError("gitea down"))
        events = MagicMock()
        wf = ProjectSyncWorkflow(
            gitea_manager=gitea_mgr,
            events=events,
            repos_path=str(tmp_path / "project_repos.json"),
            local_repos_base=str(tmp_path / "repos"),
            webhook_target_url="http://marcus:8080/webhooks/gitea",
            webhook_secret="s3cret",
        )

        mapping = await wf.ensure_repo(1, "Shopping Cart")

        assert mapping is not None
        assert wf.get_repo_for_project(1) is not None

    @pytest.mark.asyncio
    async def test_retries_webhook_on_next_lookup_after_a_failed_attempt(
        self, tmp_path, gitea_mgr
    ):
        """A webhook that failed on first creation must not be permanently
        given up on — the next ensure_repo() call for the same project
        retries it."""
        gitea_mgr.create_webhook = AsyncMock(side_effect=RuntimeError("gitea down"))
        events = MagicMock()
        wf = ProjectSyncWorkflow(
            gitea_manager=gitea_mgr,
            events=events,
            repos_path=str(tmp_path / "project_repos.json"),
            local_repos_base=str(tmp_path / "repos"),
            webhook_target_url="http://marcus:8080/webhooks/gitea",
            webhook_secret="s3cret",
        )
        await wf.ensure_repo(1, "Shopping Cart")
        gitea_mgr.create_repo.assert_called_once()

        gitea_mgr.create_webhook = AsyncMock(return_value=True)
        await wf.ensure_repo(1, "Shopping Cart")

        gitea_mgr.create_webhook.assert_called_once_with(
            "shopping-cart", "http://marcus:8080/webhooks/gitea", "s3cret"
        )
        gitea_mgr.create_repo.assert_called_once()  # still not re-created

    @pytest.mark.asyncio
    async def test_no_retry_once_webhook_confirmed(self, tmp_path, gitea_mgr):
        """Once a webhook is confirmed created, subsequent lookups don't
        re-attempt it — avoids a network round-trip on every cache hit."""
        gitea_mgr.create_webhook = AsyncMock(return_value=True)
        events = MagicMock()
        wf = ProjectSyncWorkflow(
            gitea_manager=gitea_mgr,
            events=events,
            repos_path=str(tmp_path / "project_repos.json"),
            local_repos_base=str(tmp_path / "repos"),
            webhook_target_url="http://marcus:8080/webhooks/gitea",
            webhook_secret="s3cret",
        )
        await wf.ensure_repo(1, "Shopping Cart")
        gitea_mgr.create_webhook.reset_mock()

        await wf.ensure_repo(1, "Shopping Cart")

        gitea_mgr.create_webhook.assert_not_called()

    @pytest.mark.asyncio
    async def test_retries_webhook_for_a_mapping_persisted_before_webhook_support(
        self, tmp_path, gitea_mgr
    ):
        """A mapping written to disk before this feature existed (or before
        GITEA_WEBHOOK_TOKEN was set) has no webhook_created key at all —
        the next lookup must still attempt to create the webhook."""
        repos_path = tmp_path / "project_repos.json"
        repos_path.write_text(
            json.dumps(
                {
                    "kanboard:1": {
                        "kanboard_project_id": 1,
                        "kanboard_project_name": "Shopping Cart",
                        "gitea_repo_url": "http://localhost:3000/root/shopping-cart.git",
                        "local_repo_path": "./repos/shopping-cart",
                    }
                }
            )
        )
        gitea_mgr.create_webhook = AsyncMock(return_value=True)
        events = MagicMock()
        wf = ProjectSyncWorkflow(
            gitea_manager=gitea_mgr,
            events=events,
            repos_path=str(repos_path),
            local_repos_base=str(tmp_path / "repos"),
            webhook_target_url="http://marcus:8080/webhooks/gitea",
            webhook_secret="s3cret",
        )

        await wf.ensure_repo(1, "Shopping Cart")

        gitea_mgr.create_webhook.assert_called_once_with(
            "shopping-cart", "http://marcus:8080/webhooks/gitea", "s3cret"
        )
        gitea_mgr.create_repo.assert_not_called()


class TestGetRepoForProject:
    def test_returns_none_when_unmapped(self, workflow):
        assert workflow.get_repo_for_project(999) is None

    @pytest.mark.asyncio
    async def test_all_mappings_returns_copy(self, workflow):
        await workflow._on_project_created(_make_event(1, "Shopping Cart"))
        mappings = workflow.all_mappings()
        mappings["kanboard:1"]["gitea_repo_url"] = "mutated"
        assert workflow.get_repo_for_project(1)["gitea_repo_url"] != "mutated"


class TestGetProjectIdForRepoName:
    """Reverse lookup used by the Gitea webhook receiver to route a push
    to a project's main branch to the correct project's preview — unlike
    ticket branches (globally unique ticket/<provider>/<id> names), a
    branch literally named "main" is not unique across projects, so
    resolving repo -> project explicitly is required to avoid refreshing
    the wrong project's preview."""

    @pytest.mark.asyncio
    async def test_matches_by_repo_slug(self, workflow):
        await workflow._on_project_created(_make_event(1, "Shopping Cart"))
        assert workflow.get_project_id_for_repo_name("shopping-cart") == 1

    def test_returns_none_when_no_match(self, workflow):
        assert workflow.get_project_id_for_repo_name("nonexistent-repo") is None

    def test_returns_none_on_empty_mapping_table(self, workflow):
        assert workflow.all_mappings() == {}
        assert workflow.get_project_id_for_repo_name("anything") is None

    def test_falls_back_to_slugified_name_for_pre_repo_slug_mapping(self, workflow):
        """A mapping written before the repo_slug field existed has no
        such key — ensure_repo() already falls back to re-slugifying
        kanboard_project_name for this exact case (see its own docstring
        at the matching `cached.get("repo_slug") or _slugify(...)` line);
        this reverse lookup must degrade the same way, or a project whose
        mapping predates that field silently never gets its main-branch
        preview refreshed on push (no error, just a permanent no-op)."""
        workflow._mapping["kanboard:9"] = {
            "kanboard_project_id": 9,
            "kanboard_project_name": "Shopping Cart",
            "gitea_repo_url": "http://localhost:3000/root/shopping-cart.git",
            "local_repo_path": "./repos/shopping-cart",
            # No "repo_slug" key — pre-repo_slug mapping file.
        }

        assert workflow.get_project_id_for_repo_name("shopping-cart") == 9

    @pytest.mark.asyncio
    async def test_distinguishes_between_multiple_projects(self, workflow, gitea_mgr):
        gitea_mgr.create_repo = AsyncMock(
            side_effect=[
                "http://localhost:3000/root/shopping-cart.git",
                "http://localhost:3000/root/inventory.git",
            ]
        )
        await workflow._on_project_created(_make_event(1, "Shopping Cart"))
        await workflow._on_project_created(_make_event(2, "Inventory"))
        assert workflow.get_project_id_for_repo_name("shopping-cart") == 1
        assert workflow.get_project_id_for_repo_name("inventory") == 2


class TestLoadMapping:
    def test_loads_existing_mapping_from_disk(self, tmp_path, gitea_mgr):
        repos_path = tmp_path / "project_repos.json"
        repos_path.write_text(
            json.dumps(
                {
                    "kanboard:5": {
                        "kanboard_project_id": 5,
                        "kanboard_project_name": "Existing",
                        "gitea_repo_url": "http://localhost:3000/root/existing.git",
                        "local_repo_path": "./repos/existing",
                    }
                }
            )
        )
        events = MagicMock()
        wf = ProjectSyncWorkflow(
            gitea_manager=gitea_mgr,
            events=events,
            repos_path=str(repos_path),
            local_repos_base=str(tmp_path / "repos"),
        )
        assert wf.get_repo_for_project(5)["gitea_repo_url"] == (
            "http://localhost:3000/root/existing.git"
        )

    def test_missing_file_starts_empty(self, tmp_path, gitea_mgr):
        events = MagicMock()
        wf = ProjectSyncWorkflow(
            gitea_manager=gitea_mgr,
            events=events,
            repos_path=str(tmp_path / "does_not_exist.json"),
            local_repos_base=str(tmp_path / "repos"),
        )
        assert wf.all_mappings() == {}

    def test_corrupt_file_starts_empty(self, tmp_path, gitea_mgr):
        repos_path = tmp_path / "project_repos.json"
        repos_path.write_text("NOT JSON {{{")
        events = MagicMock()
        wf = ProjectSyncWorkflow(
            gitea_manager=gitea_mgr,
            events=events,
            repos_path=str(repos_path),
            local_repos_base=str(tmp_path / "repos"),
        )
        assert wf.all_mappings() == {}
