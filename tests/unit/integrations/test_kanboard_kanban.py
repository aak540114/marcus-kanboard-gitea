"""
Unit tests for the KanboardKanban provider.

All tests mock HTTP calls — no real Kanboard instance is required.
Tests follow the Arrange-Act-Assert pattern and use pytest-asyncio for
async test support.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.integrations.providers.kanboard_kanban import (
    KanboardKanban,
    _marcus_priority_to_kb,
    _parse_kanboard_ts,
    classify_task_links,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config():
    """Minimal valid KanboardKanban config."""
    return {
        "kanboard_url": "http://localhost:8080/jsonrpc.php",
        "kanboard_api_token": "test-token-abc123",
        "kanboard_project_id": 1,
    }


@pytest.fixture
def kanban(config):
    """KanboardKanban instance with no live connection."""
    return KanboardKanban(config)


def _make_raw_task(
    task_id=1,
    title="Test task",
    description="A task",
    column_id=1,
    column_name="Backlog",
    is_active=1,
    owner_id=0,
    priority=1,
    date_creation=1700000000,
    date_modification=1700000000,
    date_due=0,
    time_estimated=0,
    project_id=1,
    tags=None,
):
    """Build a minimal Kanboard task dict as returned by the API."""
    return {
        "id": task_id,
        "title": title,
        "description": description,
        "column_id": column_id,
        "column_name": column_name,
        "is_active": is_active,
        "owner_id": owner_id,
        "priority": priority,
        "date_creation": date_creation,
        "date_modification": date_modification,
        "date_due": date_due,
        "time_estimated": time_estimated,
        "project_id": project_id,
        "tags": tags or [],
    }


def _rpc_response(result):
    """Build a mock httpx Response for a JSON-RPC reply."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"jsonrpc": "2.0", "id": 1, "result": result})
    return resp


# ---------------------------------------------------------------------------
# Initialisation tests
# ---------------------------------------------------------------------------


class TestKanboardKanbanInit:
    """Test constructor behaviour."""

    def test_url_stored_with_jsonrpc_path(self, config):
        """URL should always end with /jsonrpc.php."""
        config["kanboard_url"] = "http://localhost:8080"
        kb = KanboardKanban(config)
        assert kb._jsonrpc_url == "http://localhost:8080/jsonrpc.php"

    def test_url_with_explicit_jsonrpc_path(self, config):
        """Explicit /jsonrpc.php suffix is not doubled."""
        kb = KanboardKanban(config)
        assert kb._jsonrpc_url.endswith("/jsonrpc.php")
        assert kb._jsonrpc_url.count("/jsonrpc.php") == 1

    def test_api_token_stored(self, config):
        """API token is stored verbatim."""
        kb = KanboardKanban(config)
        assert kb._api_token == "test-token-abc123"

    def test_project_id_default(self):
        """Default project ID is 1 when not provided."""
        kb = KanboardKanban(
            {
                "kanboard_url": "http://localhost/jsonrpc.php",
                "kanboard_api_token": "tok",
            }
        )
        assert kb._project_id == 1

    def test_project_id_override(self, config):
        """Custom project ID is stored as int."""
        config["kanboard_project_id"] = "42"
        kb = KanboardKanban(config)
        assert kb._project_id == 42

    def test_client_none_before_connect(self, kanban):
        """HTTP client is None until connect() is called."""
        assert kanban._client is None

    def test_provider_enum_is_kanboard(self, kanban):
        """Provider enum value is KANBOARD."""
        from src.integrations.kanban_interface import KanbanProvider

        assert kanban.provider == KanbanProvider.KANBOARD


# ---------------------------------------------------------------------------
# connect / disconnect tests
# ---------------------------------------------------------------------------


class TestConnectDisconnect:
    """Test lifecycle methods."""

    @pytest.mark.asyncio
    async def test_connect_returns_true_on_success(self, kanban):
        """connect() returns True when project lookup succeeds."""
        project_resp = _rpc_response({"id": 1, "name": "My Project"})
        columns_resp = _rpc_response(
            [
                {"id": 1, "title": "Backlog"},
                {"id": 2, "title": "In Progress"},
                {"id": 3, "title": "Done"},
            ]
        )
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(side_effect=[project_resp, columns_resp])
        kanban._client.aclose = AsyncMock()


        with patch("httpx.AsyncClient", return_value=kanban._client):
            result = await kanban.connect()

        assert result is True
        assert kanban._project_name == "My Project"

    @pytest.mark.asyncio
    async def test_connect_returns_false_when_project_not_found(self, kanban):
        """connect() returns False if the project ID doesn't exist."""

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_rpc_response(None))
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await kanban.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_connect_returns_false_on_http_error(self, kanban):
        """connect() returns False when the server returns 4xx/5xx."""
        import httpx

        mock_client = AsyncMock()
        err_response = MagicMock()
        err_response.status_code = 401
        err_response.text = "Unauthorized"
        mock_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "401", request=MagicMock(), response=err_response
            )
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await kanban.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_disconnect_closes_client(self, kanban):
        """disconnect() closes the HTTP client and sets it to None."""
        mock_client = AsyncMock()
        mock_client.aclose = AsyncMock()
        kanban._client = mock_client
        await kanban.disconnect()
        mock_client.aclose.assert_called_once()
        assert kanban._client is None

    @pytest.mark.asyncio
    async def test_disconnect_safe_when_not_connected(self, kanban):
        """disconnect() does not raise when called before connect()."""
        await kanban.disconnect()  # should not raise


# ---------------------------------------------------------------------------
# _to_task conversion tests
# ---------------------------------------------------------------------------


class TestToTask:
    """Test the internal _to_task conversion method."""

    def test_id_is_string(self, kanban):
        """Task ID is always a string."""
        kanban._column_status_map = {1: TaskStatus.TODO}
        task = kanban._to_task(_make_raw_task(task_id=42))
        assert task.id == "42"

    def test_name_from_title(self, kanban):
        """Task name comes from the 'title' field."""
        kanban._column_status_map = {1: TaskStatus.TODO}
        task = kanban._to_task(_make_raw_task(title="Fix the bug"))
        assert task.name == "Fix the bug"

    def test_description_preserved(self, kanban):
        """Description is passed through verbatim."""
        kanban._column_status_map = {1: TaskStatus.TODO}
        task = kanban._to_task(_make_raw_task(description="Details here"))
        assert task.description == "Details here"

    def test_status_from_column_name(self, kanban):
        """Status is derived from column_name when present."""
        kanban._column_status_map = {}
        task = kanban._to_task(_make_raw_task(column_name="In Progress"))
        assert task.status == TaskStatus.IN_PROGRESS

    def test_status_from_column_id_fallback(self, kanban):
        """Status falls back to column_id map when column_name is empty."""
        kanban._column_status_map = {2: TaskStatus.IN_PROGRESS}
        raw = _make_raw_task(column_id=2, column_name="")
        task = kanban._to_task(raw)
        assert task.status == TaskStatus.IN_PROGRESS

    def test_is_active_zero_forces_done(self, kanban):
        """is_active=0 with no column_name forces DONE status."""
        kanban._column_status_map = {}
        raw = _make_raw_task(is_active=0, column_name="")
        task = kanban._to_task(raw)
        assert task.status == TaskStatus.DONE

    def test_assigned_to_none_when_owner_zero(self, kanban):
        """owner_id 0 means unassigned."""
        kanban._column_status_map = {1: TaskStatus.TODO}
        task = kanban._to_task(_make_raw_task(owner_id=0))
        assert task.assigned_to is None

    def test_assigned_to_string_when_owner_set(self, kanban):
        """Non-zero owner_id is converted to string."""
        kanban._column_status_map = {1: TaskStatus.TODO}
        task = kanban._to_task(_make_raw_task(owner_id=7))
        assert task.assigned_to == "7"

    def test_priority_mapping(self, kanban):
        """Kanboard priority 2 maps to HIGH."""
        kanban._column_status_map = {1: TaskStatus.TODO}
        task = kanban._to_task(_make_raw_task(priority=2))
        assert task.priority == Priority.HIGH

    def test_estimated_hours_passthrough(self, kanban):
        """time_estimated is Kanboard's raw hours value, passed through as-is.

        Kanboard stores time_estimated in HOURS (its UI renders the raw
        value with an 'hours' suffix — app/Template/task/
        time_tracking_summary.php in Kanboard v1.2.52); an earlier version
        of this provider wrongly assumed seconds and divided by 3600.
        """
        kanban._column_status_map = {1: TaskStatus.TODO}
        task = kanban._to_task(_make_raw_task(time_estimated=2))  # 2 hours
        assert task.estimated_hours == 2.0

    def test_due_date_populated(self, kanban):
        """Non-zero date_due is parsed to a timezone-aware datetime."""
        kanban._column_status_map = {1: TaskStatus.TODO}
        ts = 1700000000
        task = kanban._to_task(_make_raw_task(date_due=ts))
        assert task.due_date is not None
        assert task.due_date.tzinfo is not None

    def test_due_date_none_for_zero(self, kanban):
        """date_due=0 results in due_date=None."""
        kanban._column_status_map = {1: TaskStatus.TODO}
        task = kanban._to_task(_make_raw_task(date_due=0))
        assert task.due_date is None

    def test_labels_from_tags(self, kanban):
        """Task tags are mapped to the labels list."""
        kanban._column_status_map = {1: TaskStatus.TODO}
        raw = _make_raw_task(tags=[{"name": "urgent"}, {"name": "backend"}])
        task = kanban._to_task(raw)
        assert "urgent" in task.labels
        assert "backend" in task.labels

    def test_project_id_as_string(self, kanban):
        """project_id is always a string on the Task."""
        kanban._column_status_map = {1: TaskStatus.TODO}
        task = kanban._to_task(_make_raw_task(project_id=5))
        assert task.project_id == "5"

    def test_source_context_carries_raw_project_id(self, kanban):
        """Regression: source_context must carry the raw (int) Kanboard
        project_id so HumanGatedWorkflow can resolve gate_mode/verify_count/
        tech-stack checks. Previously this was never set, so those lookups
        always silently missed and fell back to defaults regardless of what
        was actually configured."""
        kanban._column_status_map = {1: TaskStatus.TODO}
        task = kanban._to_task(_make_raw_task(project_id=5))
        assert task.source_context == {"kanboard_task": {"project_id": 5}}


