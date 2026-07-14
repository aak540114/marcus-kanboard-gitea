"""
Bearer-token authentication for Marcus's HTTP endpoints.

When Marcus is exposed beyond localhost (see ``MARCUS_BIND_HOST`` in
``docker-compose.yml``), its HTTP surface — the MCP control plane an AI
agent uses to pull tasks and push code, plus the gate/description/dev-env
API routes — must not be reachable by unaccounted ("rogue") agents. This
module provides a small ASGI middleware that requires every request to
carry a shared secret as an ``Authorization: Bearer <token>`` header, so
only agents configured with the token can connect.

Design notes
------------
- **Pure ASGI, not ``BaseHTTPMiddleware``.** The MCP endpoint uses
  streamable HTTP (long-lived / streaming responses); Starlette's
  ``BaseHTTPMiddleware`` buffers the response body and breaks streaming.
  This middleware only inspects request headers and either rejects with a
  401 or passes the request through to the wrapped app untouched, so
  streaming is preserved.
- **Constant-time comparison** (``secrets.compare_digest``) so a
  timing side-channel can't be used to recover the token byte by byte.
- **Enabled iff a token is configured.** With no ``MARCUS_AGENT_TOKEN``
  set, the middleware is a transparent pass-through — this preserves the
  frictionless localhost-only default. ``scripts/setup.sh`` generates and
  sets the token when the operator opts into remote access.
- **Exempt paths.** The Kanboard webhook (``/webhooks/kanboard``) carries
  its *own* ``?token=`` secret (Kanboard sends it, not a Bearer header),
  validated separately in ``kanboard_webhook_receiver.py`` — so it is
  exempt from bearer auth rather than double-gated with a header Kanboard
  never sends.

Classes
-------
BearerAuthMiddleware
    ASGI middleware enforcing an ``Authorization: Bearer <token>`` header.

Functions
---------
get_agent_token
    Read the configured agent token from the environment (or return None).

Examples
--------
>>> app = BearerAuthMiddleware(inner_app, token="secret", exempt_paths=set())
"""

import json
import secrets
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

# ASGI type aliases (kept local to avoid a hard dependency on a types pkg).
Scope = Dict[str, Any]
Message = Dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
# Loose form so any framework's ASGI app (e.g. a Starlette instance, whose
# __call__ uses its own scope/receive/send types) structurally matches at
# this interop boundary without a cast.
ASGIApp = Callable[..., Awaitable[None]]

_DEFAULT_EXEMPT_PATHS = frozenset({"/webhooks/kanboard", "/webhooks/gitea"})


def get_agent_token() -> Optional[str]:
    """Return the configured Marcus agent token, or ``None`` if unset.

    Reads ``MARCUS_AGENT_TOKEN`` from the environment. A blank/whitespace
    value is treated as unset (auth disabled), matching how the other
    Marcus secrets behave when their env var is present but empty.

    Returns
    -------
    Optional[str]
        The token, stripped of surrounding whitespace, or ``None`` when
        the variable is unset or empty.
    """
    import os

    raw = os.getenv("MARCUS_AGENT_TOKEN", "").strip()
    return raw or None


class BearerAuthMiddleware:
    """ASGI middleware requiring an ``Authorization: Bearer <token>`` header.

    Parameters
    ----------
    app : ASGIApp
        The wrapped ASGI application (the Marcus Starlette app).
    token : Optional[str]
        The shared secret every request must present. When ``None`` or
        empty, the middleware is a transparent pass-through (auth
        disabled) — preserving the localhost-only default behavior.
    exempt_paths : Optional[Iterable[str]]
        Request paths that bypass bearer auth (they authenticate by other
        means). Defaults to ``{"/webhooks/kanboard"}``. Matching is exact
        on the ASGI ``path``.

    Notes
    -----
    Non-HTTP scopes (``lifespan``, ``websocket``) are passed straight
    through — this guards HTTP requests only.
    """

    def __init__(
        self,
        app: ASGIApp,
        token: Optional[str],
        exempt_paths: Optional[Iterable[str]] = None,
    ) -> None:
        """Initialize the middleware; see class docstring for parameters."""
        self.app = app
        self.token = token or None
        self.exempt_paths = (
            frozenset(exempt_paths)
            if exempt_paths is not None
            else _DEFAULT_EXEMPT_PATHS
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Enforce the bearer token for HTTP requests, else pass through."""
        # Auth disabled (no token) or non-HTTP scope: transparent.
        if self.token is None or scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        if self._authorized(scope):
            await self.app(scope, receive, send)
            return

        await self._reject(send)

    def _authorized(self, scope: Scope) -> bool:
        """Return True iff the request carries the correct bearer token."""
        provided = self._extract_bearer(scope)
        expected = self.token
        if provided is None or expected is None:
            return False
        # Compare as bytes, not str: secrets.compare_digest() raises
        # TypeError on a str containing non-ASCII characters, and the
        # attacker controls `provided` (it comes from a latin-1 decode of
        # the raw header, so bytes 0x80-0xFF become non-ASCII code points).
        # Comparing UTF-8 bytes never raises on content, so a hostile
        # `Authorization: Bearer \xff\xff` cleanly fails (401) instead of
        # crashing the request with a 500. Valid tokens are ASCII (hex), so
        # the encode round-trips identically on both sides.
        return secrets.compare_digest(
            provided.encode("utf-8"), expected.encode("utf-8")
        )

    @staticmethod
    def _extract_bearer(scope: Scope) -> Optional[str]:
        """Pull the token out of the ``Authorization: Bearer <token>`` header.

        ASGI headers are a list of ``(name, value)`` byte-string tuples;
        header names are lower-cased by the server.
        """
        headers: List[Tuple[bytes, bytes]] = scope.get("headers", [])
        for name, value in headers:
            if name == b"authorization":
                try:
                    decoded = value.decode("latin-1")
                except Exception:
                    return None
                parts = decoded.split(" ", 1)
                if len(parts) == 2 and parts[0].lower() == "bearer":
                    return parts[1].strip()
                return None
        return None

    async def _reject(self, send: Send) -> None:
        """Send a 401 JSON response with a ``WWW-Authenticate`` challenge."""
        body = json.dumps(
            {
                "error": "unauthorized",
                "detail": (
                    "Missing or invalid bearer token. Connect with "
                    "Authorization: Bearer <MARCUS_AGENT_TOKEN>."
                ),
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="marcus"'),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
