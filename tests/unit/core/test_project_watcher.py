"""Unit tests for src/core/project_watcher.py"""

import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.events import Events
from src.core.project_watcher import ProjectWatcher


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_file(tmp_path):
    """Temporary state file path."""
    return str(tmp_path / "known_projects.json")


@pytest.fixture
def events():
    """Minimal Events instance with a mock publish method."""
    ev = Events()
    ev.publish = AsyncMock()
    return ev


@pytest.fixture
def watcher(state_file, events):
    """ProjectWatcher backed by a temp file, not started."""
    return ProjectWatcher(
        kanboard_url="http://localhost:8080/jsonrpc.php",
        api_token="test-token",
        events=events,
        poll_interval=60.0,
        state_path=state_file,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_projects(projects: list):
    """Return an async mock that provides the given project list."""

    async def _fake_fetch(self_arg):
        return projects

    return _fake_fetch


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


class TestStatePersistence:
    """Tests for known-ID load/save cycle."""

    def test_loads_empty_set_when_file_absent(self, state_file, events):
        """Creates fresh empty set if state file does not exist."""
        assert not os.path.exists(state_file)
        w = ProjectWatcher(
            kanboard_url="http://x",
            api_token="t",
            events=events,
            state_path=state_file,
        )
        assert w._known_ids == set()

    def test_loads_known_ids_from_file(self, state_file, events):
        """Reads previously saved IDs from disk on construction."""
        with open(state_file, "w") as f:
            json.dump({"known_ids": [1, 2, 3]}, f)
        w = ProjectWatcher(
            kanboard_url="http://x",
            api_token="t",
            events=events,
            state_path=state_file,
        )
        assert w._known_ids == {1, 2, 3}

    @pytest.mark.asyncio
    async def test_saves_known_ids_after_poll(self, watcher, state_file):
        """known IDs are written to disk after poll_once() finds a new project."""
        projects = [{"id": "7", "name": "Alpha", "description": ""}]

        with patch.object(
            ProjectWatcher, "_fetch_projects", AsyncMock(return_value=projects)
        ):
            await watcher.poll_once()

        with open(state_file) as f:
            data = json.load(f)
        assert 7 in data["known_ids"]

    def test_persistence_survives_restart(self, state_file, events):
        """IDs saved by one watcher are visible to a fresh instance."""
        # Write some IDs
        with open(state_file, "w") as f:
            json.dump({"known_ids": [10, 20]}, f)

        w2 = ProjectWatcher(
            kanboard_url="http://x",
            api_token="t",
            events=events,
            state_path=state_file,
        )
        assert {10, 20} <= w2._known_ids


# ---------------------------------------------------------------------------
# poll_once()
# ---------------------------------------------------------------------------


class TestPollOnce:
    """Tests for the single-poll method."""

    @pytest.mark.asyncio
    async def test_emits_project_created_for_new_project(self, watcher, events):
        """poll_once emits project.created when a new project appears."""
        projects = [{"id": "5", "name": "New Project", "description": "Desc"}]

        with patch.object(
            ProjectWatcher, "_fetch_projects", AsyncMock(return_value=projects)
        ):
            await watcher.poll_once()

        events.publish.assert_awaited_once()
        call_args = events.publish.call_args
        assert call_args[0][0] == "project.created"
        assert call_args[1]["data"]["kanboard_project_id"] == 5
        assert call_args[1]["data"]["project_name"] == "New Project"

    @pytest.mark.asyncio
    async def test_no_event_for_already_known_project(self, watcher, events):
        """poll_once does NOT emit project.created for previously seen projects."""
        watcher._known_ids = {5}
        projects = [{"id": "5", "name": "Old Project", "description": ""}]

        with patch.object(
            ProjectWatcher, "_fetch_projects", AsyncMock(return_value=projects)
        ):
            await watcher.poll_once()

        events.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_emits_only_new_projects_in_mixed_list(self, watcher, events):
        """poll_once emits events only for new projects when list is mixed."""
        watcher._known_ids = {1}
        projects = [
            {"id": "1", "name": "Known", "description": ""},
            {"id": "2", "name": "Brand New", "description": ""},
        ]

        with patch.object(
            ProjectWatcher, "_fetch_projects", AsyncMock(return_value=projects)
        ):
            await watcher.poll_once()

        assert events.publish.await_count == 1
        data = events.publish.call_args[1]["data"]
        assert data["kanboard_project_id"] == 2

    @pytest.mark.asyncio
    async def test_no_event_when_fetch_returns_none(self, watcher, events):
        """poll_once silently skips when _fetch_projects returns None."""
        with patch.object(
            ProjectWatcher, "_fetch_projects", AsyncMock(return_value=None)
        ):
            await watcher.poll_once()

        events.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_adds_new_ids_to_known_set(self, watcher):
        """poll_once adds newly discovered IDs to _known_ids."""
        projects = [{"id": "99", "name": "Fresh", "description": ""}]

        with patch.object(
            ProjectWatcher, "_fetch_projects", AsyncMock(return_value=projects)
        ):
            await watcher.poll_once()

        assert 99 in watcher._known_ids


# ---------------------------------------------------------------------------
# Source (event metadata)
# ---------------------------------------------------------------------------


class TestEventSource:
    """Tests for event source metadata."""

    @pytest.mark.asyncio
    async def test_event_source_is_project_watcher(self, watcher, events):
        """Published events have source='project_watcher'."""
        projects = [{"id": "3", "name": "Beta", "description": ""}]

        with patch.object(
            ProjectWatcher, "_fetch_projects", AsyncMock(return_value=projects)
        ):
            await watcher.poll_once()

        call_kwargs = events.publish.call_args[1]
        assert call_kwargs["source"] == "project_watcher"