# ---------------------------------------------------------------------------
# normalize_status / normalize_priority tests
# ---------------------------------------------------------------------------


class TestNormalizeStatus:
    """Test status normalisation across common column names."""

    @pytest.mark.parametrize(
        "column_name,expected",
        [
            ("Backlog", TaskStatus.TODO),
            ("Ready", TaskStatus.READY),
            ("To Do", TaskStatus.TODO),
            ("In Progress", TaskStatus.IN_PROGRESS),
            ("WIP", TaskStatus.IN_PROGRESS),
            ("Review", TaskStatus.IN_PROGRESS),
            ("Blocked", TaskStatus.BLOCKED),
            ("On Hold", TaskStatus.BLOCKED),
            ("Done", TaskStatus.DONE),
            ("Closed", TaskStatus.DONE),
            ("Completed", TaskStatus.DONE),
            ("UnknownColumn", TaskStatus.TODO),  # default
        ],
    )
    def test_status_mapping(self, kanban, column_name, expected):
        """Column names map to the correct TaskStatus."""
        assert kanban.normalize_status(column_name) == expected

    def test_non_string_defaults_to_todo(self, kanban):
        """Non-string input defaults to TODO."""
        assert kanban.normalize_status(None) == TaskStatus.TODO
        assert kanban.normalize_status(42) == TaskStatus.TODO


class TestNormalizePriority:
    """Test priority normalisation from Kanboard integers."""

    @pytest.mark.parametrize(
        "kb_priority,expected",
        [
            (0, Priority.LOW),
            (1, Priority.MEDIUM),
            (2, Priority.HIGH),
            (3, Priority.URGENT),
            (99, Priority.MEDIUM),  # unknown → MEDIUM
        ],
    )
    def test_priority_mapping(self, kanban, kb_priority, expected):
        """Kanboard priority integers map to the correct Marcus Priority."""
        assert kanban.normalize_priority(kb_priority) == expected

    def test_non_integer_defaults_to_medium(self, kanban):
        """Non-integer input defaults to MEDIUM."""
        assert kanban.normalize_priority(None) == Priority.MEDIUM
        assert kanban.normalize_priority("high") == Priority.MEDIUM


# ---------------------------------------------------------------------------
# get_all_tasks tests
# ---------------------------------------------------------------------------


class TestGetAllTasks:
    """Test get_all_tasks() against mocked RPC responses."""

    @pytest.mark.asyncio
    async def test_raises_if_not_connected(self, kanban):
        """get_all_tasks() raises RuntimeError when client is None."""
        with pytest.raises(RuntimeError, match="connect()"):
            await kanban.get_all_tasks()

    @pytest.mark.asyncio
    async def test_returns_list_of_tasks(self, kanban):
        """get_all_tasks() returns a list of Task objects."""
        kanban._client = AsyncMock()
        active_resp = _rpc_response([_make_raw_task(task_id=1)])
        closed_resp = _rpc_response([])
        kanban._client.post = AsyncMock(
            side_effect=[_rpc_response([{"id": 1}]), active_resp, closed_resp]
        )
        kanban._column_status_map = {1: TaskStatus.TODO}
        kanban._columns_loaded = {1}

        tasks = await kanban.get_all_tasks()
        assert isinstance(tasks, list)
        assert len(tasks) == 1
        assert isinstance(tasks[0], Task)

    @pytest.mark.asyncio
    async def test_combines_active_and_closed(self, kanban):
        """Active and closed tasks are combined into one list."""
        kanban._client = AsyncMock()
        active_resp = _rpc_response([_make_raw_task(task_id=1)])
        closed_resp = _rpc_response(
            [_make_raw_task(task_id=2, is_active=0, column_name="Done")]
        )
        kanban._client.post = AsyncMock(
            side_effect=[_rpc_response([{"id": 1}]), active_resp, closed_resp]
        )
        kanban._column_status_map = {1: TaskStatus.TODO}
        kanban._columns_loaded = {1}

        tasks = await kanban.get_all_tasks()
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_empty_board_returns_empty_list(self, kanban):
        """Empty project returns an empty list."""
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response([{"id": 1}]),
                _rpc_response([]),
                _rpc_response([]),
            ]
        )
        kanban._columns_loaded = {1}
        tasks = await kanban.get_all_tasks()
        assert tasks == []

    @pytest.mark.asyncio
    async def test_polls_every_project_in_scope(self, kanban):
        """Marcus watches EVERY board it has been enabled for.

        Without this, Marcus only ever polls the single project baked into
        KANBOARD_PROJECT_ID at setup time. Enabling any OTHER project from
        its board header then has no effect whatsoever — Marcus never looks
        at it, so its ready+assigned tickets are never seen and never handed
        to an agent, while the toggle sits reassuringly ON.
        """
        kanban._client = AsyncMock()
        kanban.set_project_scope(lambda: [7, 8])
        kanban._column_status_map = {1: TaskStatus.TODO}
        kanban._columns_loaded = {7, 8}
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response([_make_raw_task(task_id=21, project_id=7)]),
                _rpc_response([]),
                _rpc_response([_make_raw_task(task_id=31, project_id=8)]),
                _rpc_response([]),
            ]
        )

        tasks = await kanban.get_all_tasks()

        assert {t.id for t in tasks} == {"21", "31"}
        polled = [
            c.kwargs["json"]["params"]["project_id"]
            for c in kanban._client.post.call_args_list
            if c.kwargs["json"]["method"] == "getAllTasks"
        ]
        assert set(polled) == {7, 8}

    @pytest.mark.asyncio
    async def test_without_a_scope_reads_every_project(self, kanban):
        """Unscoped, Marcus reads EVERY board.

        It needs visibility into projects it is not allowed to act on — to
        report "this project has ready tickets but isn't enabled", and so a
        ticket deleted on any board is noticed. Permission to TOUCH a
        ticket is a separate, per-write check.
        """
        kanban._client = AsyncMock()
        kanban._columns_loaded = {7, 8}
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response([{"id": 7}, {"id": 8}]),
                _rpc_response([]), _rpc_response([]),
                _rpc_response([]), _rpc_response([]),
            ]
        )

        await kanban.get_all_tasks()

        polled = [
            c.kwargs["json"]["params"]["project_id"]
            for c in kanban._client.post.call_args_list
            if c.kwargs["json"]["method"] == "getAllTasks"
        ]
        assert set(polled) == {7, 8}

    @pytest.mark.asyncio
    async def test_warms_columns_for_projects_beyond_the_bootstrap_one(
        self, kanban
    ):
        """Every project's columns must be cached before its tasks are
        converted, not just the single project baked into
        ``kanboard_project_id``.

        Real Kanboard's ``getAllTasks``/``getTask`` responses never include
        ``column_name`` (only ``getDetails()`` joins that in), so
        ``_to_task()`` always falls back to ``_column_status_map`` —  which
        ``_refresh_columns()`` only ever populated for ``kanban._project_id``
        at connect time. A ticket sitting in a "Ready" column on any OTHER
        project silently defaulted to TODO, so Marcus's first-sight recovery
        gate (which requires ``board_status`` to be READY or IN_PROGRESS)
        never fired and the ticket was never handed to a polling agent.
        """
        kanban._client = AsyncMock()

        columns_by_project = {
            1: [{"id": 10, "title": "Backlog"}],
            2: [{"id": 99, "title": "Ready"}],
        }

        async def fake_rpc(method, **params):
            if method == "getProjectById":
                return {"id": 1, "name": "Project One"}
            if method == "getAllProjects":
                return [{"id": 1}, {"id": 2}]
            if method == "getColumns":
                return columns_by_project[params["project_id"]]
            if method == "getAllTasks":
                if params["project_id"] == 2 and params["status_id"] == 1:
                    return [
                        _make_raw_task(
                            task_id=5, column_id=99, column_name="", project_id=2
                        )
                    ]
                return []
            return None

        kanban._rpc = AsyncMock(side_effect=fake_rpc)

        assert await kanban.connect() is True

        tasks = await kanban.get_all_tasks()

        project_2_task = next(t for t in tasks if t.id == "5")
        assert project_2_task.status == TaskStatus.READY

    @pytest.mark.asyncio
    async def test_project_listing_failure_narrows_rather_than_blinds(
        self, kanban
    ):
        """A failed getAllProjects falls back to the configured project
        rather than leaving Marcus reading nothing at all."""
        kanban._client = AsyncMock()
        polled = []

        async def fake_rpc(method, **params):
            if method == "getAllProjects":
                raise RuntimeError("listing unavailable")
            if method == "getAllTasks":
                polled.append(params["project_id"])
                return []
            return None

        kanban._rpc = fake_rpc  # type: ignore[method-assign]

        tasks = await kanban.get_all_tasks()

        assert tasks == []
        assert set(polled) == {kanban._project_id}

    @pytest.mark.asyncio
    async def test_empty_scope_polls_nothing(self, kanban):
        """No project enabled → Marcus reads no board at all, rather than
        silently falling back to the configured one (the whole point of the
        default-off access gate)."""
        kanban._client = AsyncMock()
        kanban.set_project_scope(lambda: [])
        kanban._client.post = AsyncMock()

        tasks = await kanban.get_all_tasks()

        assert tasks == []
        assert kanban._client.post.await_count == 0

    @pytest.mark.asyncio
    async def test_scope_failure_falls_back_to_configured_project(self, kanban):
        """A broken scope provider must not blind Marcus completely."""
        kanban._client = AsyncMock()

        def boom():
            raise RuntimeError("settings unreadable")

        kanban.set_project_scope(boom)
        kanban._columns_loaded = {kanban._project_id}
        kanban._client.post = AsyncMock(
            side_effect=[_rpc_response([]), _rpc_response([])]
        )

        await kanban.get_all_tasks()

        polled = [
            c.kwargs["json"]["params"]["project_id"]
            for c in kanban._client.post.call_args_list
            if c.kwargs["json"]["method"] == "getAllTasks"
        ]
        assert set(polled) == {kanban._project_id}


