"""
Unit tests for the "clone this project" server wiring in
src/marcus_mcp/server.py: CloneJob/CloneJobStore, _get_clone_job_store,
_get_project_desc_mgr, and _build_clone_workflow.

The HTTP route closures themselves (clone_project_api,
clone_project_status_api) are nested inside main()'s http-transport
branch, the same as every sibling route (project_enabled_api,
gate_setting_api, etc.) — consistent with this codebase's existing
convention, those are not unit-tested directly (no Starlette TestClient
harness exists for any route in this file); this suite covers every
extracted, importable piece of logic instead.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.marcus_mcp.server import (
    CloneJobStore,
    _build_clone_workflow,
    _get_clone_job_store,
    _get_project_desc_mgr,
)


def _make_server(**kwargs):
    defaults = dict(
        kanban_client=MagicMock(),
        provider="kanboard",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestCloneJobStore:
    def test_create_returns_a_running_job(self):
        store = CloneJobStore()
        job = store.create(1, "Cloned App")
        assert job.status == "running"
        assert job.baseline_project_id == 1
        assert job.new_name == "Cloned App"
        assert job.new_project_id is None
        assert job.warnings == []
        assert job.error is None

    def test_create_assigns_a_unique_job_id(self):
        store = CloneJobStore()
        first = store.create(1, "A")
        second = store.create(1, "A")
        assert first.job_id != second.job_id

    def test_get_returns_the_created_job(self):
        store = CloneJobStore()
        job = store.create(1, "Cloned App")
        assert store.get(job.job_id) is job

    def test_get_returns_none_for_unknown_id(self):
        store = CloneJobStore()
        assert store.get("nonexistent") is None


class TestGetCloneJobStore:
    """Must return one shared instance per server — the POST route
    creates a job on one call, the GET route polls it on another; a
    per-request store would never see a job created earlier."""

    def test_constructs_once_and_caches_on_server(self):
        server = SimpleNamespace()
        first = _get_clone_job_store(server)
        second = _get_clone_job_store(server)
        assert first is second
        assert server._clone_job_store is first

    def test_reuses_a_preexisting_instance(self):
        server = SimpleNamespace()
        existing = CloneJobStore()
        server._clone_job_store = existing
        result = _get_clone_job_store(server)
        assert result is existing


class TestGetProjectDescMgr:
    def test_constructs_once_and_caches_on_server(self):
        server = SimpleNamespace()
        with patch("src.core.project_description.ProjectDescriptionManager") as mgr_cls:
            mgr_cls.return_value = MagicMock()
            first = _get_project_desc_mgr(server)
            second = _get_project_desc_mgr(server)

        mgr_cls.assert_called_once()
        assert first is second
        assert server._project_desc_mgr is first

    def test_reuses_a_preexisting_instance(self):
        server = SimpleNamespace()
        existing = MagicMock()
        server._project_desc_mgr = existing

        with patch("src.core.project_description.ProjectDescriptionManager") as mgr_cls:
            result = _get_project_desc_mgr(server)

        mgr_cls.assert_not_called()
        assert result is existing


class TestBuildCloneWorkflow:
    def test_returns_none_when_kanban_client_is_none(self):
        server = _make_server(kanban_client=None)
        server._human_gated_workflow = MagicMock(_lifecycle=MagicMock())
        server._project_sync = MagicMock()
        assert _build_clone_workflow(server) is None

    def test_returns_none_when_human_gated_workflow_missing(self):
        server = _make_server()
        server._project_sync = MagicMock()
        assert _build_clone_workflow(server) is None

    def test_returns_none_when_project_sync_missing(self):
        server = _make_server()
        server._human_gated_workflow = MagicMock(_lifecycle=MagicMock())
        assert _build_clone_workflow(server) is None

    def test_returns_none_when_lifecycle_missing(self):
        """A HumanGatedWorkflow test double with no _lifecycle attribute
        (e.g. a bare MagicMock without that attr configured) must not
        produce a half-wired ProjectCloneWorkflow."""
        server = _make_server()
        hg = SimpleNamespace()  # deliberately no _lifecycle
        server._human_gated_workflow = hg
        server._project_sync = MagicMock()
        assert _build_clone_workflow(server) is None

    def test_constructs_workflow_with_shared_singletons(self):
        server = _make_server()
        lifecycle = MagicMock()
        human_gated = MagicMock(_lifecycle=lifecycle)
        project_sync = MagicMock()
        server._human_gated_workflow = human_gated
        server._project_sync = project_sync
        gate_mgr = MagicMock()
        server._gate_mgr = gate_mgr
        access_mgr = MagicMock()
        server._project_access_mgr = access_mgr
        desc_mgr = MagicMock()
        server._project_desc_mgr = desc_mgr

        workflow = _build_clone_workflow(server)

        assert workflow is not None
        assert workflow._kanban is server.kanban_client
        assert workflow._lifecycle is lifecycle
        assert workflow._human_gated is human_gated
        assert workflow._project_sync is project_sync
        assert workflow._gate_settings is gate_mgr
        assert workflow._project_access is access_mgr
        assert workflow._project_description is desc_mgr
        assert workflow._provider == "kanboard"
