"""
Unit tests for src/ai/verification/ai_verifier.py
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ai.verification.ai_verifier import AIVerifier, VerificationResult


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_provider(response: str) -> MagicMock:
    """Return a mock LLM provider whose complete() returns response."""
    provider = MagicMock()
    provider.complete = AsyncMock(return_value=response)
    return provider


def _json_response(passed: bool, findings=None) -> str:
    return json.dumps({"passed": passed, "findings": findings or []})


# ── VerificationResult ─────────────────────────────────────────────────────

class TestVerificationResult:
    """Tests for the VerificationResult dataclass."""

    def test_defaults(self):
        """passed=True, empty findings, empty raw_response."""
        r = VerificationResult(passed=True)
        assert r.passed is True
        assert r.findings == []
        assert r.raw_response == ""

    def test_failed_with_findings(self):
        """passed=False with findings stored correctly."""
        r = VerificationResult(passed=False, findings=["bug 1", "bug 2"])
        assert r.passed is False
        assert len(r.findings) == 2


# ── AIVerifier._parse ──────────────────────────────────────────────────────

class TestAIVerifierParse:
    """Tests for AIVerifier._parse (static, no LLM calls)."""

    def test_parse_passed_true_no_findings(self):
        """Clean JSON → passed=True, empty findings."""
        raw = _json_response(True)
        result = AIVerifier._parse(raw)
        assert result.passed is True
        assert result.findings == []

    def test_parse_passed_false_with_findings(self):
        """Failed JSON → passed=False, findings populated."""
        raw = _json_response(False, ["Missing error handler", "Test not found"])
        result = AIVerifier._parse(raw)
        assert result.passed is False
        assert result.findings == ["Missing error handler", "Test not found"]

    def test_parse_strips_markdown_fence(self):
        """JSON wrapped in ```json ... ``` is still parsed."""
        raw = "```json\n" + _json_response(True) + "\n```"
        result = AIVerifier._parse(raw)
        assert result.passed is True

    def test_parse_extracts_json_from_prose(self):
        """JSON embedded in prose text is extracted correctly."""
        raw = "Here is my review:\n" + _json_response(False, ["thing"]) + "\nDone."
        result = AIVerifier._parse(raw)
        assert result.passed is False
        assert result.findings == ["thing"]

    def test_parse_no_json_returns_failed(self):
        """No JSON object in response → passed=False with a descriptive finding."""
        result = AIVerifier._parse("I cannot determine if this is correct.")
        assert result.passed is False
        assert len(result.findings) == 1
        assert "JSON" in result.findings[0]

    def test_parse_invalid_json_returns_failed(self):
        """Malformed JSON → passed=False with a descriptive finding."""
        result = AIVerifier._parse("{passed: true, findings: }")
        assert result.passed is False
        assert len(result.findings) == 1

    def test_parse_empty_findings_list(self):
        """Empty findings list with passed=True is valid."""
        result = AIVerifier._parse('{"passed": true, "findings": []}')
        assert result.passed is True
        assert result.findings == []

    def test_parse_filters_empty_strings_from_findings(self):
        """Empty strings in findings are stripped."""
        raw = '{"passed": false, "findings": ["real issue", "", "another"]}'
        result = AIVerifier._parse(raw)
        assert result.findings == ["real issue", "another"]

    def test_parse_ignores_trailing_braces_in_prose(self):
        """JSON followed by prose containing {curly braces} is still parsed correctly."""
        json_part = _json_response(True)
        raw = json_part + "\n\nNote: see the {main} branch and {utils.py} for context."
        result = AIVerifier._parse(raw)
        # Greedy regex would have matched to the last } in {utils.py}, failing to parse.
        assert result.passed is True

    def test_parse_json_with_nested_object_in_findings(self):
        """Findings that contain curly braces as text are extracted correctly."""
        raw = '{"passed": false, "findings": ["Use {} format in logger calls"]}'
        result = AIVerifier._parse(raw)
        assert result.passed is False
        assert result.findings == ["Use {} format in logger calls"]


# ── AIVerifier.verify ──────────────────────────────────────────────────────

class TestAIVerifierVerify:
    """Tests for the full AIVerifier.verify() method."""

    @pytest.mark.asyncio
    async def test_verify_passes_when_llm_says_passed(self):
        """LLM response with passed=True → VerificationResult.passed is True."""
        provider = _make_provider(_json_response(True))
        verifier = AIVerifier(provider=provider)

        result = await verifier.verify(
            ticket_id="1",
            ticket_title="Add login button",
            acceptance_criteria=["Button is visible", "Button links to /login"],
            diff_text="+ <button>Login</button>",
        )

        assert result.passed is True
        assert result.findings == []
        provider.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_verify_fails_when_llm_says_failed(self):
        """LLM response with passed=False → VerificationResult carries findings."""
        findings = ["The button is missing the href attribute"]
        provider = _make_provider(_json_response(False, findings))
        verifier = AIVerifier(provider=provider)

        result = await verifier.verify(
            ticket_id="2",
            ticket_title="Fix nav link",
            acceptance_criteria=["Link opens /dashboard"],
            diff_text="+ <a>Dashboard</a>",
        )

        assert result.passed is False
        assert result.findings == findings

    @pytest.mark.asyncio
    async def test_verify_empty_diff_fails_immediately(self):
        """Empty diff → failed without calling the LLM."""
        provider = _make_provider(_json_response(True))
        verifier = AIVerifier(provider=provider)

        result = await verifier.verify(
            ticket_id="3",
            ticket_title="Implement feature",
            acceptance_criteria=["Feature works"],
            diff_text="   ",
        )

        assert result.passed is False
        assert "empty" in result.findings[0].lower()
        provider.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_verify_llm_error_fails_open(self):
        """LLM call raises an exception → passes (fail-open) to avoid blocking."""
        provider = MagicMock()
        provider.complete = AsyncMock(side_effect=RuntimeError("API down"))
        verifier = AIVerifier(provider=provider)

        result = await verifier.verify(
            ticket_id="4",
            ticket_title="Some ticket",
            acceptance_criteria=[],
            diff_text="+ some change",
        )

        assert result.passed is True  # fail-open
        assert "error" in result.raw_response.lower()

    @pytest.mark.asyncio
    async def test_verify_prompt_includes_ticket_title(self):
        """The prompt sent to the LLM contains the ticket title."""
        provider = _make_provider(_json_response(True))
        verifier = AIVerifier(provider=provider)

        await verifier.verify(
            ticket_id="5",
            ticket_title="My Unique Ticket Title",
            acceptance_criteria=[],
            diff_text="+ change",
        )

        call_args = provider.complete.call_args[0][0]
        assert "My Unique Ticket Title" in call_args

    @pytest.mark.asyncio
    async def test_verify_prompt_includes_acceptance_criteria(self):
        """The prompt contains the acceptance criteria."""
        provider = _make_provider(_json_response(True))
        verifier = AIVerifier(provider=provider)

        await verifier.verify(
            ticket_id="6",
            ticket_title="Ticket",
            acceptance_criteria=["Must handle 404", "Must log errors"],
            diff_text="+ change",
        )

        call_args = provider.complete.call_args[0][0]
        assert "Must handle 404" in call_args
        assert "Must log errors" in call_args

    @pytest.mark.asyncio
    async def test_verify_truncates_large_diff(self):
        """Diffs larger than _MAX_DIFF_CHARS are truncated before sending."""
        from src.ai.verification.ai_verifier import _MAX_DIFF_CHARS

        provider = _make_provider(_json_response(True))
        verifier = AIVerifier(provider=provider)
        large_diff = "+" + "x" * (_MAX_DIFF_CHARS + 5000)

        await verifier.verify(
            ticket_id="7",
            ticket_title="Ticket",
            acceptance_criteria=[],
            diff_text=large_diff,
        )

        call_args = provider.complete.call_args[0][0]
        assert "truncated" in call_args

    @pytest.mark.asyncio
    async def test_verify_no_criteria_still_runs(self):
        """Empty acceptance criteria list does not crash the verifier."""
        provider = _make_provider(_json_response(True))
        verifier = AIVerifier(provider=provider)

        result = await verifier.verify(
            ticket_id="8",
            ticket_title="Ticket",
            acceptance_criteria=[],
            diff_text="+ change",
        )

        assert result.passed is True