# ---------------------------------------------------------------------------
# get_task_by_id tests
# ---------------------------------------------------------------------------


class TestGetTaskById:
    """Test get_task_by_id() against mocked RPC responses."""

    @pytest.mark.asyncio
    async def test_raises_if_not_connected(self, kanban):
        """get_task_by_id() raises RuntimeError when client is None."""
        with pytest.raises(RuntimeError, match="connect()"):
            await kanban.get_task_by_id("5")

    @pytest.mark.asyncio
    async def test_returns_none_when_task_missing(self, kanban):
        """A nonexistent task id returns None rather than raising."""
        kanban._client = AsyncMock()
        kanban._rpc = AsyncMock(return_value=None)

        assert await kanban.get_task_by_id("999") is None

    @pytest.mark.asyncio
    async def test_warms_columns_for_a_task_from_another_project(self, kanban):
        """A single-task lookup must warm that task's project's columns
        too, not just self._project_id's — the same gap that made
        get_all_tasks() silently default every non-bootstrap-project task
        to TODO (see test_warms_columns_for_projects_beyond_the_bootstrap_one)
        applies here whenever a caller looks up one ticket by id directly.
        """
        kanban._client = AsyncMock()

        async def fake_rpc(method, **params):
            if method == "getTask":
                return _make_raw_task(
                    task_id=5, column_id=99, column_name="", project_id=2
                )
            if method == "getColumns":
                assert params["project_id"] == 2
                return [{"id": 99, "title": "Ready"}]
            return None

        kanban._rpc = AsyncMock(side_effect=fake_rpc)

        task = await kanban.get_task_by_id("5")

        assert task is not None
        assert task.status == TaskStatus.READY

    @pytest.mark.asyncio
    async def test_does_not_refetch_already_loaded_columns(self, kanban):
        """A project whose columns are already cached isn't refetched."""
        kanban._client = AsyncMock()
        kanban._columns_loaded = {2}
        kanban._column_status_map = {99: TaskStatus.WAITING_FOR_HUMAN}

        async def fake_rpc(method, **params):
            if method == "getTask":
                return _make_raw_task(
                    task_id=5, column_id=99, column_name="", project_id=2
                )
            if method == "getColumns":
                raise AssertionError("columns already loaded; should not refetch")
            return None

        kanban._rpc = AsyncMock(side_effect=fake_rpc)

        task = await kanban.get_task_by_id("5")

        assert task is not None
        assert task.status == TaskStatus.WAITING_FOR_HUMAN


# ---------------------------------------------------------------------------
# get_available_tasks tests
# ---------------------------------------------------------------------------


class TestGetAvailableTasks:
    """Test get_available_tasks() filtering."""

    @pytest.mark.asyncio
    async def test_returns_only_todo_unassigned(self, kanban):
        """Only TODO + unassigned tasks are returned as available."""
        now = datetime.now(timezone.utc)
        all_tasks = [
            Task(
                id="1",
                name="Open",
                status=TaskStatus.TODO,
                assigned_to=None,
                priority=Priority.MEDIUM,
                description="",
                created_at=now,
                updated_at=now,
                due_date=None,
                estimated_hours=0.0,
            ),
            Task(
                id="2",
                name="Taken",
                status=TaskStatus.TODO,
                assigned_to="7",
                priority=Priority.MEDIUM,
                description="",
                created_at=now,
                updated_at=now,
                due_date=None,
                estimated_hours=0.0,
            ),
            Task(
                id="3",
                name="WIP",
                status=TaskStatus.IN_PROGRESS,
                assigned_to=None,
                priority=Priority.MEDIUM,
                description="",
                created_at=now,
                updated_at=now,
                due_date=None,
                estimated_hours=0.0,
            ),
        ]
        kanban.get_all_tasks = AsyncMock(return_value=all_tasks)
        available = await kanban.get_available_tasks()
        assert len(available) == 1
        assert available[0].id == "1"


# ---------------------------------------------------------------------------
# add_comment tests
# ---------------------------------------------------------------------------


class TestAddComment:
    """Test add_comment()."""

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self, kanban):
        """add_comment() returns True when createComment succeeds."""
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(return_value=_rpc_response(42))
        result = await kanban.add_comment("1", "Progress update")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_api_error(self, kanban):
        """add_comment() returns False when the RPC call raises."""
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(side_effect=RuntimeError("API error"))
        result = await kanban.add_comment("1", "comment")
        assert result is False

    @pytest.mark.asyncio
    async def test_raises_if_not_connected(self, kanban):
        """add_comment() raises RuntimeError when client is None."""
        with pytest.raises(RuntimeError, match="connect()"):
            await kanban.add_comment("1", "hello")

    @staticmethod
    def _params_of(kanban, method):
        """Return the params dict of the first RPC call to *method*."""
        for c in kanban._client.post.call_args_list:
            if c.kwargs["json"]["method"] == method:
                return c.kwargs["json"]["params"]
        return None

    @pytest.mark.asyncio
    async def test_posts_as_existing_marcus_bot_user(self, kanban):
        """When a 'marcus' user exists, the comment is posted as its id."""
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response({"id": 7, "username": "marcus"}),  # getUserByName
                _rpc_response(101),  # createComment
            ]
        )
        assert await kanban.add_comment("1", "hello") is True
        assert kanban._comment_user_id == 7
        assert self._params_of(kanban, "createComment")["user_id"] == 7

    @pytest.mark.asyncio
    async def test_creates_marcus_bot_user_when_absent(self, kanban):
        """When no 'marcus' user exists, it is created and used."""
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response(None),  # getUserByName → not found
                _rpc_response(9),  # createUser → new id
                _rpc_response(102),  # createComment
            ]
        )
        assert await kanban.add_comment("1", "hello") is True
        assert kanban._comment_user_id == 9
        methods = [
            c.kwargs["json"]["method"] for c in kanban._client.post.call_args_list
        ]
        assert "createUser" in methods
        assert self._params_of(kanban, "createComment")["user_id"] == 9

    @pytest.mark.asyncio
    async def test_comment_user_id_cached_across_comments(self, kanban):
        """The bot user is resolved once, then reused (no repeat lookup)."""
        kanban._client = AsyncMock()
        kanban._comment_user_id = 7  # already resolved
        kanban._client.post = AsyncMock(return_value=_rpc_response(200))
        assert await kanban.add_comment("1", "a") is True
        methods = [
            c.kwargs["json"]["method"] for c in kanban._client.post.call_args_list
        ]
        assert "getUserByName" not in methods  # used the cache
        assert self._params_of(kanban, "createComment")["user_id"] == 7

    @pytest.mark.asyncio
    async def test_comment_falls_back_to_anonymous_on_lookup_error(self, kanban):
        """If resolving the bot user fails, the comment still posts (as 0)."""
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(
            side_effect=[
                RuntimeError("user lookup boom"),  # getUserByName
                _rpc_response(103),  # createComment
            ]
        )
        assert await kanban.add_comment("1", "hello") is True
        assert kanban._comment_user_id is None  # not cached → retried next time
        assert self._params_of(kanban, "createComment")["user_id"] == 0


# ---------------------------------------------------------------------------
# get_comments tests
# ---------------------------------------------------------------------------


class TestGetComments:
    """Test get_comments()."""

    @pytest.mark.asyncio
    async def test_normalizes_comment_fields(self, kanban):
        """get_comments() maps Kanboard's raw fields to content/author/date."""
        kanban._client = AsyncMock()
        raw = [
            {"comment": "First reply", "username": "alice", "date_creation": 1700000001},
            {"comment": "Second reply", "username": "", "date_creation": 1700000002},
        ]
        kanban._client.post = AsyncMock(return_value=_rpc_response(raw))
        result = await kanban.get_comments("1")
        assert result == [
            {"content": "First reply", "author": "alice", "date": 1700000001},
            {"content": "Second reply", "author": None, "date": 1700000002},
        ]

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_comments(self, kanban):
        """get_comments() returns [] when Kanboard's result is empty/None."""
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(return_value=_rpc_response(None))
        result = await kanban.get_comments("1")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_rpc_failure(self, kanban):
        """get_comments() fails soft (empty list) rather than raising —
        comment history is supplementary context, not a hard requirement."""
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(side_effect=RuntimeError("API error"))
        result = await kanban.get_comments("1")
        assert result == []

    @pytest.mark.asyncio
    async def test_raises_if_not_connected(self, kanban):
        """get_comments() raises RuntimeError when client is None."""
        with pytest.raises(RuntimeError, match="connect()"):
            await kanban.get_comments("1")


# ---------------------------------------------------------------------------
# get_task_links tests
# ---------------------------------------------------------------------------


