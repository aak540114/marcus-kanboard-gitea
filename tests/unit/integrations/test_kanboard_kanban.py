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

        import httpx

        with patch("httpx.AsyncClient", return_value=kanban._client):
            result = await kanban.connect()

        assert result is True
        assert kanban._project_name == "My Project"

    @pytest.mark.asyncio
    async def test_connect_returns_false_when_project_not_found(self, kanban):
        """connect() returns False if the project ID doesn't exist."""
        import httpx

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

    def test_estimated_hours_from_seconds(self, kanban):
        """time_estimated in seconds is converted to hours."""
        kanban._column_status_map = {1: TaskStatus.TODO}
        task = kanban._to_task(_make_raw_task(time_estimated=7200))  # 2h
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
        kanban._client.post = AsyncMock(side_effect=[active_resp, closed_resp])
        kanban._column_status_map = {1: TaskStatus.TODO}

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
        kanban._client.post = AsyncMock(side_effect=[active_resp, closed_resp])
        kanban._column_status_map = {1: TaskStatus.TODO}

        tasks = await kanban.get_all_tasks()
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_empty_board_returns_empty_list(self, kanban):
        """Empty project returns an empty list."""
        kanban._client = AsyncMock()
        kanban._client.post = AsyncMock(
            side_effect=[_rpc_response([]), _rpc_response([])]
        )
        tasks = await kanban.get_all_tasks()
        assert tasks == []


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


# ---------------------------------------------------------------------------
# move_task_to_column tests
# ---------------------------------------------------------------------------


class TestMoveTaskToColumn:
    """Test move_task_to_column()."""

    @pytest.mark.asyncio
    async def test_moves_to_known_column(self, kanban):
        """move_task_to_column() calls moveTaskPosition with the correct column ID."""
        kanban._client = AsyncMock()
        kanban._column_map = {"in progress": 2, "done": 3}
        kanban._column_status_map = {2: TaskStatus.IN_PROGRESS, 3: TaskStatus.DONE}
        # moveTaskPosition → True; openTask response
        kanban._client.post = AsyncMock(
            side_effect=[_rpc_response(True), _rpc_response(True)]
        )
        result = await kanban.move_task_to_column("5", "In Progress")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_for_unknown_column(self, kanban):
        """move_task_to_column() returns False for an unknown column name."""
        kanban._client = AsyncMock()
        kanban._column_map = {"backlog": 1}
        kanban._column_status_map = {1: TaskStatus.TODO}
        result = await kanban.move_task_to_column("5", "NonExistentColumn")
        assert result is False

    @pytest.mark.asyncio
    async def test_raises_if_not_connected(self, kanban):
        """move_task_to_column() raises RuntimeError when client is None."""
        with pytest.raises(RuntimeError, match="connect()"):
            await kanban.move_task_to_column("1", "Done")


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
        # getUserByName returns None
        kanban._client.post = AsyncMock(
            side_effect=[_rpc_response(None), _rpc_response(99)]
        )
        result = await kanban.assign_task("10", "agent-xyz")
        assert result is True  # comment added successfully


# ---------------------------------------------------------------------------
# get_project_metrics tests
# ---------------------------------------------------------------------------


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
