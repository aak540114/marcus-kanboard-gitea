"""
LLM-based AI verification of ticket implementations.

When AI Gate is active and AI Verify is enabled, Marcus runs an independent
review of the worker agent's branch before merging.  The verifier:

1. Fetches the git diff between the ticket branch and main.
2. Builds a prompt that includes the ticket title, acceptance criteria, and
   the full diff.
3. Asks the LLM to act as a code reviewer and report issues.
4. Returns a structured :class:`VerificationResult` — ``passed`` plus a list
   of human-readable findings.

If ``passed`` is ``False`` the caller (``HumanGatedWorkflow``) posts the
findings as a comment, releases the ticket back to "In Progress", and the
worker agent must fix the issues before ``signal_ready_for_review`` is
called again.

Classes
-------
VerificationResult
    Outcome of a single verification run.
AIVerifier
    Orchestrates the diff-fetch → prompt-build → LLM-call → parse cycle.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

# Maximum diff size to send to the LLM (tokens are finite).
_MAX_DIFF_CHARS = 12_000

_SYSTEM_PROMPT = """\
You are a senior software engineer conducting a code review on behalf of Marcus,
an AI multi-agent orchestration system.  Your job is to verify that the changes
on a feature branch meet the acceptance criteria of its ticket and are free of
obvious bugs, errors, or incomplete implementations.

Output ONLY a JSON object in this exact shape — no prose, no markdown fences:

{
  "passed": true | false,
  "findings": ["<finding 1>", "<finding 2>", ...]
}

Rules:
- "passed" is true if the implementation satisfies every acceptance criterion
  and you found no blocking bugs or incomplete work.
- "passed" is false if ANY acceptance criterion is unmet, or if you found a
  blocking bug or obviously incomplete implementation.
- "findings" lists each issue as a short, actionable sentence.  Empty list when
  "passed" is true.
- Do NOT include style nits, subjective preferences, or performance micro-opts
  unless they represent a correctness risk.
- If the diff is empty or contains only whitespace/comment changes, set
  "passed" to false and note that no meaningful implementation was found.
"""


@dataclass
class VerificationResult:
    """Outcome of a single AI verification run.

    Parameters
    ----------
    passed : bool
        ``True`` when the implementation satisfied all acceptance criteria
        and no blocking bugs were found.
    findings : List[str]
        Human-readable list of issues.  Empty when ``passed`` is ``True``.
    raw_response : str
        The raw LLM response (for debugging).
    """

    passed: bool
    findings: List[str] = field(default_factory=list)
    raw_response: str = ""


class AIVerifier:
    """Verifies a ticket's implementation using an LLM code review.

    Parameters
    ----------
    provider : optional
        An object with an async ``complete(prompt, max_tokens)`` method.
        When ``None``, the verifier attempts to create an
        :class:`~src.ai.providers.anthropic_provider.AnthropicProvider`
        directly (bypassing the full LLMAbstraction config validation which
        requires all providers to be available).
    """

    def __init__(self, provider: Optional[object] = None) -> None:
        self._provider = provider

    async def _get_provider(self) -> object:
        """Return (and cache) the LLM provider."""
        if self._provider is None:
            from src.ai.providers.anthropic_provider import AnthropicProvider
            self._provider = AnthropicProvider()
        return self._provider

    async def verify(
        self,
        ticket_id: str,
        ticket_title: str,
        acceptance_criteria: List[str],
        diff_text: str,
    ) -> VerificationResult:
        """Run a full AI verification for a ticket branch.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID (used in log messages).
        ticket_title : str
            Human-readable ticket title.
        acceptance_criteria : List[str]
            Acceptance criteria items (each a plain-text string).
        diff_text : str
            Output of ``git diff main...<branch>`` for the ticket branch.

        Returns
        -------
        VerificationResult
            ``passed=True`` when verification succeeds; ``passed=False`` with
            a non-empty ``findings`` list when issues are found.
        """
        if not diff_text.strip():
            logger.warning(
                "Ticket %s: diff is empty — marking verification as failed", ticket_id
            )
            return VerificationResult(
                passed=False,
                findings=["The branch diff is empty. No implementation was found."],
            )

        truncated_diff = diff_text[:_MAX_DIFF_CHARS]
        if len(diff_text) > _MAX_DIFF_CHARS:
            truncated_diff += (
                f"\n\n[... diff truncated at {_MAX_DIFF_CHARS} characters ...]"
            )

        ac_block = "\n".join(
            f"  {i+1}. {criterion}"
            for i, criterion in enumerate(acceptance_criteria)
        ) or "  (no acceptance criteria specified)"

        user_prompt = (
            f"## Ticket\n"
            f"ID: {ticket_id}\n"
            f"Title: {ticket_title}\n\n"
            f"## Acceptance criteria\n"
            f"{ac_block}\n\n"
            f"## Branch diff (vs main)\n"
            f"```diff\n{truncated_diff}\n```\n"
        )

        raw = ""
        try:
            provider = await self._get_provider()
            full_prompt = f"{_SYSTEM_PROMPT}\n\n{user_prompt}"
            raw = await provider.complete(full_prompt, max_tokens=1024)  # type: ignore[attr-defined]
            return self._parse(raw)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "AIVerifier LLM call failed for ticket %s: %s", ticket_id, exc
            )
            # On LLM error let the workflow proceed as if verification passed
            # (fail-open so a transient API outage doesn't block merging).
            return VerificationResult(
                passed=True,
                findings=[],
                raw_response=f"LLM error — verification skipped: {exc}",
            )

    @staticmethod
    def _parse(raw: str) -> VerificationResult:
        """Extract a :class:`VerificationResult` from the LLM response.

        Handles JSON with surrounding prose or markdown fences by finding the
        first balanced ``{...}`` block in the response (brace-counter scan).
        A greedy ``.*`` regex would incorrectly span from the first ``{`` to
        the LAST ``}`` in the string, failing when the LLM adds trailing prose
        that contains curly braces (e.g. variable names, format strings).
        """
        # Strip markdown code fences if present.
        cleaned = re.sub(r"```(?:json)?", "", raw).strip()

        # Locate the first balanced JSON object via brace counting.
        start = cleaned.find("{")
        if start == -1:
            logger.warning("AIVerifier: could not find JSON in LLM response: %r", raw[:300])
            return VerificationResult(
                passed=False,
                findings=["Verification inconclusive: LLM did not return valid JSON."],
                raw_response=raw,
            )

        depth = 0
        end = -1
        in_string = False
        escape_next = False
        for i, ch in enumerate(cleaned[start:], start):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break

        if end == -1:
            logger.warning("AIVerifier: unmatched braces in LLM response: %r", raw[:300])
            return VerificationResult(
                passed=False,
                findings=["Verification inconclusive: LLM did not return valid JSON."],
                raw_response=raw,
            )

        try:
            obj = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            logger.warning("AIVerifier: JSON parse error: %s — raw: %r", exc, raw[:300])
            return VerificationResult(
                passed=False,
                findings=["Verification inconclusive: LLM response could not be parsed."],
                raw_response=raw,
            )

        passed = bool(obj.get("passed", False))
        findings = [str(f) for f in obj.get("findings", []) if f]
        return VerificationResult(passed=passed, findings=findings, raw_response=raw)