class TestGetTaskLinks:
    """Test get_task_links()."""

    @pytest.mark.asyncio
    async def test_classifies_links_by_direction(self, kanban):
        """get_task_links() splits raw links into depends_on/blocks/relates_to.

        Raw link fixtures use the REAL Kanboard v1.2.52 payload shape:
        TaskLinkModel::getAll() aliases the opposite task's id to a key
        named ``task_id`` (``opposite_task_id AS task_id``) — there is no
        ``opposite_task_id`` key in the response. An earlier version of
        this suite used ``opposite_task_id`` fixtures, matching the (buggy)
        implementation but not real payloads.
        """
        kanban._client = AsyncMock()
        raw = [
            {
                "label": "is blocked by",
                "task_id": 5,
                "title": "Schema migration",
                "column_title": "Done",
            },
            {
                "label": "blocks",
                "task_id": 9,
                "title": "Deploy",
                "column_title": "Todo",
            },
            {
                "label": "related",
                "task_id": 3,
                "title": "Docs",
                "column_title": "Backlog",
            },
        ]
        kanban._client.post = AsyncMock(return_value=_rpc_response(raw))
        result = await kanban.get_task_links("1")
        assert result == {
            "depends_on": [
                {"task_id": "5", "title": "Schema migration", "column": "Done"}
            ],
            "blocks": [{"task_id": "9", "title": "Deploy", "column": "Todo"}],
            "relates_to": [{"task_id": "3", "title": "Docs", "column": "Backlog"}],
        }

    @pytest.mark.asyncio
    async def test_calls_getAllTaskLinks_rpc_method(self, kanban):
        """get_task_links() must call getAllTaskLinks — getTaskLinks does not exist.

        Kanboard v1.2.52's TaskLinkProcedure defines getAllTaskLinks(task_id);
        there is no getTaskLinks method, and Kanboard registers no aliases —
        calling it returns a JSON-RPC "Method not found" error, which the
        soft-fail path silently turned into permanently empty link data.
        """
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(return_value=_rpc_response([]))
        await kanban.get_task_links("7")
        body = kanban._client.post.call_args.kwargs.get("json") or (
            kanban._client.post.call_args.args[1]
            if len(kanban._client.post.call_args.args) > 1
            else None
        )
        assert body["method"] == "getAllTaskLinks"
        assert body["params"] == {"task_id": 7}

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_links(self, kanban):
        """get_task_links() returns all-empty groups for a ticket with no links."""
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(return_value=_rpc_response(None))
        result = await kanban.get_task_links("1")
        assert result == {"depends_on": [], "blocks": [], "relates_to": []}

    @pytest.mark.asyncio
    async def test_returns_empty_on_rpc_failure(self, kanban):
        """get_task_links() fails soft rather than raising."""
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(side_effect=RuntimeError("API error"))
        result = await kanban.get_task_links("1")
        assert result == {"depends_on": [], "blocks": [], "relates_to": []}

    @pytest.mark.asyncio
    async def test_raises_if_not_connected(self, kanban):
        """get_task_links() raises RuntimeError when client is None."""
        with pytest.raises(RuntimeError, match="connect()"):
            await kanban.get_task_links("1")


# ---------------------------------------------------------------------------
# move_task_to_column tests
# ---------------------------------------------------------------------------


