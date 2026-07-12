"""
Claude CLI (subscription) LLM Provider for Marcus AI.

Routes Marcus's own internal LLM calls (task decomposition, dependency
inference, effort estimation, blocker analysis) through a locally-installed
``claude`` CLI in non-interactive print mode, instead of a metered
Anthropic API key. Whatever auth the CLI is already logged into — most
commonly a Claude Pro/Max subscription via ``claude login`` — is what gets
used; this provider never reads or sets ``ANTHROPIC_API_KEY`` /
``CLAUDE_API_KEY``, matching the same principle already documented for the
Anthropic provider in ``llm_abstraction.py`` (writing that env var would
force ``claude`` subprocesses to bill an API key instead of using the
subscription).

Classes
-------
ClaudeCliProvider
    Subscription-backed provider that shells out to ``claude -p`` instead
    of calling the Anthropic API directly.

Notes
-----
Requires the ``claude`` CLI installed and authenticated (``claude login``)
on the same machine Marcus itself runs on. This works for any deployment
topology where Marcus's *own* process has that login — Kanboard and Gitea
can live on entirely separate hosts, since Marcus already talks to them
over plain HTTP regardless of where its own LLM calls come from.

Each call spawns a full ``claude`` CLI process (observed latency: several
seconds to tens of seconds per call, versus typical sub-second direct API
latency), because it goes through the full agent harness for a single
completion. Subscription usage limits apply and are shared with any
interactive Claude Code sessions on the same account.

Examples
--------
>>> provider = ClaudeCliProvider(model="sonnet")
>>> analysis = await provider.analyze_task(task, context)
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional

from src.cost_tracking.cost_recorder import get_recorder

from .local_provider import LocalLLMProvider, _strip_reasoning_blocks

logger = logging.getLogger(__name__)


class ClaudeCliProvider(LocalLLMProvider):
    """Subscription-backed LLM provider that shells out to the ``claude`` CLI.

    Inherits all semantic-analysis business logic (prompt building, JSON
    response parsing, fallback behavior on failure) from
    ``LocalLLMProvider`` unchanged — only the transport hook
    (``_call_local_llm``) is overridden, following the same pattern
    ``CloudLLMProvider`` uses to swap in a different backend.

    Parameters
    ----------
    model : Optional[str]
        Model alias to pass via ``claude --model`` (e.g. ``"sonnet"``,
        ``"opus"``, ``"haiku"``). When ``None`` (default), no ``--model``
        flag is passed and the CLI uses its own session default. Not
        wired to ``config.ai.model`` automatically — that field's default
        is an old dated Anthropic API model string (e.g.
        ``"claude-3-haiku-20240307"``), not a valid CLI alias, and passing
        it through would break the CLI invocation for anyone using this
        provider's defaults.
    timeout : float
        Seconds to wait for one ``claude`` CLI invocation before killing
        the subprocess and raising. Default 120s — each call is a full
        CLI process spawn, not a lightweight API request.

    Raises
    ------
    (none at construction — no credentials to validate; a missing or
    unauthenticated ``claude`` binary surfaces as a runtime error on the
    first actual call, handled the same way every other provider's
    transport failures are: caught per-business-method and turned into a
    safe fallback response.)
    """

    def __init__(self, model: Optional[str] = None, timeout: float = 120.0) -> None:
        """Initialize the Claude CLI provider (no HTTP client, no API key)."""
        # Deliberately skip LocalLLMProvider.__init__ — it builds an httpx
        # client pointed at a local server URL, none of which applies here.
        from src.config.marcus_config import get_config

        config = get_config()

        # LocalLLMProvider.model is typed `str` (always required there) —
        # normalize None to "" here to match that type exactly rather than
        # widen it (an empty string is falsy, so `if self.model:` below
        # behaves identically to the Optional[str] version would have).
        self.model: str = model or ""
        self.max_tokens = config.ai.max_tokens
        self.temperature = config.ai.temperature
        self.timeout = timeout

        logger.info(
            "Claude CLI (subscription) provider initialized%s",
            f" with model={model}" if model else " (using CLI session default model)",
        )

    def _cost_provider_name(self) -> str:
        """Return ``'claude_subscription'`` as the cost-event provider tag."""
        return "claude_subscription"

    # Override the internal dispatch every inherited business method
    # (analyze_task, infer_dependencies, estimate_effort, ...) routes
    # through, so they transparently use the CLI instead of an HTTP call.
    async def _call_local_llm(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: float = 0.7,
    ) -> str:
        """Delegate to ``_call_claude_cli``.

        ``max_tokens``/``temperature`` are accepted for interface
        compatibility with the parent class but not forwarded to the CLI —
        ``claude -p`` does not expose sampling-parameter flags; Claude
        Code manages its own generation parameters internally.
        """
        return await self._call_claude_cli(prompt)

    async def _call_claude_cli(self, prompt: str) -> str:
        """Run one non-interactive ``claude -p`` invocation and return its text.

        Parameters
        ----------
        prompt : str
            Prompt to send.

        Returns
        -------
        str
            The CLI's response text, with any leading ``<think>...</think>``
            reasoning-block prefix stripped (reusing the same helper the
            local/cloud providers use).

        Raises
        ------
        RuntimeError
            On subprocess timeout, non-zero exit, non-JSON stdout, or an
            ``is_error`` result in the CLI's JSON envelope.
        """
        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            # No tool access needed or wanted for a text/JSON completion —
            # unlike the coding agents Runner mode spawns, these are pure
            # analysis calls with no file/Bash access.
            "--allowedTools",
            "",
        ]
        if self.model:
            cmd += ["--model", self.model]

        # No env= kwarg: the subprocess inherits this process's environment
        # unmodified. Never inject ANTHROPIC_API_KEY/CLAUDE_API_KEY here —
        # doing so would force this (and any other) claude CLI subprocess
        # to bill a metered API key instead of using whatever the CLI is
        # already logged into.
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        start = time.monotonic()
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"claude CLI timed out after {self.timeout}s"
            ) from None

        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI exited {proc.returncode}: "
                f"{stderr.decode(errors='replace')[:500]}"
            )

        try:
            envelope: Dict[str, Any] = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"claude CLI returned non-JSON output: {exc}"
            ) from exc

        if envelope.get("is_error"):
            raise RuntimeError(f"claude CLI reported an error: {envelope.get('result')}")

        result = envelope.get("result", "")
        if not isinstance(result, str):
            raise RuntimeError(f"Unexpected claude CLI result type: {type(result)}")

        usage = envelope.get("usage") or {}
        get_recorder().record_planner_call(
            operation="analyze",
            provider=self._cost_provider_name(),
            model=self.model or envelope.get("model") or "default",
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0)),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
            latency_ms=int((time.monotonic() - start) * 1000),
            request_id=envelope.get("session_id"),
        )

        return _strip_reasoning_blocks(result)
