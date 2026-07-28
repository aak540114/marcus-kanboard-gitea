"""Unit tests for src/core/project_watcher.py"""

import json
import os
import pytest
from unittest.mock import AsyncMock, patch

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


class TestIsProvisionedRetry:
    """With an is_provisioned callback, un-provisioned projects re-emit.

    Regression: the watcher marked a project 'known' the first time it saw
    it, then never re-emitted — so if repo creation FAILED downstream (e.g.
    a bad Gitea token scope → 403), the project was skipped forever and the
    repo never got created even after the token was fixed. When given an
    is_provisioned(pid) predicate (wired to the real repo mapping), the
    watcher re-emits every poll until the repo actually exists, then stops.
    """

    @pytest.mark.asyncio
    async def test_reemits_until_provisioned(self, state_file, events):
        provisioned: set = set()
        w = ProjectWatcher(
            kanboard_url="http://x/jsonrpc.php",
            api_token="t",
            events=events,
            state_path=state_file,
            is_provisioned=lambda pid: pid in provisioned,
        )
        projects = [{"id": 5, "name": "App", "description": ""}]
        with patch.object(
            ProjectWatcher, "_fetch_projects", AsyncMock(return_value=projects)
        ):
            await w.poll_once()          # not provisioned → emit (attempt 1)
            await w.poll_once()          # still not provisioned → emit again
            assert events.publish.await_count == 2
            provisioned.add(5)           # repo now exists
            await w.poll_once()          # provisioned → no more emits
        assert events.publish.await_count == 2

    @pytest.mark.asyncio
    async def test_provisioned_project_never_emits(self, state_file, events):
        w = ProjectWatcher(
            kanboard_url="http://x/jsonrpc.php",
            api_token="t",
            events=events,
            state_path=state_file,
            is_provisioned=lambda pid: True,  # already has a repo
        )
        projects = [{"id": 7, "name": "Existing", "description": ""}]
        with patch.object(
            ProjectWatcher, "_fetch_projects", AsyncMock(return_value=projects)
        ):
            await w.poll_once()
        events.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_without_callback_uses_known_set(self, state_file, events):
        """No callback → legacy behaviour: emit once per project id."""
        w = ProjectWatcher(
            kanboard_url="http://x/jsonrpc.php",
            api_token="t",
            events=events,
            state_path=state_file,
        )
        projects = [{"id": 9, "name": "One", "description": ""}]
        with patch.object(
            ProjectWatcher, "_fetch_projects", AsyncMock(return_value=projects)
        ):
            await w.poll_once()
            await w.poll_once()
        assert events.publish.await_count == 1


# ---------------------------------------------------------------------------
# notify_project() — the instant push path
# ---------------------------------------------------------------------------


class TestNotifyProject:
    """``notify_project(pid)`` provisions a single project on demand.

    This is the push counterpart to the background poll: the Kanboard
    plugin calls Marcus the instant a project page loads, and Marcus
    fetches just that one project and emits ``project.created`` if it
    still needs provisioning — reusing the same idempotent
    ``_needs_emit`` / persistence machinery as ``poll_once``.
    """

    @pytest.mark.asyncio
    async def test_emits_for_new_unprovisioned_project(self, watcher, events):
        """A never-seen project id emits project.created and returns True."""
        project = {"id": 11, "name": "New", "description": "d"}
        with patch.object(
            ProjectWatcher, "_fetch_project", AsyncMock(return_value=project)
        ):
            emitted = await watcher.notify_project(11)
        assert emitted is True
        events.publish.assert_awaited_once()
        assert 11 in watcher._known_ids

    @pytest.mark.asyncio
    async def test_no_emit_when_already_known(self, watcher, events):
        """Without an is_provisioned callback, a known id does not re-emit."""
        project = {"id": 12, "name": "Known", "description": ""}
        with patch.object(
            ProjectWatcher, "_fetch_project", AsyncMock(return_value=project)
        ):
            first = await watcher.notify_project(12)
            second = await watcher.notify_project(12)
        assert first is True
        assert second is False
        assert events.publish.await_count == 1

    @pytest.mark.asyncio
    async def test_no_emit_when_already_provisioned(self, state_file, events):
        """An already-provisioned project emits nothing (idempotent poke)."""
        w = ProjectWatcher(
            kanboard_url="http://x/jsonrpc.php",
            api_token="t",
            events=events,
            state_path=state_file,
            is_provisioned=lambda pid: True,
        )
        project = {"id": 13, "name": "Has repo", "description": ""}
        with patch.object(
            ProjectWatcher, "_fetch_project", AsyncMock(return_value=project)
        ):
            emitted = await w.notify_project(13)
        assert emitted is False
        events.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reemits_while_unprovisioned_even_if_known(
        self, state_file, events
    ):
        """With is_provisioned False, a repeat poke retries (re-emits)."""
        w = ProjectWatcher(
            kanboard_url="http://x/jsonrpc.php",
            api_token="t",
            events=events,
            state_path=state_file,
            is_provisioned=lambda pid: False,
        )
        project = {"id": 14, "name": "Retry", "description": ""}
        with patch.object(
            ProjectWatcher, "_fetch_project", AsyncMock(return_value=project)
        ):
            await w.notify_project(14)
            await w.notify_project(14)
        assert events.publish.await_count == 2

    @pytest.mark.asyncio
    async def test_returns_false_when_project_not_found(self, watcher, events):
        """A missing/unknown project id emits nothing and returns False."""
        with patch.object(
            ProjectWatcher, "_fetch_project", AsyncMock(return_value=None)
        ):
            emitted = await watcher.notify_project(999)
        assert emitted is False
        events.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_false_for_nonpositive_id(self, watcher, events):
        """A non-positive id is rejected without any RPC or emit."""
        with patch.object(
            ProjectWatcher, "_fetch_project", AsyncMock()
        ) as fetch:
            emitted = await watcher.notify_project(0)
        assert emitted is False
        fetch.assert_not_awaited()
        events.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_persists_known_ids(self, watcher, state_file):
        """A successful poke writes the id to the state file."""
        project = {"id": 15, "name": "Persist", "description": ""}
        with patch.object(
            ProjectWatcher, "_fetch_project", AsyncMock(return_value=project)
        ):
            await watcher.notify_project(15)
        with open(state_file) as f:
            data = json.load(f)
        assert 15 in data["known_ids"]