class TestMoveTaskToColumn:
    """Test move_task_to_column()."""

    @staticmethod
    def _rpc_methods(kanban):
        """Return the JSON-RPC method names issued through the mocked client."""
        return [
            c.kwargs["json"]["method"]
            for c in kanban._client.post.call_args_list
        ]

    @pytest.mark.asyncio
    async def test_moves_to_known_column(self, kanban):
        """move_task_to_column() calls moveTaskPosition with the correct column ID."""
        kanban._client = AsyncMock()
        kanban._column_map = {"in progress": 2, "done": 3}
        kanban._column_status_map = {2: TaskStatus.IN_PROGRESS, 3: TaskStatus.DONE}
        # getTask locates the task; moveTaskPosition → True; getTask verifies.
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response(_make_raw_task(task_id=5, column_id=1, project_id=1)),
                _rpc_response(True),
                _rpc_response(
                    _make_raw_task(
                        task_id=5, column_id=2, project_id=1, is_active=1
                    )
                ),
            ]
        )
        result = await kanban.move_task_to_column("5", "In Progress")
        assert result is True
        # Task already open + non-done target → NO openTask (a spurious
        # openTask fires a task.open webhook on every move — the feedback
        # loop that locked Kanboard's SQLite).
        assert "openTask" not in self._rpc_methods(kanban)

    @pytest.mark.asyncio
    async def test_closed_task_reopened_when_moved_to_workable_column(
        self, kanban
    ):
        """A board-closed task moved to a non-done column IS reopened."""
        kanban._client = AsyncMock()
        kanban._column_map = {"in progress": 2}
        kanban._column_status_map = {2: TaskStatus.IN_PROGRESS}
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response(
                    _make_raw_task(
                        task_id=5, column_id=1, project_id=1, is_active=0
                    )
                ),
                _rpc_response(True),
                _rpc_response(
                    _make_raw_task(
                        task_id=5, column_id=2, project_id=1, is_active=0
                    )
                ),
                _rpc_response(True),  # openTask
            ]
        )
        result = await kanban.move_task_to_column("5", "In Progress")
        assert result is True
        assert "openTask" in self._rpc_methods(kanban)

    @pytest.mark.asyncio
    async def test_move_to_done_does_not_close_the_task(self, kanban):
        """Moving to Done must NOT close the task — a closed task is
        invisible on the Kanboard board.

        Kanboard's board renders the search query from
        UserSession::getFilters(), which defaults to ``status:open``
        (app/Core/User/UserSession.php). closeTask sets ``is_active = 0``
        (TaskStatusModel::changeStatus), so a closed card is filtered out
        of EVERY column — it doesn't appear in Done, it disappears from the
        board altogether. That reads to a human as "Marcus never moved
        it", while Marcus's comment on the ticket is still plainly there.

        The Done column is already the signal that the work is finished;
        Kanboard's own UI does not auto-close a card dragged to the last
        column either.
        """
        kanban._client = AsyncMock()
        kanban._column_map = {"done": 3}
        kanban._column_status_map = {3: TaskStatus.DONE}
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response(_make_raw_task(task_id=5, column_id=1, project_id=1)),
                _rpc_response(True),
                _rpc_response(
                    _make_raw_task(
                        task_id=5, column_id=3, project_id=1, is_active=1
                    )
                ),
            ]
        )
        assert await kanban.move_task_to_column("5", "Done") is True
        assert "closeTask" not in self._rpc_methods(kanban)

    @pytest.mark.asyncio
    async def test_move_to_done_reopens_a_human_closed_task(self, kanban):
        """A task a human closed in the UI is reopened when Marcus moves it,
        so the card becomes visible again on the board.

        This is the corrective direction of the same flag and must stay:
        without it a card a human closed stays invisible forever, however
        many times Marcus moves it.
        """
        kanban._client = AsyncMock()
        kanban._column_map = {"done": 3}
        kanban._column_status_map = {3: TaskStatus.DONE}
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response(
                    _make_raw_task(
                        task_id=5, column_id=1, project_id=1, is_active=0
                    )
                ),
                _rpc_response(True),
                _rpc_response(
                    _make_raw_task(
                        task_id=5, column_id=3, project_id=1, is_active=0
                    )
                ),
                _rpc_response(True),  # openTask
            ]
        )
        assert await kanban.move_task_to_column("5", "Done") is True
        assert "openTask" in self._rpc_methods(kanban)

    @pytest.mark.asyncio
    async def test_stale_column_map_refreshed_then_moves(self, kanban):
        """A column missing from the CACHED map (stale after a reconciliation)
        is found after a refresh — no full reconcile needed."""
        kanban._client = AsyncMock()
        kanban._column_map = {}  # stale / empty
        kanban._column_status_map = {}

        async def _refresh(project_id=None):
            kanban._column_map = {"waiting for human": 5}
            kanban._column_status_map = {5: TaskStatus.WAITING_FOR_HUMAN}

        with patch.object(
            kanban, "_refresh_columns", side_effect=_refresh
        ), patch.object(kanban, "ensure_columns", new=AsyncMock()) as ensure:
            kanban._client.post = AsyncMock(
                side_effect=[
                    _rpc_response(
                        _make_raw_task(task_id=5, column_id=1, project_id=1)
                    ),
                    _rpc_response(True),  # moveTaskPosition
                    _rpc_response(
                        _make_raw_task(
                            task_id=5, column_id=5, project_id=1, is_active=1
                        )
                    ),
                ]
            )
            result = await kanban.move_task_to_column("5", "Waiting for Human")
        assert result is True
        ensure.assert_not_awaited()  # a refresh alone fixed it

    @pytest.mark.asyncio
    async def test_missing_column_reconciled_then_moves(self, kanban):
        """A project still on Kanboard defaults (no 'waiting for human') gets
        reconciled to Marcus's columns, then the move succeeds."""
        kanban._client = AsyncMock()
        kanban._column_map = {}
        kanban._column_status_map = {}
        state = {"reconciled": False}

        async def _refresh(project_id=None):
            if state["reconciled"]:
                kanban._column_map = {"waiting for human": 5}
                kanban._column_status_map = {5: TaskStatus.WAITING_FOR_HUMAN}

        async def _ensure(_pid):
            state["reconciled"] = True
            return True

        with patch.object(
            kanban, "_refresh_columns", side_effect=_refresh
        ), patch.object(
            kanban, "ensure_columns", side_effect=_ensure
        ) as ensure:
            kanban._client.post = AsyncMock(
                side_effect=[
                    _rpc_response(
                        _make_raw_task(task_id=5, column_id=1, project_id=1)
                    ),
                    _rpc_response(True),
                    _rpc_response(
                        _make_raw_task(
                            task_id=5, column_id=5, project_id=1, is_active=1
                        )
                    ),
                ]
            )
            result = await kanban.move_task_to_column("5", "Waiting for Human")
        assert result is True
        ensure.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_column_never_resolves(self, kanban):
        """A truly unknown column returns False (loudly) after reconcile.

        The lookup that follows an unresolvable column finds the task is
        already in the configured project, so there is nowhere to retry.
        """
        kanban._client = AsyncMock()
        kanban._column_map = {}
        kanban._column_status_map = {}
        kanban._client.post = AsyncMock(
            return_value=_rpc_response(
                _make_raw_task(task_id=5, column_id=1, project_id=1)
            )
        )
        with patch.object(
            kanban, "_refresh_columns", new=AsyncMock()
        ), patch.object(kanban, "ensure_columns", new=AsyncMock()):
            result = await kanban.move_task_to_column("5", "Nonexistent Column")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_for_unknown_column(self, kanban):
        """move_task_to_column() returns False for a column that stays unknown
        even after a refresh + reconcile attempt."""
        kanban._client = AsyncMock()
        kanban._column_map = {"backlog": 1}
        kanban._column_status_map = {1: TaskStatus.TODO}
        kanban._client.post = AsyncMock(
            return_value=_rpc_response(
                _make_raw_task(task_id=5, column_id=1, project_id=1)
            )
        )
        with patch.object(
            kanban, "_refresh_columns", new=AsyncMock()
        ), patch.object(kanban, "ensure_columns", new=AsyncMock()):
            result = await kanban.move_task_to_column("5", "NonExistentColumn")
        assert result is False

    @pytest.mark.asyncio
    async def test_noop_move_treated_as_success(self, kanban):
        """A task already sitting in the target column counts as success,
        and no pointless write is issued.

        Kanboard's TaskPositionModel::movePosition() returns false both on
        real failures AND when the task is already in the requested
        column/position (a no-op), so its return value could never tell
        those apart. Knowing where the task is before moving settles it
        outright: the move is simply skipped.
        """
        kanban._client = AsyncMock()
        kanban._column_map = {"in progress": 2, "done": 3}
        kanban._column_status_map = {2: TaskStatus.IN_PROGRESS, 3: TaskStatus.DONE}
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response(
                    _make_raw_task(
                        task_id=5, column_id=2, project_id=1, is_active=1
                    )
                ),
            ]
        )
        result = await kanban.move_task_to_column("5", "In Progress")
        assert result is True
        assert "moveTaskPosition" not in self._rpc_methods(kanban)
        assert "openTask" not in self._rpc_methods(kanban)

    @pytest.mark.asyncio
    async def test_real_move_failure_still_returns_false(self, kanban):
        """moveTaskPosition=false with the task NOT in the target column is
        a genuine failure and must stay False."""
        kanban._client = AsyncMock()
        kanban._column_map = {"in progress": 2}
        kanban._column_status_map = {2: TaskStatus.IN_PROGRESS}
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response(_make_raw_task(task_id=5, column_id=1, project_id=1)),
                _rpc_response(False),
                _rpc_response(
                    _make_raw_task(task_id=5, column_id=1, project_id=1)
                ),
            ]
        )
        result = await kanban.move_task_to_column("5", "In Progress")
        assert result is False

    @pytest.mark.asyncio
    async def test_raises_if_not_connected(self, kanban):
        """move_task_to_column() raises RuntimeError when client is None."""
        with pytest.raises(RuntimeError, match="connect()"):
            await kanban.move_task_to_column("1", "Done")

    @pytest.mark.asyncio
    async def test_truthy_move_task_position_is_not_trusted(self, kanban):
        """moveTaskPosition's return value does NOT prove the move happened.

        Kanboard's underlying UPDATE ... WHERE project_id=? AND id=? still
        returns true when project_id doesn't match the task and ZERO rows
        were affected (PicoDb's Table::update() returns
        execute() !== false, not the affected row count). If Marcus trusts
        a truthy moveTaskPosition result without verifying, a project_id
        mismatch silently no-ops while LOOKING like success — the precise
        bug behind "comments post but cards never move columns". This must
        be caught by verifying against getTask regardless of what
        moveTaskPosition returned.
        """
        kanban._client = AsyncMock()
        kanban._column_map = {"in progress": 2}
        kanban._column_status_map = {2: TaskStatus.IN_PROGRESS}
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response(
                    _make_raw_task(task_id=5, column_id=99, project_id=1)
                ),
                _rpc_response(True),  # moveTaskPosition: misleadingly "true"
                _rpc_response(  # getTask: task never actually moved
                    _make_raw_task(task_id=5, column_id=99, project_id=1)
                ),
            ]
        )
        result = await kanban.move_task_to_column("5", "In Progress")
        assert result is False

    @pytest.mark.asyncio
    async def test_moves_within_the_tasks_actual_project(self, kanban):
        """A task belonging to a DIFFERENT Kanboard project than this
        provider's configured self._project_id is moved correctly, and the
        write is aimed at that project from the outset — resolving THAT
        project's columns, never the configured default's.
        """
        kanban._client = AsyncMock()
        kanban._column_map = {"in progress": 2}  # project 1's (default) columns
        kanban._column_status_map = {2: TaskStatus.IN_PROGRESS}
        assert kanban._project_id == 1

        async def _refresh(project_id=None):
            if project_id == 42:
                kanban._project_columns[42] = {"in progress": 7}
                kanban._column_status_map[7] = TaskStatus.IN_PROGRESS

        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response(  # getTask: the task is in project 42, column 55
                    _make_raw_task(
                        task_id=5, column_id=55, project_id=42, is_active=1
                    )
                ),
                _rpc_response(True),  # moveTaskPosition against project 42
                _rpc_response(  # getTask verifies
                    _make_raw_task(
                        task_id=5, column_id=7, project_id=42, is_active=1
                    )
                ),
            ]
        )
        with patch.object(kanban, "_refresh_columns", side_effect=_refresh):
            result = await kanban.move_task_to_column("5", "In Progress")
        assert result is True
        move_calls = [
            c.kwargs["json"]["params"]
            for c in kanban._client.post.call_args_list
            if c.kwargs["json"]["method"] == "moveTaskPosition"
        ]
        # Exactly one write, against the task's own project — project 1's
        # column id (2) is never used.
        assert len(move_calls) == 1
        assert move_calls[0]["project_id"] == 42
        assert move_calls[0]["column_id"] == 7

    @pytest.mark.asyncio
    async def test_reconciles_the_tasks_actual_project_not_the_default(
        self, kanban
    ):
        """When the OTHER project's board doesn't have the target column at
        all yet, ensure_columns is called for the task's REAL project — not
        self._project_id (the bug in a prior fix: reconciling the wrong
        project's board never helps a task that lives elsewhere).

        The default project (1) already has a same-named "Waiting for
        Human" column — realistic, since ensure_columns gives every
        project the same standard names — so resolving against the wrong
        board would silently succeed and hide the mistake. Only the
        task's own project (42) is missing the column and needs
        reconciling.
        """
        kanban._client = AsyncMock()
        kanban._column_map = {"in progress": 2, "waiting for human": 6}
        kanban._column_status_map = {2: TaskStatus.IN_PROGRESS, 6: TaskStatus.WAITING_FOR_HUMAN}

        async def _refresh(project_id=None):
            return None  # never finds it on project 42 via a plain refresh

        async def _ensure(pid):
            if pid == 42:
                kanban._project_columns[42] = {"waiting for human": 8}
                kanban._column_status_map[8] = TaskStatus.WAITING_FOR_HUMAN

        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response(  # getTask: the task lives in project 42
                    _make_raw_task(task_id=5, column_id=1, project_id=42)
                ),
                _rpc_response(True),  # moveTaskPosition against project 42
                _rpc_response(  # getTask verifies
                    _make_raw_task(
                        task_id=5, column_id=8, project_id=42, is_active=1
                    )
                ),
            ]
        )
        with patch.object(
            kanban, "_refresh_columns", side_effect=_refresh
        ), patch.object(
            kanban, "ensure_columns", side_effect=_ensure
        ) as ensure:
            result = await kanban.move_task_to_column("5", "Waiting for Human")
        assert result is True
        ensure.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_never_touches_a_board_the_ticket_does_not_live_on(
        self, kanban
    ):
        """Moving a ticket must not mutate an unrelated project's board.

        When the target column is missing, the board gets reconciled to
        Marcus's layout — which RENAMES a human's columns (Backlog ->
        Todo, Work in progress -> In Progress), adds four more and
        reorders them all. Attempting the move against the configured
        project before knowing where the ticket actually lives means a
        ticket in project 42 rewrites the columns of project 1, a board it
        has nothing to do with.

        Neither the write nor the reconcile may be aimed anywhere but the
        ticket's own project.
        """
        kanban._client = AsyncMock()
        kanban._column_map = {"todo": 1}  # configured project lacks the column
        kanban._column_status_map = {1: TaskStatus.TODO}
        assert kanban._project_id == 1

        async def _refresh(project_id=None):
            if project_id == 42:
                kanban._project_columns[42] = {"waiting for human": 9}
                kanban._column_status_map[9] = TaskStatus.WAITING_FOR_HUMAN

        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response(
                    _make_raw_task(task_id=5, column_id=70, project_id=42)
                ),
                _rpc_response(True),
                _rpc_response(
                    _make_raw_task(
                        task_id=5, column_id=9, project_id=42, is_active=1
                    )
                ),
            ]
        )
        with patch.object(
            kanban, "_refresh_columns", side_effect=_refresh
        ), patch.object(kanban, "ensure_columns", new=AsyncMock()) as ensure:
            result = await kanban.move_task_to_column("5", "Waiting for Human")

        assert result is True
        # Project 1's board was never reconciled or written to.
        for call in ensure.await_args_list:
            assert call.args[0] == 42
        touched = {
            c.kwargs["json"]["params"].get("project_id")
            for c in kanban._client.post.call_args_list
            if "project_id" in c.kwargs["json"]["params"]
        }
        assert touched <= {42}

    @pytest.mark.asyncio
    async def test_never_writes_a_foreign_projects_column_id(self, kanban):
        """The write must never carry a column id from another project's
        board, because Kanboard will happily store it.

        Kanboard only rejects a cross-project moveTaskPosition from
        v1.2.50 on, where app/Api/Procedure/TaskProcedure.php gained
        `if ($taskProjectId !== (int) $project_id) return false;`. On
        v1.2.49 and earlier the write still lands:
        TaskPositionModel::saveTaskPosition runs
        `UPDATE tasks SET column_id=? WHERE id=?` — scoped by task id
        only, never by project. The task then points at a column its own
        board does not contain and the card VANISHES from that board.

        Resolving the task's project before resolving the column makes
        that unrepresentable: the only column id that can ever be written
        comes from the task's own project's map.
        """
        kanban._client = AsyncMock()
        kanban._column_map = {"in progress": 2}  # project 1's column
        kanban._column_status_map = {2: TaskStatus.IN_PROGRESS}
        assert kanban._project_id == 1

        async def _refresh(project_id=None):
            if project_id == 42:
                kanban._project_columns[42] = {"in progress": 7}
                kanban._column_status_map[7] = TaskStatus.IN_PROGRESS

        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response(
                    _make_raw_task(
                        task_id=5, column_id=70, project_id=42, is_active=1
                    )
                ),
                _rpc_response(True),
                _rpc_response(
                    _make_raw_task(
                        task_id=5, column_id=7, project_id=42, is_active=1
                    )
                ),
            ]
        )
        with patch.object(kanban, "_refresh_columns", side_effect=_refresh):
            result = await kanban.move_task_to_column("5", "In Progress")

        assert result is True
        move_calls = [
            c.kwargs["json"]["params"]
            for c in kanban._client.post.call_args_list
            if c.kwargs["json"]["method"] == "moveTaskPosition"
        ]
        assert len(move_calls) == 1
        assert move_calls[0]["project_id"] == 42
        # Project 1's "in progress" (id 2) is never written anywhere.
        assert move_calls[0]["column_id"] == 7

    @pytest.mark.asyncio
    async def test_moves_when_column_only_exists_on_the_tasks_own_project(
        self, kanban
    ):
        """A ticket in another project is moved even when the configured
        project's board has no such column — that board is irrelevant to
        it and is never consulted.

        Realistic trigger: the configured project is still on Kanboard's
        stock columns (Backlog/Ready/Work in progress/Done), while the
        ticket's own project already has 'Waiting for Human'.
        """
        kanban._client = AsyncMock()
        kanban._column_map = {"backlog": 1, "done": 4}  # no 'waiting for human'
        kanban._column_status_map = {1: TaskStatus.TODO, 4: TaskStatus.DONE}
        assert kanban._project_id == 1

        async def _refresh(project_id=None):
            if project_id == 42:
                kanban._project_columns[42] = {"waiting for human": 9}
                kanban._column_status_map[9] = TaskStatus.WAITING_FOR_HUMAN

        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response(
                    _make_raw_task(task_id=5, column_id=70, project_id=42)
                ),
                _rpc_response(True),
                _rpc_response(
                    _make_raw_task(
                        task_id=5, column_id=9, project_id=42, is_active=1
                    )
                ),
            ]
        )
        with patch.object(
            kanban, "_refresh_columns", side_effect=_refresh
        ), patch.object(kanban, "ensure_columns", new=AsyncMock()) as ensure:
            result = await kanban.move_task_to_column("5", "Waiting for Human")

        assert result is True
        ensure.assert_not_awaited()  # project 42 already had the column
        move_calls = [
            c.kwargs["json"]["params"]
            for c in kanban._client.post.call_args_list
            if c.kwargs["json"]["method"] == "moveTaskPosition"
        ]
        assert len(move_calls) == 1
        assert move_calls[0]["project_id"] == 42
        assert move_calls[0]["column_id"] == 9

    @pytest.mark.asyncio
    async def test_failed_move_is_logged_loudly(self, kanban, caplog):
        """A move that ultimately fails must log an ERROR naming the ticket
        and column.

        14 of the 16 call sites in human_gated_workflow.py discard this
        method's boolean, so a False return is otherwise completely silent:
        Marcus's internal state advances, the card stays put, and the logs
        say nothing about why. Logging here — one place, rather than at 16
        call sites — makes every failure visible no matter who called.
        """
        import logging

        kanban._client = AsyncMock()
        kanban._column_map = {"in progress": 2}
        kanban._column_status_map = {2: TaskStatus.IN_PROGRESS}
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response(False),
                _rpc_response(  # never landed on column 2
                    _make_raw_task(task_id=5, column_id=1, project_id=1)
                ),
            ]
        )
        with caplog.at_level(logging.ERROR):
            result = await kanban.move_task_to_column("5", "In Progress")

        assert result is False
        assert any(
            "5" in r.getMessage() and "In Progress" in r.getMessage()
            for r in caplog.records
            if r.levelno >= logging.ERROR
        )

    @pytest.mark.asyncio
    async def test_successful_move_logs_no_error(self, kanban, caplog):
        """The happy path must stay quiet — no spurious ERROR noise."""
        import logging

        kanban._client = AsyncMock()
        kanban._column_map = {"in progress": 2}
        kanban._column_status_map = {2: TaskStatus.IN_PROGRESS}
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response(
                    _make_raw_task(task_id=5, column_id=1, project_id=1)
                ),
                _rpc_response(True),
                _rpc_response(
                    _make_raw_task(
                        task_id=5, column_id=2, project_id=1, is_active=1
                    )
                ),
            ]
        )
        with caplog.at_level(logging.ERROR):
            result = await kanban.move_task_to_column("5", "In Progress")

        assert result is True
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    @pytest.mark.asyncio
    async def test_no_retry_when_task_genuinely_stays_in_wrong_column(
        self, kanban
    ):
        """A task that genuinely refuses to move is reported as a failure
        without any further attempts — one lookup, one write, one
        verification, and no speculative retry against another board."""
        kanban._client = AsyncMock()
        kanban._column_map = {"in progress": 2}
        kanban._column_status_map = {2: TaskStatus.IN_PROGRESS}
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response(
                    _make_raw_task(task_id=5, column_id=1, project_id=1)
                ),
                _rpc_response(False),
                _rpc_response(
                    _make_raw_task(task_id=5, column_id=1, project_id=1)
                ),
            ]
        )
        result = await kanban.move_task_to_column("5", "In Progress")
        assert result is False
        assert kanban._client.post.await_count == 3


