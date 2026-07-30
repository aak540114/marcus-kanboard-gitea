"""
Unit tests for the SSE stream's shutdown behaviour.

The ``/api/events/stream`` endpoint holds one long-lived connection per open
Kanboard browser tab. Uvicorn's graceful shutdown stops accepting new
connections and then WAITS for the existing ones to close before it runs the
app's lifespan shutdown — and Marcus's lifespan shutdown is what stops
BoardWatcher and ProjectWatcher.

So an SSE generator that loops forever pins the whole process open: Ctrl+C
prints "Waiting for connections to close" and hangs there, while Marcus's
watchers keep polling Kanboard in the background because their shutdown hook
is queued behind a connection that will never end.
"""

from unittest.mock import MagicMock

from src.marcus_mcp.server import _server_is_shutting_down


class TestServerIsShuttingDown:
    """The predicate the SSE loop uses to break out on shutdown."""

    def test_false_before_any_shutdown(self):
        """A normally-serving uvicorn keeps the stream open."""
        server = MagicMock()
        server._uvicorn_server = MagicMock(should_exit=False, force_exit=False)
        assert _server_is_shutting_down(server) is False

    def test_true_once_uvicorn_sets_should_exit(self):
        """uvicorn sets should_exit in its SIGINT handler, BEFORE it starts
        waiting for connections — which is exactly the window in which the
        stream has to notice and let go."""
        server = MagicMock()
        server._uvicorn_server = MagicMock(should_exit=True, force_exit=False)
        assert _server_is_shutting_down(server) is True

    def test_true_on_force_exit(self):
        """A second Ctrl+C (force quit) also ends the stream."""
        server = MagicMock()
        server._uvicorn_server = MagicMock(should_exit=False, force_exit=True)
        assert _server_is_shutting_down(server) is True

    def test_false_when_no_uvicorn_server_attached(self):
        """Non-HTTP modes (stdio) have no uvicorn server; the predicate must
        not crash or spuriously report shutdown."""
        server = MagicMock(spec=[])
        assert _server_is_shutting_down(server) is False
