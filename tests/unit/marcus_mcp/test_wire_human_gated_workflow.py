"""
Unit tests for ``_wire_human_gated_workflow`` in src/marcus_mcp/server.py

This is the function that constructs GiteaManager -> ProjectSyncWorkflow ->
HumanGatedWorkflow at HTTP-server startup and registers the workflow so the
human-gated MCP tools (get_work_context, signal_ready_for_review, etc.)
stop returning "HumanGatedWorkflow not initialised". All collaborators
(GiteaManager, HumanGatedWorkflow, ProjectSyncWorkflow, register_workflow)
are patched — no real network or subprocess calls.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.events import Events
from src.marcus_mcp.server import (
    _get_dev_env_settings_mgr,
    _get_gate_settings_mgr,
    _read_bounded_body,
    _resolve_ticket_branch,
    _resolve_ticket_repo_path,
    _verify_ticket_belongs_to_project,
    _wire_human_gated_workflow,
)


def _make_server(events=MagicMock(), kanban_client=MagicMock(), provider="kanboard"):
    return SimpleNamespace(events=events, kanban_client=kanban_client, provider=provider)


class TestSkipsWhenPrerequisitesMissing:
    @pytest.mark.asyncio
    async def test_skips_when_events_is_none(self):
        server = _make_server(events=None)
        with patch("src.workflows.human_gated_workflow.HumanGatedWorkflow") as wf_cls:
            await _wire_human_gated_workflow(server)
        wf_cls.assert_not_called()
        assert not hasattr(server, "_human_gated_workflow")

    @pytest.mark.asyncio
    async def test_skips_when_kanban_client_is_none(self):
        server = _make_server(kanban_client=None)
        with patch("src.workflows.human_gated_workflow.HumanGatedWorkflow") as wf_cls:
            await _wire_human_gated_workflow(server)
        wf_cls.assert_not_called()
        assert not hasattr(server, "_human_gated_workflow")


class TestConstructsWorkflowWithoutGitea:
    @pytest.mark.asyncio
    async def test_starts_workflow_when_gitea_env_vars_absent(self, monkeypatch):
        monkeypatch.delenv("GITEA_URL", raising=False)
        monkeypatch.delenv("GITEA_TOKEN", raising=False)
        server = _make_server()

        mock_workflow = AsyncMock()
        with (
            patch(
                "src.workflows.human_gated_workflow.HumanGatedWorkflow",
                return_value=mock_workflow,
            ) as wf_cls,
            patch("src.marcus_mcp.tools.human_gated.register_workflow") as register,
            patch("src.integrations.gitea_manager.GiteaManager") as gitea_cls,
        ):
            await _wire_human_gated_workflow(server)

        gitea_cls.assert_not_called()
        wf_cls.assert_called_once()
        kwargs = wf_cls.call_args.kwargs
        assert kwargs["kanban"] is server.kanban_client
        assert kwargs["events"] is server.events
        assert kwargs["provider_name"] == "kanboard"
        assert kwargs["project_sync"] is None
        register.assert_called_once_with(mock_workflow)
        mock_workflow.start.assert_awaited_once()
        assert server._human_gated_workflow is mock_workflow
        assert server._project_sync is None


class TestConstructsWorkflowWithGitea:
    @pytest.mark.asyncio
    async def test_wires_project_sync_and_webhook_when_gitea_configured(self, monkeypatch):
        monkeypatch.setenv("GITEA_URL", "http://gitea:3000")
        monkeypatch.setenv("GITEA_TOKEN", "tok123")
        monkeypatch.setenv("GITEA_WEBHOOK_TOKEN", "s3cret")
        server = _make_server()

        mock_gitea = AsyncMock()
        mock_gitea.connect = AsyncMock(return_value=True)
        mock_workflow = AsyncMock()

        with (
            patch(
                "src.integrations.gitea_manager.GiteaManager", return_value=mock_gitea
            ) as gitea_cls,
            patch(
                "src.workflows.project_sync_workflow.ProjectSyncWorkflow"
            ) as sync_cls,
            patch(
                "src.workflows.human_gated_workflow.HumanGatedWorkflow",
                return_value=mock_workflow,
            ) as wf_cls,
            patch("src.marcus_mcp.tools.human_gated.register_workflow"),
        ):
            sync_instance = sync_cls.return_value
            await _wire_human_gated_workflow(server)

        gitea_cls.assert_called_once_with(
            gitea_url="http://gitea:3000", token="tok123", namespace=None
        )
        mock_gitea.connect.assert_awaited_once()
        sync_cls.assert_called_once()
        sync_kwargs = sync_cls.call_args.kwargs
        assert sync_kwargs["webhook_secret"] == "s3cret"
        assert sync_kwargs["webhook_target_url"] == "http://marcus:4298/webhooks/gitea"
        sync_instance.subscribe.assert_called_once()
        wf_kwargs = wf_cls.call_args.kwargs
        assert wf_kwargs["project_sync"] is sync_instance
        assert server._gitea_manager is mock_gitea

    @pytest.mark.asyncio
    async def test_no_webhook_target_when_secret_not_configured(self, monkeypatch):
        monkeypatch.setenv("GITEA_URL", "http://gitea:3000")
        monkeypatch.setenv("GITEA_TOKEN", "tok123")
        monkeypatch.delenv("GITEA_WEBHOOK_TOKEN", raising=False)
        server = _make_server()

        mock_gitea = AsyncMock()
        mock_gitea.connect = AsyncMock(return_value=True)

        with (
            patch("src.integrations.gitea_manager.GiteaManager", return_value=mock_gitea),
            patch("src.workflows.project_sync_workflow.ProjectSyncWorkflow") as sync_cls,
            patch("src.workflows.human_gated_workflow.HumanGatedWorkflow", return_value=AsyncMock()),
            patch("src.marcus_mcp.tools.human_gated.register_workflow"),
        ):
            await _wire_human_gated_workflow(server)

        sync_kwargs = sync_cls.call_args.kwargs
        assert sync_kwargs["webhook_secret"] is None
        assert sync_kwargs["webhook_target_url"] is None

    @pytest.mark.asyncio
    async def test_falls_back_to_no_project_sync_when_gitea_connect_fails(self, monkeypatch):
        monkeypatch.setenv("GITEA_URL", "http://gitea:3000")
        monkeypatch.setenv("GITEA_TOKEN", "tok123")
        server = _make_server()

        mock_gitea = AsyncMock()
        mock_gitea.connect = AsyncMock(return_value=False)
        mock_workflow = AsyncMock()

        with (
            patch("src.integrations.gitea_manager.GiteaManager", return_value=mock_gitea),
            patch("src.workflows.project_sync_workflow.ProjectSyncWorkflow") as sync_cls,
            patch(
                "src.workflows.human_gated_workflow.HumanGatedWorkflow",
                return_value=mock_workflow,
            ) as wf_cls,
            patch("src.marcus_mcp.tools.human_gated.register_workflow"),
        ):
            await _wire_human_gated_workflow(server)

        sync_cls.assert_not_called()
        wf_kwargs = wf_cls.call_args.kwargs
        assert wf_kwargs["project_sync"] is None
        assert not hasattr(server, "_gitea_manager")


class TestSharesDevEnvManagerSingleton:
    @pytest.mark.asyncio
    async def test_reuses_existing_dev_env_manager(self, monkeypatch):
        monkeypatch.delenv("GITEA_URL", raising=False)
        server = _make_server()
        existing_dev_mgr = MagicMock()
        server._dev_env_manager = existing_dev_mgr

        with (
            patch(
                "src.workflows.human_gated_workflow.HumanGatedWorkflow",
                return_value=AsyncMock(),
            ) as wf_cls,
            patch("src.marcus_mcp.tools.human_gated.register_workflow"),
            patch("src.core.dev_environment.DevEnvironmentManager") as dev_env_cls,
        ):
            await _wire_human_gated_workflow(server)

        dev_env_cls.assert_not_called()
        assert wf_cls.call_args.kwargs["dev_env_manager"] is existing_dev_mgr


class TestGetDevEnvSettingsMgr:
    """_get_dev_env_settings_mgr must return one shared instance per server.

    DevEnvSettingsManager caches its value in memory after the first disk
    read — a second, independently-constructed instance would silently
    diverge from a human's live change via /api/dev-env-setting, so every
    caller (both DevEnvironmentManager construction sites and the API
    route) must resolve to the exact same object.
    """

    def test_constructs_once_and_caches_on_server(self):
        server = SimpleNamespace()
        with patch("src.core.dev_env_settings.DevEnvSettingsManager") as mgr_cls:
            mgr_cls.return_value = MagicMock()
            first = _get_dev_env_settings_mgr(server)
            second = _get_dev_env_settings_mgr(server)

        mgr_cls.assert_called_once()
        assert first is second
        assert server._dev_env_settings_mgr is first

    def test_reuses_a_preexisting_instance(self):
        server = SimpleNamespace()
        existing = MagicMock()
        server._dev_env_settings_mgr = existing

        with patch("src.core.dev_env_settings.DevEnvSettingsManager") as mgr_cls:
            result = _get_dev_env_settings_mgr(server)

        mgr_cls.assert_not_called()
        assert result is existing


class TestGetGateSettingsMgr:
    """_get_gate_settings_mgr must return one shared instance per server.

    GateSettingManager loads its JSON file into memory once at
    construction and never re-reads it, so two instances silently
    diverge. The concrete failure this pins: a human flipping a project
    from "ai" to "human" gate via /api/gate-setting wrote through the
    route's instance while the workflow kept auto-merging off its own
    stale cache — unsupervised merges continuing AFTER the human turned
    them off, until the next restart.
    """

    def test_constructs_once_and_caches_on_server(self):
        server = SimpleNamespace()
        with patch("src.core.gate_settings.GateSettingManager") as mgr_cls:
            mgr_cls.return_value = MagicMock()
            first = _get_gate_settings_mgr(server)
            second = _get_gate_settings_mgr(server)

        mgr_cls.assert_called_once()
        assert first is second
        assert server._gate_mgr is first

    def test_reuses_a_preexisting_instance(self):
        server = SimpleNamespace()
        existing = MagicMock()
        server._gate_mgr = existing

        with patch("src.core.gate_settings.GateSettingManager") as mgr_cls:
            result = _get_gate_settings_mgr(server)

        mgr_cls.assert_not_called()
        assert result is existing

    @pytest.mark.asyncio
    async def test_workflow_receives_the_shared_gate_manager(self, monkeypatch):
        """_wire_human_gated_workflow passes the SAME instance the
        /api/gate-setting routes resolve — not a fresh one."""
        monkeypatch.delenv("GITEA_URL", raising=False)
        monkeypatch.delenv("GITEA_TOKEN", raising=False)
        server = _make_server()
        shared = MagicMock()
        server._gate_mgr = shared

        with (
            patch(
                "src.workflows.human_gated_workflow.HumanGatedWorkflow",
                return_value=AsyncMock(),
            ) as wf_cls,
            patch("src.marcus_mcp.tools.human_gated.register_workflow"),
        ):
            await _wire_human_gated_workflow(server)

        assert wf_cls.call_args.kwargs["gate_settings"] is shared


class TestResolveTicketBranch:
    """_resolve_ticket_branch prefers the lifecycle record over convention."""

    def test_no_human_gated_workflow_uses_convention(self):
        server = SimpleNamespace()
        assert _resolve_ticket_branch(server, "42", "kanboard") == "ticket/kanboard/42"

    def test_no_lifecycle_record_uses_convention(self):
        workflow = MagicMock()
        workflow._lifecycle.get = MagicMock(return_value=None)
        server = SimpleNamespace(_human_gated_workflow=workflow)
        assert _resolve_ticket_branch(server, "42", "kanboard") == "ticket/kanboard/42"

    def test_uses_the_records_real_branch_name(self):
        record = SimpleNamespace(branch_name="feature/custom-branch")
        workflow = MagicMock()
        workflow._lifecycle.get = MagicMock(return_value=record)
        server = SimpleNamespace(_human_gated_workflow=workflow)
        assert _resolve_ticket_branch(server, "42", "kanboard") == "feature/custom-branch"

    def test_empty_branch_name_on_record_falls_back_to_convention(self):
        record = SimpleNamespace(branch_name="")
        workflow = MagicMock()
        workflow._lifecycle.get = MagicMock(return_value=record)
        server = SimpleNamespace(_human_gated_workflow=workflow)
        assert _resolve_ticket_branch(server, "42", "kanboard") == "ticket/kanboard/42"


class TestResolveTicketRepoPath:
    """_resolve_ticket_repo_path resolves (or on-demand provisions) a repo path."""

    @pytest.mark.asyncio
    async def test_no_project_sync_returns_none(self):
        server = SimpleNamespace(_project_sync=None, kanban_client=MagicMock())
        assert await _resolve_ticket_repo_path(server, "1") is None

    @pytest.mark.asyncio
    async def test_empty_project_id_returns_none(self):
        server = SimpleNamespace(_project_sync=MagicMock(), kanban_client=MagicMock())
        assert await _resolve_ticket_repo_path(server, "") is None

    @pytest.mark.asyncio
    async def test_non_numeric_project_id_returns_none(self):
        server = SimpleNamespace(_project_sync=MagicMock(), kanban_client=MagicMock())
        assert await _resolve_ticket_repo_path(server, "not-a-number") is None

    @pytest.mark.asyncio
    async def test_returns_cached_mapping_without_provisioning(self):
        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(
            return_value={"local_repo_path": "./data/repos/shopping-cart"}
        )
        project_sync.ensure_repo = AsyncMock()
        server = SimpleNamespace(_project_sync=project_sync, kanban_client=MagicMock())

        result = await _resolve_ticket_repo_path(server, "1")

        assert result == "./data/repos/shopping-cart"
        project_sync.ensure_repo.assert_not_called()

    @pytest.mark.asyncio
    async def test_provisions_on_demand_when_no_mapping_and_name_resolvable(self):
        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(return_value=None)
        project_sync.ensure_repo = AsyncMock(
            return_value={"local_repo_path": "./data/repos/new-project"}
        )
        kanban = MagicMock()
        kanban.get_project_name = AsyncMock(return_value="New Project")
        server = SimpleNamespace(_project_sync=project_sync, kanban_client=kanban)

        result = await _resolve_ticket_repo_path(server, "5")

        project_sync.ensure_repo.assert_awaited_once_with(5, "New Project")
        assert result == "./data/repos/new-project"

    @pytest.mark.asyncio
    async def test_kanban_without_get_project_name_returns_none(self):
        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(return_value=None)
        project_sync.ensure_repo = AsyncMock()
        kanban = MagicMock(spec=[])  # no get_project_name attribute
        server = SimpleNamespace(_project_sync=project_sync, kanban_client=kanban)

        result = await _resolve_ticket_repo_path(server, "5")

        assert result is None
        project_sync.ensure_repo.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_project_name_failure_returns_none(self):
        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(return_value=None)
        project_sync.ensure_repo = AsyncMock()
        kanban = MagicMock()
        kanban.get_project_name = AsyncMock(side_effect=RuntimeError("kanban down"))
        server = SimpleNamespace(_project_sync=project_sync, kanban_client=kanban)

        result = await _resolve_ticket_repo_path(server, "5")

        assert result is None
        project_sync.ensure_repo.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_repo_failure_returns_none(self):
        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(return_value=None)
        project_sync.ensure_repo = AsyncMock(return_value=None)
        kanban = MagicMock()
        kanban.get_project_name = AsyncMock(return_value="New Project")
        server = SimpleNamespace(_project_sync=project_sync, kanban_client=kanban)

        result = await _resolve_ticket_repo_path(server, "5")

        assert result is None


def _make_task_with_project(project_id):
    task = MagicMock()
    task.source_context = {"kanboard_task": {"project_id": project_id}}
    return task


class TestVerifyTicketBelongsToProject:
    """_verify_ticket_belongs_to_project fails closed unless verified true.

    /dev-env/view is unauthenticated by default and project_id now drives
    real external side effects (Gitea repo/webhook auto-provisioning) —
    this guards against pairing an arbitrary real project_id with a
    fabricated ticket_id.
    """

    @pytest.mark.asyncio
    async def test_empty_project_id_returns_false(self):
        kanban = MagicMock()
        server = SimpleNamespace(kanban_client=kanban)
        assert await _verify_ticket_belongs_to_project(server, "42", "") is False

    @pytest.mark.asyncio
    async def test_no_kanban_client_returns_false(self):
        server = SimpleNamespace(kanban_client=None)
        assert await _verify_ticket_belongs_to_project(server, "42", "1") is False

    @pytest.mark.asyncio
    async def test_kanban_without_get_task_by_id_returns_false(self):
        kanban = MagicMock(spec=[])  # no get_task_by_id attribute
        server = SimpleNamespace(kanban_client=kanban)
        assert await _verify_ticket_belongs_to_project(server, "42", "1") is False

    @pytest.mark.asyncio
    async def test_get_task_by_id_failure_returns_false(self):
        kanban = MagicMock()
        kanban.get_task_by_id = AsyncMock(side_effect=RuntimeError("kanban down"))
        server = SimpleNamespace(kanban_client=kanban)
        assert await _verify_ticket_belongs_to_project(server, "42", "1") is False

    @pytest.mark.asyncio
    async def test_ticket_not_found_returns_false(self):
        kanban = MagicMock()
        kanban.get_task_by_id = AsyncMock(return_value=None)
        server = SimpleNamespace(kanban_client=kanban)
        assert await _verify_ticket_belongs_to_project(server, "42", "1") is False

    @pytest.mark.asyncio
    async def test_matching_project_id_returns_true(self):
        kanban = MagicMock()
        kanban.get_task_by_id = AsyncMock(return_value=_make_task_with_project(1))
        server = SimpleNamespace(kanban_client=kanban)
        assert await _verify_ticket_belongs_to_project(server, "42", "1") is True

    @pytest.mark.asyncio
    async def test_mismatched_project_id_returns_false(self):
        """The core abuse case: real ticket, but a different, fabricated
        project_id supplied by the caller."""
        kanban = MagicMock()
        kanban.get_task_by_id = AsyncMock(return_value=_make_task_with_project(1))
        server = SimpleNamespace(kanban_client=kanban)
        assert await _verify_ticket_belongs_to_project(server, "42", "99") is False

    @pytest.mark.asyncio
    async def test_missing_source_context_returns_false(self):
        task = MagicMock()
        task.source_context = None
        kanban = MagicMock()
        kanban.get_task_by_id = AsyncMock(return_value=task)
        server = SimpleNamespace(kanban_client=kanban)
        assert await _verify_ticket_belongs_to_project(server, "42", "1") is False

    @pytest.mark.asyncio
    async def test_project_id_as_int_matches_string_query_param(self):
        """Kanboard's own project_id is typically an int; the query param
        arrives as a string — comparison must not be type-sensitive."""
        kanban = MagicMock()
        kanban.get_task_by_id = AsyncMock(return_value=_make_task_with_project(7))
        server = SimpleNamespace(kanban_client=kanban)
        assert await _verify_ticket_belongs_to_project(server, "42", "7") is True


class _FakeRequest:
    """Minimal Starlette-Request-shaped fake: .headers.get() + async .stream()."""

    def __init__(self, body: bytes, content_length=None, chunk_size: int = 8):
        self._body = body
        self._chunk_size = chunk_size
        self.headers = {} if content_length is None else {"content-length": content_length}

    async def stream(self):
        for i in range(0, len(self._body), self._chunk_size):
            yield self._body[i : i + self._chunk_size]


class TestReadBoundedBody:
    """_read_bounded_body caps webhook payload size before signature/token
    checks run — those routes are exempt from bearer auth, so an
    unbounded read would let an unauthenticated caller exhaust memory."""

    @pytest.mark.asyncio
    async def test_returns_full_body_within_limit(self):
        req = _FakeRequest(b"small payload")
        result = await _read_bounded_body(req, max_bytes=1024)
        assert result == b"small payload"

    @pytest.mark.asyncio
    async def test_content_length_over_limit_rejected_without_streaming(self):
        req = _FakeRequest(b"x" * 10, content_length="999999999")

        async def _never_stream():
            raise AssertionError("must not stream once Content-Length exceeds the cap")
            yield b""  # pragma: no cover

        req.stream = _never_stream
        result = await _read_bounded_body(req, max_bytes=1024)
        assert result is None

    @pytest.mark.asyncio
    async def test_streamed_body_over_limit_rejected(self):
        """No (or an understated) Content-Length — the streaming cap still
        catches an oversized body, e.g. chunked transfer encoding."""
        req = _FakeRequest(b"x" * 2000, chunk_size=100)
        result = await _read_bounded_body(req, max_bytes=1024)
        assert result is None

    @pytest.mark.asyncio
    async def test_malformed_content_length_falls_back_to_streaming_cap(self):
        req = _FakeRequest(b"x" * 2000, content_length="not-a-number", chunk_size=100)
        result = await _read_bounded_body(req, max_bytes=1024)
        assert result is None

    @pytest.mark.asyncio
    async def test_body_exactly_at_limit_is_accepted(self):
        req = _FakeRequest(b"x" * 1024, chunk_size=128)
        result = await _read_bounded_body(req, max_bytes=1024)
        assert result == b"x" * 1024


class TestAcGeneratorWiredToLLM:
    """The workflow must get an ACGenerator backed by the configured LLM.

    Without an llm_generate callable, ACGenerator always falls back to a
    keyword heuristic that produces a near-identical generic checklist
    for every ticket — the "AC is the same for all tickets" bug. Wiring
    it to the engine's generate_text() makes AC ticket-specific.
    """

    @pytest.mark.asyncio
    async def test_ac_generator_uses_engine_generate_text(self, monkeypatch):
        monkeypatch.delenv("GITEA_URL", raising=False)
        server = _make_server()
        engine = MagicMock()
        engine.generate_text = AsyncMock(return_value="- [ ] specific")
        server.ai_engine = engine

        with (
            patch(
                "src.workflows.human_gated_workflow.HumanGatedWorkflow",
                return_value=AsyncMock(),
            ) as wf_cls,
            patch("src.marcus_mcp.tools.human_gated.register_workflow"),
        ):
            await _wire_human_gated_workflow(server)

        ac_gen = wf_cls.call_args.kwargs["ac_generator"]
        assert ac_gen is not None
        # The generator's llm_generate is the engine's generate_text.
        assert ac_gen._llm_generate is engine.generate_text

    @pytest.mark.asyncio
    async def test_no_engine_leaves_ac_generator_none(self, monkeypatch):
        """No ai_engine on the server → ac_generator=None (heuristic default
        inside the workflow), never a crash."""
        monkeypatch.delenv("GITEA_URL", raising=False)
        server = _make_server()  # no ai_engine attribute

        with (
            patch(
                "src.workflows.human_gated_workflow.HumanGatedWorkflow",
                return_value=AsyncMock(),
            ) as wf_cls,
            patch("src.marcus_mcp.tools.human_gated.register_workflow"),
        ):
            await _wire_human_gated_workflow(server)

        assert wf_cls.call_args.kwargs["ac_generator"] is None


class TestProjectWatcherAutoRepo:
    """Every Kanboard project should get a Gitea repo automatically.

    ProjectWatcher polls getAllProjects and emits project.created for any
    project it hasn't seen; ProjectSyncWorkflow._on_project_created turns
    that into ensure_repo (idempotent). The watcher was built but never
    started — so repos were only ever created on-demand when an agent
    first worked a ticket. Starting it means existing projects missing a
    repo get one on the first poll (empty known-set → emits for all).
    """

    @pytest.mark.asyncio
    async def test_watcher_started_when_gitea_and_kanboard_configured(
        self, monkeypatch
    ):
        monkeypatch.setenv("GITEA_URL", "http://gitea:3000")
        monkeypatch.setenv("GITEA_TOKEN", "tok123")
        monkeypatch.setenv("KANBOARD_URL", "http://kanboard/jsonrpc.php")
        monkeypatch.setenv("KANBOARD_API_TOKEN", "kbtok")
        server = _make_server()

        mock_gitea = AsyncMock()
        mock_gitea.connect = AsyncMock(return_value=True)
        mock_watcher = AsyncMock()

        with (
            patch("src.integrations.gitea_manager.GiteaManager", return_value=mock_gitea),
            patch("src.workflows.project_sync_workflow.ProjectSyncWorkflow"),
            patch("src.workflows.human_gated_workflow.HumanGatedWorkflow", return_value=AsyncMock()),
            patch("src.marcus_mcp.tools.human_gated.register_workflow"),
            patch(
                "src.core.project_watcher.ProjectWatcher", return_value=mock_watcher
            ) as watcher_cls,
        ):
            await _wire_human_gated_workflow(server)

        watcher_cls.assert_called_once()
        kwargs = watcher_cls.call_args.kwargs
        assert kwargs["kanboard_url"] == "http://kanboard/jsonrpc.php"
        assert kwargs["api_token"] == "kbtok"
        mock_watcher.start.assert_awaited_once()
        assert server._project_watcher is mock_watcher

    @pytest.mark.asyncio
    async def test_watcher_starts_without_gitea_for_columns(self, monkeypatch):
        """Watcher decoupled from Gitea: with Kanboard configured but no
        Gitea, it still starts (for column reconciliation) and passes
        is_provisioned=None (no repo mapping to gate on)."""
        monkeypatch.delenv("GITEA_URL", raising=False)
        monkeypatch.setenv("KANBOARD_URL", "http://kb/jsonrpc.php")
        monkeypatch.setenv("KANBOARD_API_TOKEN", "t")
        server = _make_server()
        mock_watcher = AsyncMock()

        with (
            patch("src.workflows.human_gated_workflow.HumanGatedWorkflow", return_value=AsyncMock()),
            patch("src.marcus_mcp.tools.human_gated.register_workflow"),
            patch(
                "src.core.project_watcher.ProjectWatcher", return_value=mock_watcher
            ) as watcher_cls,
        ):
            await _wire_human_gated_workflow(server)

        watcher_cls.assert_called_once()
        assert watcher_cls.call_args.kwargs["is_provisioned"] is None
        mock_watcher.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_watcher_without_kanboard_creds(self, monkeypatch):
        """No KANBOARD_URL/API_TOKEN → nothing to poll → no watcher."""
        monkeypatch.delenv("GITEA_URL", raising=False)
        monkeypatch.delenv("KANBOARD_URL", raising=False)
        monkeypatch.delenv("KANBOARD_API_TOKEN", raising=False)
        server = _make_server()

        with (
            patch("src.workflows.human_gated_workflow.HumanGatedWorkflow", return_value=AsyncMock()),
            patch("src.marcus_mcp.tools.human_gated.register_workflow"),
            patch("src.core.project_watcher.ProjectWatcher") as watcher_cls,
        ):
            await _wire_human_gated_workflow(server)

        watcher_cls.assert_not_called()
        assert not hasattr(server, "_project_watcher")


class TestColumnAutoReconcile:
    """Every new Kanboard project gets Marcus's columns, Gitea or not."""

    @pytest.mark.asyncio
    async def test_project_created_triggers_ensure_columns(self, monkeypatch):
        monkeypatch.setenv("KANBOARD_URL", "http://kb/jsonrpc.php")
        monkeypatch.setenv("KANBOARD_API_TOKEN", "t")
        monkeypatch.delenv("GITEA_URL", raising=False)  # no Gitea → still runs

        events = Events()
        kanban = MagicMock()
        kanban.ensure_columns = AsyncMock()
        server = SimpleNamespace(
            events=events, kanban_client=kanban, provider="kanboard"
        )

        with (
            patch(
                "src.workflows.human_gated_workflow.HumanGatedWorkflow",
                return_value=AsyncMock(),
            ),
            patch("src.marcus_mcp.tools.human_gated.register_workflow"),
            patch(
                "src.core.project_watcher.ProjectWatcher", return_value=AsyncMock()
            ),
        ):
            await _wire_human_gated_workflow(server)

        await events.publish(
            "project.created", source="test", data={"kanboard_project_id": 5}
        )
        kanban.ensure_columns.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_reconcile_is_once_per_project(self, monkeypatch):
        """Re-fired project.created (repo retry) doesn't re-reconcile columns."""
        monkeypatch.setenv("KANBOARD_URL", "http://kb/jsonrpc.php")
        monkeypatch.setenv("KANBOARD_API_TOKEN", "t")
        monkeypatch.delenv("GITEA_URL", raising=False)

        events = Events()
        kanban = MagicMock()
        kanban.ensure_columns = AsyncMock()
        server = SimpleNamespace(
            events=events, kanban_client=kanban, provider="kanboard"
        )
        with (
            patch(
                "src.workflows.human_gated_workflow.HumanGatedWorkflow",
                return_value=AsyncMock(),
            ),
            patch("src.marcus_mcp.tools.human_gated.register_workflow"),
            patch(
                "src.core.project_watcher.ProjectWatcher", return_value=AsyncMock()
            ),
        ):
            await _wire_human_gated_workflow(server)

        for _ in range(3):
            await events.publish(
                "project.created", source="test",
                data={"kanboard_project_id": 5},
            )
        kanban.ensure_columns.assert_awaited_once_with(5)