# ---------------------------------------------------------------------------
# _resolve_column_id tests
# ---------------------------------------------------------------------------


class TestResolveColumnId:
    """Column-name resolution must never silently pick an unrelated column.

    An exact (case-insensitive) name always wins. The fallback exists so
    Marcus's canonical "in progress" still finds a board's "Work in
    progress" — but a raw substring test is far too loose, because several
    of Marcus's column names are substrings of ordinary English words a
    human might well name a column:

        "done"  is a substring of "abandoned"
        "ready" is a substring of "already done"

    Matching those sends finished tickets to "Abandoned". Worse, because a
    match WAS found, the ensure_columns() self-heal never runs, so the
    correct column is never created and the mistake repeats forever.
    """

    def test_exact_name_wins(self, kanban):
        """An exact case-insensitive match is preferred over any fallback."""
        kanban._column_map = {"blocked - waiting for input": 1, "blocked": 2}
        assert kanban._resolve_column_id(1, "Blocked") == 2

    def test_word_boundary_fallback_still_finds_work_in_progress(self, kanban):
        """The fallback that matters keeps working: Marcus's canonical
        'in progress' resolves to Kanboard's stock 'Work in progress'."""
        kanban._column_map = {"backlog": 1, "work in progress": 2, "done": 3}
        assert kanban._resolve_column_id(1, "in progress") == 2

    def test_does_not_match_done_against_abandoned(self, kanban):
        """'done' must NOT resolve to an 'Abandoned' column."""
        kanban._column_map = {"abandoned": 1, "in progress": 2}
        assert kanban._resolve_column_id(1, "done") is None

    def test_does_not_match_ready_against_already_done(self, kanban):
        """'ready' must NOT resolve to an 'Already done' column."""
        kanban._column_map = {"already done": 1}
        assert kanban._resolve_column_id(1, "ready") is None

    def test_prefers_the_most_specific_match(self, kanban):
        """With several boundary matches, the shortest (most specific)
        column name wins, so resolution can't depend on board order."""
        kanban._column_map = {
            "blocked - waiting for human input": 1,
            "blocked externally": 2,
            "blocked": 3,
        }
        assert kanban._resolve_column_id(1, "blocked") == 3

    def test_unknown_column_returns_none(self, kanban):
        """A genuinely absent column returns None so the caller reconciles."""
        kanban._column_map = {"todo": 1, "done": 2}
        assert kanban._resolve_column_id(1, "waiting for human") is None


# ---------------------------------------------------------------------------
# download_attachment tests
# ---------------------------------------------------------------------------


class TestDownloadAttachment:
    """Test download_attachment()."""

    @pytest.mark.asyncio
    async def test_downloads_via_downloadTaskFile_rpc(self, kanban):
        """Content comes from the downloadTaskFile RPC, already base64.

        Kanboard's getTaskFile 'path' is an object-storage key (e.g.
        'tasks/123/<sha1>' under DATA_DIR/files), NOT a web route — the
        old implementation HTTP-GETting {base}/{path} could never fetch
        real file content. TaskFileProcedure::downloadTaskFile(file_id)
        returns the file's bytes base64-encoded in one call.
        """
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response({"id": 9, "name": "spec.pdf", "path": "tasks/5/abc"}),
                _rpc_response("aGVsbG8="),  # base64("hello")
            ]
        )
        result = await kanban.download_attachment("9", "fallback.pdf", task_id="5")
        assert result["success"] is True
        assert result["data"]["content"] == "aGVsbG8="
        assert result["data"]["filename"] == "spec.pdf"
        second_call_body = kanban._client.post.call_args_list[1].kwargs.get(
            "json"
        ) or kanban._client.post.call_args_list[1].args[1]
        assert second_call_body["method"] == "downloadTaskFile"
        assert second_call_body["params"] == {"file_id": 9}

    @pytest.mark.asyncio
    async def test_missing_file_returns_failure(self, kanban):
        """A file id Kanboard doesn't know returns success=False, no raise."""
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(return_value=_rpc_response(None))
        result = await kanban.download_attachment("404", "x.txt")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_empty_content_returns_failure(self, kanban):
        """downloadTaskFile returning empty/false yields success=False."""
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(
            side_effect=[
                _rpc_response({"id": 9, "name": "spec.pdf"}),
                _rpc_response(False),
            ]
        )
        result = await kanban.download_attachment("9", "spec.pdf")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# create_subtask / mark_subtask_done tests
# ---------------------------------------------------------------------------


class TestCreateSubtask:
    """Test create_subtask()."""

    @pytest.mark.asyncio
    async def test_raises_if_not_connected(self, kanban):
        """create_subtask() raises RuntimeError when client is None."""
        with pytest.raises(RuntimeError, match="connect()"):
            await kanban.create_subtask("10", "#11 Do the thing")

    @pytest.mark.asyncio
    async def test_returns_new_subtask_id(self, kanban):
        """A successful createSubtask call returns its id as a string."""
        kanban._client = AsyncMock()
        kanban._rpc = AsyncMock(return_value=42)

        result = await kanban.create_subtask("10", "#11 Do the thing")

        assert result == "42"
        kanban._rpc.assert_awaited_once_with(
            "createSubtask", task_id=10, title="#11 Do the thing"
        )

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self, kanban):
        """A falsy createSubtask response yields None, not a crash."""
        kanban._client = AsyncMock()
        kanban._rpc = AsyncMock(return_value=False)

        result = await kanban.create_subtask("10", "#11 Do the thing")

        assert result is None


