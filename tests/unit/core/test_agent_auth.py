"""
Unit tests for the bearer-token authentication middleware.

Verifies that Marcus's HTTP surface rejects requests without a valid
``Authorization: Bearer <token>`` header when a token is configured, stays
open when no token is set (localhost default), and never gates exempt
paths (the Kanboard webhook, which authenticates by its own ``?token=``).
"""

import os
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

import pytest

from src.core.agent_auth import BearerAuthMiddleware, get_agent_token

pytestmark = pytest.mark.unit


def _http_scope(path: str = "/mcp", auth: str | None = None) -> Dict[str, Any]:
    """Build a minimal ASGI HTTP scope, optionally with an auth header."""
    headers: List[Tuple[bytes, bytes]] = []
    if auth is not None:
        headers.append((b"authorization", auth.encode("latin-1")))
    return {"type": "http", "path": path, "headers": headers}


class _Recorder:
    """A stand-in downstream ASGI app that records whether it was called."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.called = True


async def _drain_send() -> Tuple[List[Dict[str, Any]], Any]:
    """Return a (messages, send) pair capturing what the middleware sends."""
    messages: List[Dict[str, Any]] = []

    async def send(message: Dict[str, Any]) -> None:
        messages.append(message)

    return messages, send


async def _noop_receive() -> Dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


class TestGetAgentToken:
    """get_agent_token() reads MARCUS_AGENT_TOKEN, treating blank as unset."""

    def test_returns_none_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert get_agent_token() is None

    def test_returns_none_when_blank(self) -> None:
        with patch.dict(os.environ, {"MARCUS_AGENT_TOKEN": "   "}, clear=True):
            assert get_agent_token() is None

    def test_returns_stripped_token(self) -> None:
        with patch.dict(os.environ, {"MARCUS_AGENT_TOKEN": "  secret  "}, clear=True):
            assert get_agent_token() == "secret"


@pytest.mark.asyncio
class TestBearerAuthMiddleware:
    """Enforcement behavior of the bearer-token middleware."""

    async def test_passthrough_when_no_token_configured(self) -> None:
        """Auth disabled (token=None) → every request passes through."""
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token=None)
        messages, send = await _drain_send()

        await mw(_http_scope(auth=None), _noop_receive, send)

        assert inner.called is True
        assert messages == []  # middleware sent nothing itself

    async def test_rejects_request_with_no_auth_header(self) -> None:
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(_http_scope(auth=None), _noop_receive, send)

        assert inner.called is False
        assert messages[0]["status"] == 401

    async def test_rejects_wrong_token(self) -> None:
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(_http_scope(auth="Bearer wrong"), _noop_receive, send)

        assert inner.called is False
        assert messages[0]["status"] == 401

    async def test_rejects_non_bearer_scheme(self) -> None:
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(_http_scope(auth="Basic secret"), _noop_receive, send)

        assert inner.called is False
        assert messages[0]["status"] == 401

    async def test_allows_correct_token(self) -> None:
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(_http_scope(auth="Bearer secret"), _noop_receive, send)

        assert inner.called is True
        assert messages == []

    async def test_bearer_scheme_is_case_insensitive(self) -> None:
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(_http_scope(auth="bearer secret"), _noop_receive, send)

        assert inner.called is True

    async def test_webhook_path_is_exempt(self) -> None:
        """The Kanboard webhook authenticates by its own ?token=, so the
        bearer middleware must let it through even with no header."""
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(
            _http_scope(path="/webhooks/kanboard", auth=None), _noop_receive, send
        )

        assert inner.called is True
        assert messages == []

    async def test_non_http_scope_passes_through(self) -> None:
        """lifespan/websocket scopes are not gated."""
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw({"type": "lifespan"}, _noop_receive, send)

        assert inner.called is True

    async def test_protected_api_route_requires_token(self) -> None:
        """A state-mutating API route (gate flip) must be gated too, not
        just the MCP mount."""
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(
            _http_scope(path="/api/gate-setting/project", auth=None),
            _noop_receive,
            send,
        )

        assert inner.called is False
        assert messages[0]["status"] == 401