class TestIsEnabledGate:
    """A project not enabled for Marcus (ProjectAccessSettingManager) must
    never get project.created emitted for it — no auto-created Gitea repo,
    no column reconciliation — regardless of is_provisioned/known-set state.
    """

    @pytest.mark.asyncio
    async def test_disabled_project_never_emits(self, state_file, events):
        w = ProjectWatcher(
            kanboard_url="http://x/jsonrpc.php",
            api_token="t",
            events=events,
            state_path=state_file,
            is_enabled=lambda pid: False,
        )
        projects = [{"id": 9, "name": "New", "description": ""}]
        with patch.object(
            ProjectWatcher, "_fetch_projects", AsyncMock(return_value=projects)
        ):
            await w.poll_once()
        events.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enabled_project_emits_normally(self, state_file, events):
        w = ProjectWatcher(
            kanboard_url="http://x/jsonrpc.php",
            api_token="t",
            events=events,
            state_path=state_file,
            is_enabled=lambda pid: True,
        )
        projects = [{"id": 9, "name": "New", "description": ""}]
        with patch.object(
            ProjectWatcher, "_fetch_projects", AsyncMock(return_value=projects)
        ):
            await w.poll_once()
        events.publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_is_enabled_predicate_does_not_gate(self, state_file, events):
        """Without an is_enabled predicate, behaviour is unchanged (legacy
        emit-once-per-id / is_provisioned logic applies untouched)."""
        w = ProjectWatcher(
            kanboard_url="http://x/jsonrpc.php",
            api_token="t",
            events=events,
            state_path=state_file,
        )
        projects = [{"id": 9, "name": "New", "description": ""}]
        with patch.object(
            ProjectWatcher, "_fetch_projects", AsyncMock(return_value=projects)
        ):
            await w.poll_once()
        events.publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disabled_project_notify_also_does_not_emit(
        self, state_file, events
    ):
        """The instant-push path (notify_project) respects the same gate."""
        w = ProjectWatcher(
            kanboard_url="http://x/jsonrpc.php",
            api_token="t",
            events=events,
            state_path=state_file,
            is_enabled=lambda pid: False,
        )
        project = {"id": 9, "name": "New", "description": ""}
        with patch.object(
            ProjectWatcher, "_fetch_project", AsyncMock(return_value=project)
        ):
            emitted = await w.notify_project(9)
        assert emitted is False
        events.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_re_polls_disabled_project_once_enabled(self, state_file, events):
        """A project that WAS disabled starts emitting the moment the
        predicate reports it enabled — the human flipping the toggle takes
        effect on the very next poll, no restart needed."""
        state = {"enabled": False}
        w = ProjectWatcher(
            kanboard_url="http://x/jsonrpc.php",
            api_token="t",
            events=events,
            state_path=state_file,
            is_enabled=lambda pid: state["enabled"],
        )
        projects = [{"id": 9, "name": "New", "description": ""}]
        with patch.object(
            ProjectWatcher, "_fetch_projects", AsyncMock(return_value=projects)
        ):
            await w.poll_once()
            events.publish.assert_not_awaited()
            state["enabled"] = True
            await w.poll_once()
        events.publish.assert_awaited_once()