class TestMarkSubtaskDone:
    """Test mark_subtask_done()."""

    @pytest.mark.asyncio
    async def test_raises_if_not_connected(self, kanban):
        """mark_subtask_done() raises RuntimeError when client is None."""
        with pytest.raises(RuntimeError, match="connect()"):
            await kanban.mark_subtask_done("10", "#11 ")

    @pytest.mark.asyncio
    async def test_marks_matching_subtask_done(self, kanban):
        """The subtask whose title starts with the prefix is set to done
        (status=2) via updateSubtask, keyed by its own id."""
        kanban._client = AsyncMock()

        async def fake_rpc(method, **params):
            if method == "getAllSubtasks":
                return [
                    {"id": 5, "title": "#9 Some other child"},
                    {"id": 6, "title": "#11 Do the thing"},
                ]
            if method == "updateSubtask":
                return True
            return None

        kanban._rpc = AsyncMock(side_effect=fake_rpc)

        result = await kanban.mark_subtask_done("10", "#11 ")

        assert result is True
        update_call = [
            c for c in kanban._rpc.call_args_list if c.args[0] == "updateSubtask"
        ][0]
        assert update_call.kwargs == {"id": 6, "task_id": 10, "status": 2}

    @pytest.mark.asyncio
    async def test_no_matching_subtask_returns_false(self, kanban):
        """No subtask matches the prefix → False, no updateSubtask call."""
        kanban._client = AsyncMock()

        async def fake_rpc(method, **params):
            if method == "getAllSubtasks":
                return [{"id": 5, "title": "#9 Some other child"}]
            return None

        kanban._rpc = AsyncMock(side_effect=fake_rpc)

        result = await kanban.mark_subtask_done("10", "#11 ")

        assert result is False
        assert all(
            c.args[0] != "updateSubtask" for c in kanban._rpc.call_args_list
        )


# ---------------------------------------------------------------------------
# assign_task tests
# ---------------------------------------------------------------------------


class TestAssignTask:
    """Test assign_task()."""

    @pytest.mark.asyncio
    async def test_assigns_by_numeric_id(self, kanban):
        """assign_task() with a numeric string calls updateTask with owner_id."""
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(return_value=_rpc_response(True))
        result = await kanban.assign_task("10", "5")
        assert result is True

    @pytest.mark.asyncio
    async def test_falls_back_to_comment_when_user_not_found(self, kanban):
        """assign_task() falls back to a comment when user lookup fails."""
        kanban._client = AsyncMock()
        kanban._comment_user_id = 7  # Marcus bot user already resolved (cached)
        # getUserByName(assignee) returns None → fall back to a comment.
        kanban._client.post = AsyncMock(
            side_effect=[_rpc_response(None), _rpc_response(99)]
        )
        result = await kanban.assign_task("10", "agent-xyz")
        assert result is True  # comment added successfully


# ---------------------------------------------------------------------------
# get_project_metrics tests
# ---------------------------------------------------------------------------


class TestGetProjectName:
    """Test get_project_name()."""

    @pytest.mark.asyncio
    async def test_returns_name_for_configured_project_from_cache(self, kanban):
        """The configured project's name is served from the connect()-time
        cache without an extra RPC call."""
        kanban._client = AsyncMock()
        kanban._project_name = "Marcus Project"
        # _project_id defaults to 1 per the `config` fixture
        result = await kanban.get_project_name(1)
        assert result == "Marcus Project"

    @pytest.mark.asyncio
    async def test_looks_up_a_different_project_via_rpc(self, kanban):
        """A project id other than the configured one is fetched live."""
        kanban._client = AsyncMock()
        kanban._project_name = "Marcus Project"
        kanban._client.post = AsyncMock(
            return_value=_rpc_response({"id": 7, "name": "Other Project"})
        )
        result = await kanban.get_project_name(7)
        assert result == "Other Project"

    @pytest.mark.asyncio
    async def test_returns_none_when_project_not_found(self, kanban):
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(return_value=_rpc_response(None))
        result = await kanban.get_project_name(999)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_rpc_failure(self, kanban):
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(side_effect=RuntimeError("API error"))
        result = await kanban.get_project_name(7)
        assert result is None

    @pytest.mark.asyncio
    async def test_raises_if_not_connected(self, kanban):
        with pytest.raises(RuntimeError, match="connect()"):
            await kanban.get_project_name(1)


class TestGetProjectMetrics:
    """Test get_project_metrics()."""

    @pytest.mark.asyncio
    async def test_counts_by_status(self, kanban):
        """Metrics contain correct counts per status."""
        now = datetime.now(timezone.utc)
        tasks = [
            Task(
                id="1",
                name="T1",
                status=TaskStatus.TODO,
                assigned_to=None,
                priority=Priority.MEDIUM,
                description="",
                created_at=now,
                updated_at=now,
                due_date=None,
                estimated_hours=0.0,
            ),
            Task(
                id="2",
                name="T2",
                status=TaskStatus.IN_PROGRESS,
                assigned_to=None,
                priority=Priority.MEDIUM,
                description="",
                created_at=now,
                updated_at=now,
                due_date=None,
                estimated_hours=0.0,
            ),
            Task(
                id="3",
                name="T3",
                status=TaskStatus.DONE,
                assigned_to=None,
                priority=Priority.MEDIUM,
                description="",
                created_at=now,
                updated_at=now,
                due_date=None,
                estimated_hours=0.0,
            ),
            Task(
                id="4",
                name="T4",
                status=TaskStatus.BLOCKED,
                assigned_to=None,
                priority=Priority.MEDIUM,
                description="",
                created_at=now,
                updated_at=now,
                due_date=None,
                estimated_hours=0.0,
            ),
        ]
        kanban._client = AsyncMock()  # satisfy the connection guard
        kanban.get_all_tasks = AsyncMock(return_value=tasks)
        metrics = await kanban.get_project_metrics()
        assert metrics["total_tasks"] == 4
        assert metrics["backlog_tasks"] == 1
        assert metrics["in_progress_tasks"] == 1
        assert metrics["completed_tasks"] == 1
        assert metrics["blocked_tasks"] == 1


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestParseKanboardTs:
    """Test _parse_kanboard_ts helper."""

    def test_parses_unix_timestamp(self):
        """Integer Unix timestamp is converted to UTC datetime."""
        result = _parse_kanboard_ts(1700000000)
        assert result is not None
        assert result.tzinfo is not None

    def test_returns_none_for_zero(self):
        """Zero timestamp returns None (unset date in Kanboard)."""
        assert _parse_kanboard_ts(0) is None

    def test_returns_none_for_none(self):
        """None input returns None."""
        assert _parse_kanboard_ts(None) is None

    def test_returns_none_for_empty_string(self):
        """Empty string returns None."""
        assert _parse_kanboard_ts("") is None

    def test_parses_string_timestamp(self):
        """String timestamps (as returned by some Kanboard versions) are parsed."""
        result = _parse_kanboard_ts("1700000000")
        assert result is not None

    def test_result_is_utc(self):
        """Parsed datetime is always UTC."""
        result = _parse_kanboard_ts(1700000000)
        assert result.tzinfo == timezone.utc


class TestMarcusPriorityToKb:
    """Test _marcus_priority_to_kb helper."""

    @pytest.mark.parametrize(
        "priority,expected",
        [
            ("low", 0),
            ("medium", 1),
            ("high", 2),
            ("urgent", 3),
            ("critical", 3),
            ("Priority.LOW", 0),
            ("Priority.MEDIUM", 1),
            ("Priority.HIGH", 2),
            ("Priority.URGENT", 3),
            (None, 1),  # default medium
        ],
    )
    def test_priority_conversions(self, priority, expected):
        """Marcus priority values convert to the correct Kanboard integers."""
        assert _marcus_priority_to_kb(priority) == expected


# ---------------------------------------------------------------------------
# classify_task_links tests
# ---------------------------------------------------------------------------


class TestClassifyTaskLinks:
    """Test the module-level classify_task_links() helper directly."""

    def test_empty_input_returns_empty_groups(self):
        assert classify_task_links([]) == {
            "depends_on": [],
            "blocks": [],
            "relates_to": [],
        }

    @pytest.mark.parametrize(
        "label,group",
        [
            ("is blocked by", "depends_on"),
            ("is a child of", "depends_on"),
            ("depends on", "depends_on"),
            ("blocks", "blocks"),
            ("is a parent of", "blocks"),
            ("relates to", "relates_to"),
            ("", "relates_to"),
        ],
    )
    def test_label_classification(self, label, group):
        """Each link label lands in its direction group, reading the real
        Kanboard payload key ``task_id`` (the opposite task's id, aliased
        by TaskLinkModel::getAll — never ``opposite_task_id``)."""
        raw = [
            {
                "label": label,
                "task_id": 7,
                "title": "Other ticket",
                "column_title": "Todo",
            }
        ]
        result = classify_task_links(raw)
        assert len(result[group]) == 1
        assert result[group][0] == {
            "task_id": "7",
            "title": "Other ticket",
            "column": "Todo",
        }

    def test_label_matching_is_case_insensitive(self):
        """Label matching lower-cases before comparing against the label sets."""
        raw = [{"label": "BLOCKS", "task_id": 1, "title": "x", "column_title": "y"}]
        result = classify_task_links(raw)
        assert len(result["blocks"]) == 1

    def test_missing_fields_default_safely(self):
        """A link entry missing task_id/title/column_title must not
        raise — fields default to empty rather than crashing."""
        result = classify_task_links([{"label": "blocks"}])
        assert result["blocks"] == [{"task_id": "", "title": "", "column": ""}]


