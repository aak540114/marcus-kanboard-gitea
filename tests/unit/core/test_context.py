"""
Unit tests for the Context system
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.context import (
    Context,
    Decision,
    DependentTask,
    TaskContext,
    sweep_context_retention,
)
from src.core.events import Events, EventTypes
from src.core.models import Priority, Task, TaskStatus


class TestTaskContext:
    """Test suite for TaskContext dataclass"""

    def test_task_context_creation(self):
        """Test creating a TaskContext"""
        context = TaskContext(
            task_id="task_123",
            previous_implementations={"task_1": {"apis": ["GET /users"]}},
            dependent_tasks=[{"task_id": "task_2", "task_name": "Frontend"}],
            related_patterns=[{"type": "api", "pattern": "REST"}],
            architectural_decisions=[{"what": "Use JWT", "why": "Stateless"}],
        )

        assert context.task_id == "task_123"
        assert "task_1" in context.previous_implementations
        assert len(context.dependent_tasks) == 1
        assert len(context.related_patterns) == 1
        assert len(context.architectural_decisions) == 1

    def test_task_context_to_dict(self):
        """Test converting TaskContext to dictionary"""
        context = TaskContext(
            task_id="task_123",
            previous_implementations={"task_1": {"apis": ["GET /users"]}},
        )

        result = context.to_dict()

        assert result["task_id"] == "task_123"
        assert "previous_implementations" in result
        assert "dependent_tasks" in result


class TestDependentTask:
    """Test suite for DependentTask dataclass"""

    def test_dependent_task_creation(self):
        """Test creating a DependentTask"""
        dep_task = DependentTask(
            task_id="task_2",
            task_name="Login UI",
            expected_interface="/auth/login endpoint",
        )

        assert dep_task.task_id == "task_2"
        assert dep_task.task_name == "Login UI"
        assert dep_task.expected_interface == "/auth/login endpoint"
        assert dep_task.dependency_type == "functional"


class TestDecision:
    """Test suite for Decision dataclass"""

    def test_decision_creation(self):
        """Test creating a Decision"""
        decision = Decision(
            decision_id="dec_1",
            task_id="task_123",
            agent_id="agent_1",
            timestamp=datetime.now(timezone.utc),
            what="Use PostgreSQL",
            why="Need ACID compliance",
            impact="All services must use SQL",
        )

        assert decision.decision_id == "dec_1"
        assert decision.task_id == "task_123"
        assert decision.agent_id == "agent_1"
        assert decision.what == "Use PostgreSQL"

    def test_decision_to_dict(self):
        """Test converting Decision to dictionary"""
        timestamp = datetime.now(timezone.utc)
        decision = Decision(
            decision_id="dec_1",
            task_id="task_123",
            agent_id="agent_1",
            timestamp=timestamp,
            what="Use JWT",
            why="Stateless auth",
            impact="All APIs need JWT validation",
        )

        result = decision.to_dict()

        assert result["decision_id"] == "dec_1"
        assert result["timestamp"] == timestamp.isoformat()
        assert result["what"] == "Use JWT"


class TestContext:
    """Test suite for Context system"""

    @pytest.fixture
    def context(self):
        """Create a Context instance for testing"""
        return Context()

    @pytest.fixture
    def context_with_events(self):
        """Create a Context instance with Events system"""
        events = Events()
        return Context(events=events)

    def test_initialization(self, context):
        """Test Context initialization"""
        assert context.implementations == {}
        assert context.dependencies == {}
        assert context.decisions == []
        assert context.patterns == {}
        assert context._decision_counter == 0

    @pytest.mark.asyncio
    async def test_add_implementation(self, context):
        """Test adding implementation details"""
        await context.add_implementation(
            "task_1",
            {
                "apis": ["GET /users", "POST /users"],
                "models": ["User"],
                "patterns": [{"type": "rest", "name": "RESTful API"}],
            },
        )

        assert "task_1" in context.implementations
        assert "apis" in context.implementations["task_1"]
        assert len(context.implementations["task_1"]["apis"]) == 2
        assert "rest" in context.patterns

    @pytest.mark.asyncio
    async def test_add_implementation_with_events(self, context_with_events):
        """Test that adding implementation triggers events"""
        handler = AsyncMock()
        context_with_events.events.subscribe(EventTypes.IMPLEMENTATION_FOUND, handler)

        await context_with_events.add_implementation("task_1", {"apis": ["GET /users"]})

        handler.assert_called_once()
        event = handler.call_args[0][0]
        assert event.event_type == EventTypes.IMPLEMENTATION_FOUND
        assert event.data["task_id"] == "task_1"

    def test_add_dependency(self, context):
        """Test adding task dependencies"""
        dep_task = DependentTask(
            task_id="task_2",
            task_name="Frontend Login",
            expected_interface="/auth/login endpoint",
        )

        context.add_dependency("task_1", dep_task)

        assert "task_1" in context.dependencies
        assert len(context.dependencies["task_1"]) == 1
        assert context.dependencies["task_1"][0].task_name == "Frontend Login"

    @pytest.mark.asyncio
    async def test_log_decision(self, context):
        """Test logging architectural decisions"""
        decision = await context.log_decision(
            agent_id="agent_1",
            task_id="task_123",
            what="Use JWT for authentication",
            why="Need stateless auth for mobile apps",
            impact="All endpoints must validate JWT tokens",
        )

        assert decision.decision_id.startswith("dec_")
        assert decision.agent_id == "agent_1"
        assert decision.task_id == "task_123"
        assert len(context.decisions) == 1

    @pytest.mark.asyncio
    async def test_log_decision_with_events(self, context_with_events):
        """Test that logging decision triggers events"""
        handler = AsyncMock()
        context_with_events.events.subscribe(EventTypes.DECISION_LOGGED, handler)

        await context_with_events.log_decision(
            "agent_1", "task_123", "Use PostgreSQL", "Need ACID", "All services use SQL"
        )

        handler.assert_called_once()
        event = handler.call_args[0][0]
        assert event.event_type == EventTypes.DECISION_LOGGED
        assert event.data["what"] == "Use PostgreSQL"

    @pytest.mark.asyncio
    async def test_get_context_empty(self, context):
        """Test getting context for a task with no dependencies"""
        task_context = await context.get_context("task_123", [])

        assert task_context.task_id == "task_123"
        assert len(task_context.previous_implementations) == 0
        assert len(task_context.dependent_tasks) == 0

    @pytest.mark.asyncio
    async def test_get_context_with_dependencies(self, context):
        """Test getting context with implementations and dependencies"""
        # Add some implementations
        await context.add_implementation(
            "task_1", {"apis": ["GET /users"], "models": ["User"]}
        )

        # Add dependent tasks
        context.add_dependency(
            "task_123", DependentTask("task_2", "Frontend", "/api/data")
        )

        # Log a decision
        await context.log_decision(
            "agent_1", "task_1", "Use REST", "Standard approach", "task_123"
        )

        # Get context
        task_context = await context.get_context("task_123", ["task_1"])

        assert "task_1" in task_context.previous_implementations
        assert len(task_context.dependent_tasks) == 1
        assert task_context.dependent_tasks[0]["task_name"] == "Frontend"
        assert len(task_context.architectural_decisions) > 0

    def test_analyze_dependencies(self, context):
        """Test analyzing task dependencies"""
        from datetime import datetime


        tasks = [
            Task(
                id="task_1",
                name="Backend API",
                description="Build API",
                status=TaskStatus.TODO,
                priority=Priority.MEDIUM,
                assigned_to=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                due_date=None,
                estimated_hours=8.0,
                labels=["backend", "api"],
                dependencies=[],
            ),
            Task(
                id="task_2",
                name="Frontend UI",
                description="Build UI",
                status=TaskStatus.TODO,
                priority=Priority.MEDIUM,
                assigned_to=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                due_date=None,
                estimated_hours=6.0,
                labels=["frontend", "ui"],
                dependencies=["task_1"],
            ),
            Task(
                id="task_3",
                name="API Tests",
                description="Test API",
                status=TaskStatus.TODO,
                priority=Priority.MEDIUM,
                assigned_to=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                due_date=None,
                estimated_hours=4.0,
                labels=["test", "api"],
                dependencies=[],
            ),
        ]

        async def run_test():
            dep_map = await context.analyze_dependencies(tasks)

            # task_1 should have task_2 as dependent (direct dependency)
            assert "task_1" in dep_map
            assert "task_2" in dep_map["task_1"]

            # task_1 might have task_3 as dependent (inferred)
            # This depends on inference rules


        asyncio.run(run_test())

    def test_infer_dependency(self, context):
        """Test dependency inference logic"""
        from datetime import datetime


        backend_task = Task(
            id="task_1",
            name="User API",
            description="User API implementation",
            status=TaskStatus.TODO,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=8.0,
            labels=["backend", "api"],
        )

        frontend_task = Task(
            id="task_2",
            name="User Dashboard",
            description="User dashboard interface",
            status=TaskStatus.TODO,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=6.0,
            labels=["frontend", "ui"],
        )

        test_task = Task(
            id="task_3",
            name="API Tests",
            description="API testing suite",
            status=TaskStatus.TODO,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=4.0,
            labels=["test"],
        )

        # Frontend should depend on backend
        assert context._infer_dependency(frontend_task, backend_task) is True

        # Tests should depend on implementation
        assert context._infer_dependency(test_task, backend_task) is True

        # Backend should not depend on frontend
        assert context._infer_dependency(backend_task, frontend_task) is False

    @pytest.mark.asyncio
    async def test_get_decisions_for_task(self, context):
        """Test getting decisions for a specific task"""
        await context.log_decision(
            "agent_1", "task_1", "Decision 1", "Why 1", "Impact 1"
        )
        await context.log_decision(
            "agent_2", "task_2", "Decision 2", "Why 2", "Impact 2"
        )
        await context.log_decision(
            "agent_1", "task_1", "Decision 3", "Why 3", "Impact 3"
        )

        task_1_decisions = await context.get_decisions_for_task("task_1")

        assert len(task_1_decisions) == 2
        assert all(d.task_id == "task_1" for d in task_1_decisions)

    @pytest.mark.asyncio
    async def test_get_implementation_summary(self, context):
        """Test getting implementation summary"""
        await context.add_implementation("task_1", {"apis": ["GET /users"]})
        await context.add_implementation("task_2", {"apis": ["GET /posts"]})
        await context.log_decision("agent_1", "task_1", "Use REST", "Standard", "All")

        summary = await context.get_implementation_summary()

        assert summary["total_implementations"] == 2
        assert summary["total_decisions"] == 1
        assert "recent_implementations" in summary

    @pytest.mark.asyncio
    async def test_clear_old_data(self, context):
        """Test clearing old context data"""
        # Add current data
        await context.add_implementation("task_1", {"apis": ["GET /users"]})

        # Add old decision (mock old timestamp)
        old_decision = Decision(
            decision_id="old_1",
            task_id="old_task",
            agent_id="agent_1",
            timestamp=datetime.now(timezone.utc) - timedelta(days=40),
            what="Old decision",
            why="Old reason",
            impact="Old impact",
        )
        context.decisions.append(old_decision)

        # Add recent decision
        await context.log_decision(
            "agent_2", "task_2", "Recent decision", "Recent reason", "Recent impact"
        )

        # Should have 2 decisions before clearing
        assert len(context.decisions) == 2

        # Clear data older than 30 days
        await context.clear_old_data(days=30)

        # Should only have 1 recent decision
        assert len(context.decisions) == 1
        assert context.decisions[0].what == "Recent decision"

    @pytest.mark.asyncio
    async def test_pattern_extraction(self, context):
        """Test pattern extraction from implementations"""
        await context.add_implementation(
            "task_1",
            {
                "patterns": [
                    {"type": "auth", "name": "JWT"},
                    {"type": "api", "name": "REST"},
                ]
            },
        )

        await context.add_implementation(
            "task_2",
            {
                "patterns": [
                    {"type": "auth", "name": "OAuth"},
                    {"type": "api", "name": "GraphQL"},
                ]
            },
        )

        # Check patterns were extracted
        assert "auth" in context.patterns
        assert "api" in context.patterns
        assert len(context.patterns["auth"]) == 2
        assert len(context.patterns["api"]) == 2


class TestDecisionsByTaskIdIndex:
    """Test suite for Context._decisions_by_task_id staying in sync with
    self.decisions across every mutation site."""

    @pytest.fixture
    def context(self):
        """Create a Context instance for testing"""
        return Context()

    @pytest.mark.asyncio
    async def test_log_decision_populates_index(self, context):
        """log_decision must add the new Decision to the task_id index,
        not just to self.decisions."""
        decision = await context.log_decision(
            "agent_1", "task_1", "Use REST", "Standard", "All"
        )

        assert context._decisions_by_task_id["task_1"] == [decision]

    @pytest.mark.asyncio
    async def test_index_groups_multiple_decisions_for_same_task(self, context):
        """Two decisions logged against the same task_id must both appear
        under that key, in logging order."""
        d1 = await context.log_decision("agent_1", "task_1", "A", "why A", "x")
        d2 = await context.log_decision("agent_2", "task_1", "B", "why B", "y")

        assert context._decisions_by_task_id["task_1"] == [d1, d2]

    @pytest.mark.asyncio
    async def test_clear_old_data_rebuilds_index_dropping_pruned_decisions(
        self, context
    ):
        """The staleness bug this test guards against: clear_old_data used
        to prune self.decisions without touching
        self._decisions_by_task_id, leaving the index pointing at
        decisions that no longer exist in self.decisions. If that
        regresses, get_decisions_for_task (which reads only the index)
        would keep returning an old decision forever, no matter how many
        days are passed to clear_old_data.
        """
        old_decision = Decision(
            decision_id="old_1",
            task_id="old_task",
            agent_id="agent_1",
            timestamp=datetime.now(timezone.utc) - timedelta(days=40),
            what="Old decision",
            why="Old reason",
            impact="Old impact",
        )
        context.decisions.append(old_decision)
        context._decisions_by_task_id.setdefault("old_task", []).append(old_decision)

        recent = await context.log_decision(
            "agent_2", "task_2", "Recent decision", "Recent reason", "Recent impact"
        )

        await context.clear_old_data(days=30)

        assert context._decisions_by_task_id.get("old_task", []) == []
        assert context._decisions_by_task_id["task_2"] == [recent]
        assert await context.get_decisions_for_task("old_task") == []
        assert await context.get_decisions_for_task("task_2") == [recent]

    @pytest.mark.asyncio
    async def test_get_decisions_for_task_reads_from_index(self, context):
        """get_decisions_for_task must reflect the index, not a fresh scan
        of self.decisions — proven by mutating self.decisions directly
        (bypassing log_decision) and confirming the index is unaffected."""
        await context.log_decision("agent_1", "task_1", "A", "why", "impact")

        stray = Decision(
            decision_id="stray_1",
            task_id="task_1",
            agent_id="agent_2",
            timestamp=datetime.now(timezone.utc),
            what="Stray",
            why="Bypassed the index on purpose",
            impact="none",
        )
        context.decisions.append(stray)

        result = await context.get_decisions_for_task("task_1")

        assert stray not in result

    @pytest.mark.asyncio
    async def test_get_context_dependency_decisions_use_index(self, context):
        """get_context's dependency-decision lookup must find decisions
        logged against a dependency task_id via the index."""
        await context.log_decision(
            "agent_1", "dep_task", "Use REST", "Standard approach", "n/a"
        )

        task_context = await context.get_context("task_123", ["dep_task"])

        assert len(task_context.architectural_decisions) == 1
        assert task_context.architectural_decisions[0]["what"] == "Use REST"

    @pytest.mark.asyncio
    async def test_get_context_loads_persisted_decisions_before_first_call(self):
        """get_context must trigger the lazy persisted-data load itself.

        Before this fix, get_context never called
        _ensure_persisted_data_loaded, so a Context backed by persistence
        would silently scan an empty self.decisions/index on its very
        first get_context call — missing every decision from a previous
        run — until some other method (get_decisions_for_task,
        clear_old_data, ...) happened to trigger the load first.
        """
        persisted_decision = Decision(
            decision_id="dec_1",
            task_id="dep_task",
            agent_id="agent_1",
            timestamp=datetime.now(timezone.utc),
            what="Persisted decision",
            why="From a previous run",
            impact="n/a",
        )
        persistence = AsyncMock()
        persistence.get_decisions = AsyncMock(return_value=[persisted_decision])
        context = Context(persistence=persistence)

        task_context = await context.get_context("task_123", ["dep_task"])

        assert len(task_context.architectural_decisions) == 1
        assert (
            task_context.architectural_decisions[0]["what"] == "Persisted decision"
        )


class TestSweepContextRetention:
    """Test suite for sweep_context_retention, the periodic-sweep entry
    point that prunes every Context a MarcusServer holds."""

    @pytest.mark.asyncio
    async def test_sweeps_the_global_context(self):
        global_context = AsyncMock()
        server = SimpleNamespace(context=global_context, project_manager=None)

        swept = await sweep_context_retention(server, days=30)

        global_context.clear_old_data.assert_awaited_once_with(days=30)
        assert swept == 1

    @pytest.mark.asyncio
    async def test_sweeps_every_project_context(self):
        project_context_1 = AsyncMock()
        project_context_2 = AsyncMock()
        project_manager = SimpleNamespace(
            contexts={
                "p1": SimpleNamespace(context=project_context_1),
                "p2": SimpleNamespace(context=project_context_2),
            }
        )
        server = SimpleNamespace(context=None, project_manager=project_manager)

        swept = await sweep_context_retention(server, days=14)

        project_context_1.clear_old_data.assert_awaited_once_with(days=14)
        project_context_2.clear_old_data.assert_awaited_once_with(days=14)
        assert swept == 2

    @pytest.mark.asyncio
    async def test_dedups_when_global_and_project_context_are_the_same_object(self):
        """server.context and a per-project ProjectContext.context can be
        the SAME Context instance (set_global_context aliases them) — the
        sweep must not call clear_old_data on it twice."""
        shared_context = AsyncMock()
        project_manager = SimpleNamespace(
            contexts={"p1": SimpleNamespace(context=shared_context)}
        )
        server = SimpleNamespace(context=shared_context, project_manager=project_manager)

        swept = await sweep_context_retention(server)

        shared_context.clear_old_data.assert_awaited_once()
        assert swept == 1

    @pytest.mark.asyncio
    async def test_handles_missing_attributes_gracefully(self):
        """A server-like object missing context/project_manager entirely
        (e.g. a bare test double) must not raise."""
        server = SimpleNamespace()

        swept = await sweep_context_retention(server)

        assert swept == 0

    @pytest.mark.asyncio
    async def test_handles_none_project_context_values_gracefully(self):
        project_manager = SimpleNamespace(
            contexts={"p1": SimpleNamespace(context=None)}
        )
        server = SimpleNamespace(context=None, project_manager=project_manager)

        swept = await sweep_context_retention(server)

        assert swept == 0

    @pytest.mark.asyncio
    async def test_default_retention_days_is_30(self):
        global_context = AsyncMock()
        server = SimpleNamespace(context=global_context, project_manager=None)

        await sweep_context_retention(server)

        global_context.clear_old_data.assert_awaited_once_with(days=30)