class TestEnsureColumns:
    """ensure_columns reconciles a project to Marcus's column layout."""

    @pytest.mark.asyncio
    async def test_fresh_project_gets_marcus_columns_in_order(self, kanban):
        """Kanboard defaults are renamed + missing columns added + ordered."""
        kanban._client = AsyncMock()
        # Kanboard's default new-project columns.
        defaults = [
            {"id": 1, "title": "Backlog", "position": 1},
            {"id": 2, "title": "Ready", "position": 2},
            {"id": 3, "title": "Work in progress", "position": 3},
            {"id": 4, "title": "Done", "position": 4},
        ]

        async def fake_rpc(method, **params):
            if method == "getColumns":
                return defaults
            if method == "addColumn":
                # Blocked -> 5, Waiting for Human -> 6
                return 5 if params["title"] == "Blocked" else 6
            return True

        kanban._rpc = AsyncMock(side_effect=fake_rpc)

        result = await kanban.ensure_columns(7)

        assert result is True
        calls = kanban._rpc.call_args_list
        # Renames: Backlog->Todo, Work in progress->In Progress
        renamed = {
            c.kwargs["title"]
            for c in calls
            if c.args and c.args[0] == "updateColumn"
        }
        assert renamed == {"Todo", "In Progress"}
        # Added the two truly-missing columns
        added = {
            c.kwargs["title"]
            for c in calls
            if c.args and c.args[0] == "addColumn"
        }
        assert added == {"Blocked", "Waiting for Human"}
        # Repositioned all six into the desired order (positions 1..6)
        repos = [
            c for c in calls if c.args and c.args[0] == "changeColumnPosition"
        ]
        assert len(repos) == 6
        assert [c.kwargs["position"] for c in repos] == [1, 2, 3, 4, 5, 6]

    @pytest.mark.asyncio
    async def test_failed_add_column_reports_failure(self, kanban):
        """A column Kanboard refused to create must make ensure_columns
        report False, not claim a successful reconciliation.

        addColumn returns the new column id, or a falsy value when it
        fails (e.g. the API user can't write this project's board).
        Storing that falsy id and returning True unconditionally told the
        caller the board was reconciled when the target column still does
        not exist — so the caller retried a resolve that could never
        succeed, and the card silently never moved.
        """
        kanban._client = AsyncMock()

        async def fake_rpc(method, **params):
            if method == "getColumns":
                return [{"id": 1, "title": "Todo", "position": 1}]
            if method == "addColumn":
                return None  # Kanboard refused
            return True

        kanban._rpc = AsyncMock(side_effect=fake_rpc)

        result = await kanban.ensure_columns(7)

        assert result is False
        # The falsy id must never reach changeColumnPosition.
        repos = [
            c
            for c in kanban._rpc.call_args_list
            if c.args and c.args[0] == "changeColumnPosition"
        ]
        assert all(c.kwargs["column_id"] for c in repos)

    @pytest.mark.asyncio
    async def test_idempotent_when_already_correct(self, kanban):
        """Already-Marcus columns → no rename, no add (only repositions)."""
        kanban._client = AsyncMock()
        existing = [
            {"id": i + 1, "title": t, "position": i + 1}
            for i, t in enumerate(
                ["Todo", "Ready", "In Progress", "Blocked", "Waiting for Human", "Done"]
            )
        ]

        async def fake_rpc(method, **params):
            return existing if method == "getColumns" else True

        kanban._rpc = AsyncMock(side_effect=fake_rpc)

        await kanban.ensure_columns(7)

        methods = [c.args[0] for c in kanban._rpc.call_args_list]
        assert "updateColumn" not in methods
        assert "addColumn" not in methods

    @pytest.mark.asyncio
    async def test_never_removes_extra_columns(self, kanban):
        """A human-added extra column is left alone (no removeColumn)."""
        kanban._client = AsyncMock()
        existing = [
            {"id": 1, "title": "Todo", "position": 1},
            {"id": 2, "title": "Ready", "position": 2},
            {"id": 3, "title": "In Progress", "position": 3},
            {"id": 4, "title": "QA", "position": 4},  # human extra
            {"id": 5, "title": "Blocked", "position": 5},
            {"id": 6, "title": "Waiting for Human", "position": 6},
            {"id": 7, "title": "Done", "position": 7},
        ]

        async def fake_rpc(method, **params):
            return existing if method == "getColumns" else True

        kanban._rpc = AsyncMock(side_effect=fake_rpc)

        await kanban.ensure_columns(7)

        methods = [c.args[0] for c in kanban._rpc.call_args_list]
        assert "removeColumn" not in methods

    @pytest.mark.asyncio
    async def test_refreshes_column_cache_for_configured_project(self, kanban):
        """Reconciling the CONFIGURED project rebuilds the column cache.

        Otherwise moves to newly-added Blocked/Waiting-for-Human columns
        fail with 'column not found' until the process restarts.
        """
        kanban._client = AsyncMock()
        kanban._project_id = 7  # make the reconciled project the configured one
        kanban._column_map = {}  # simulate a stale/empty connect()-time cache
        marcus_cols = [
            {"id": i + 1, "title": t, "position": i + 1}
            for i, t in enumerate(
                ["Todo", "Ready", "In Progress", "Blocked", "Waiting for Human", "Done"]
            )
        ]

        async def fake_rpc(method, **params):
            return marcus_cols if method == "getColumns" else True

        kanban._rpc = AsyncMock(side_effect=fake_rpc)

        await kanban.ensure_columns(7)

        # Cache now resolves the gate columns the workflow moves cards to.
        assert "blocked" in kanban._column_map
        assert "waiting for human" in kanban._column_map
        assert "in progress" in kanban._column_map

    @pytest.mark.asyncio
    async def test_does_not_refresh_cache_for_other_project(self, kanban):
        """Reconciling a NON-configured project leaves this client's cache."""
        kanban._client = AsyncMock()
        kanban._project_id = 1
        kanban._column_map = {"sentinel": 99}

        async def fake_rpc(method, **params):
            return [] if method == "getColumns" else True

        kanban._rpc = AsyncMock(side_effect=fake_rpc)

        await kanban.ensure_columns(7)  # different project

        assert kanban._column_map == {"sentinel": 99}

    @pytest.mark.asyncio
    async def test_raises_if_not_connected(self, kanban):
        with pytest.raises(RuntimeError, match="connect()"):
            await kanban.ensure_columns(7)


# ---------------------------------------------------------------------------
# _rpc() retry-on-transient-failure tests
# ---------------------------------------------------------------------------


class TestRpcRetry:
    """_rpc() must retry a TRANSIENT Kanboard failure (5xx / network error —
    e.g. a fleeting SQLite "database is locked" surfacing as an uncaught PHP
    exception → HTTP 500) instead of hard-failing the whole operation on the
    first hiccup. It must NOT retry a clean JSON-RPC error response (Kanboard
    successfully processed the request and told us it's invalid) or a 4xx —
    those are not transient, and retrying just delays the same failure.
    """

    @pytest.fixture(autouse=True)
    def _no_sleep(self):
        """Retries would otherwise really sleep (with backoff) and slow the
        suite; every test here patches asyncio.sleep to a no-op."""
        with patch("asyncio.sleep", new=AsyncMock()):
            yield

    @pytest.mark.asyncio
    async def test_retries_on_5xx_then_succeeds(self, kanban):
        """A transient 500 is retried and a subsequent success is returned."""
        import httpx

        kanban._client = AsyncMock()
        err_response = MagicMock()
        err_response.status_code = 500
        err_response.text = "database is locked"
        call_count = {"n": 0}

        async def post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise httpx.HTTPStatusError(
                    "500", request=MagicMock(), response=err_response
                )
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"jsonrpc": "2.0", "result": 42})
            return resp

        kanban._client.post = AsyncMock(side_effect=post)
        result = await kanban._rpc("getTask", task_id=1)
        assert result == 42
        assert call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts(self, kanban):
        """Persistent 5xx failures still eventually raise, bounded."""
        import httpx

        kanban._client = AsyncMock()
        err_response = MagicMock()
        err_response.status_code = 500
        err_response.text = "database is locked"
        kanban._client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "500", request=MagicMock(), response=err_response
            )
        )
        with pytest.raises(httpx.HTTPStatusError):
            await kanban._rpc("getTask", task_id=1)
        assert kanban._client.post.await_count <= 5  # bounded, not infinite
        assert kanban._client.post.await_count > 1  # actually retried

    @pytest.mark.asyncio
    async def test_does_not_retry_on_4xx(self, kanban):
        """A 4xx (client error — bad auth, malformed request) is not
        transient; retrying it would just waste time re-hitting the same
        failure."""
        import httpx

        kanban._client = AsyncMock()
        err_response = MagicMock()
        err_response.status_code = 401
        err_response.text = "Unauthorized"
        kanban._client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "401", request=MagicMock(), response=err_response
            )
        )
        with pytest.raises(httpx.HTTPStatusError):
            await kanban._rpc("getTask", task_id=1)
        assert kanban._client.post.await_count == 1

    @pytest.mark.asyncio
    async def test_does_not_retry_on_clean_rpc_error(self, kanban):
        """A well-formed {"error": ...} JSON-RPC response means Kanboard
        processed the request and rejected it — not a transient failure."""
        kanban._client = AsyncMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(
            return_value={
                "jsonrpc": "2.0",
                "error": {"code": -32602, "message": "Task not found"},
            }
        )
        kanban._client.post = AsyncMock(return_value=resp)
        with pytest.raises(RuntimeError, match="Task not found"):
            await kanban._rpc("getTask", task_id=999)
        assert kanban._client.post.await_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_network_error(self, kanban):
        """A transient connection error (daemon momentarily unreachable) is
        also retried, same as a 5xx."""
        import httpx

        kanban._client = AsyncMock()
        call_count = {"n": 0}

        async def post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise httpx.ConnectError("connection refused")
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"jsonrpc": "2.0", "result": True})
            return resp

        kanban._client.post = AsyncMock(side_effect=post)
        result = await kanban._rpc("moveTaskPosition", task_id=1)
        assert result is True
        assert call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_does_not_retry_on_read_timeout(self, kanban):
        """A ReadTimeout (unlike a ConnectError) means the request may
        already have reached and been processed by the Kanboard server —
        the client just never saw the response. Blindly retrying a
        non-idempotent write like createComment risks posting it twice, so
        this must NOT be retried the way a pre-send ConnectError is."""
        import httpx

        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(
            side_effect=httpx.ReadTimeout("timed out waiting for response")
        )
        with pytest.raises(httpx.ReadTimeout):
            await kanban._rpc("createComment", task_id=1, content="hi")
        assert kanban._client.post.await_count == 1
