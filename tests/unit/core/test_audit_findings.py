"""
Unit tests for src/core/audit_findings.py

Background: an audit ticket (HumanGatedWorkflow.create_audit_ticket) is
instructed to flag each independently-verified bug it finds by posting a
"### 🔍 Audit Finding: <title>" comment via post_ticket_progress, with the
bug explanation, reproduction steps, and proposed fix as the body. When
the audit ticket signals ready for review, Marcus scans its comments for
these entries and turns each into its own child ticket.
"""

import pytest

from src.core.audit_findings import AuditFinding, extract_findings_from_comment

pytestmark = pytest.mark.unit


class TestExtractFindingsFromComment:
    def test_extracts_title_and_body(self):
        content = (
            "### Marcus Agent — Progress Update\n\n"
            "### 🔍 Audit Finding: Off-by-one in pagination\n"
            "**Bug:** The last page is silently dropped.\n"
            "**How to reproduce:** Create 11 items with page size 10, "
            "request page 2, observe it's empty.\n"
            "**Proposed fix:** Use `ceil(total / page_size)` for the "
            "page count.\n"
            "\n\n---\n"
            "*Posted automatically by Marcus AI agent. Reply to this "
            "ticket to interact with the agent.*"
        )
        findings = extract_findings_from_comment(content)
        assert len(findings) == 1
        assert findings[0].title == "Off-by-one in pagination"
        assert "last page is silently dropped" in findings[0].body
        assert "ceil(total / page_size)" in findings[0].body

    def test_extracts_finding_with_no_trailing_footer(self):
        content = (
            "### 🔍 Audit Finding: Race in cache invalidation\n"
            "Two writers can both pass the staleness check before either "
            "writes, so the cache serves stale data indefinitely."
        )
        findings = extract_findings_from_comment(content)
        assert len(findings) == 1
        assert findings[0].title == "Race in cache invalidation"

    def test_case_insensitive_on_audit_finding_words(self):
        content = "### 🔍 audit FINDING: Something\nDetails here."
        findings = extract_findings_from_comment(content)
        assert len(findings) == 1
        assert findings[0].title == "Something"

    def test_no_finding_returns_empty_list(self):
        content = (
            "### Marcus Agent — Progress Update\n\n"
            "**Progress:** [███░░░░░░░] 30%\n"
            "Reviewed the auth module, found nothing so far.\n"
        )
        assert extract_findings_from_comment(content) == []

    def test_empty_content_returns_empty_list(self):
        assert extract_findings_from_comment("") == []
        assert extract_findings_from_comment(None) == []

    def test_two_findings_in_one_comment_are_split_not_merged(self):
        """Regression guard for the same failure mode decision_notes.py's
        _NOTE_RE already guards against: without a non-consuming
        lookahead, two findings in one post_ticket_progress call merge
        into a single match spanning both, embedding a stray heading
        fragment inside the first finding's captured body."""
        content = (
            "### 🔍 Audit Finding: First bug\n"
            "Body of the first finding.\n\n"
            "### 🔍 Audit Finding: Second bug\n"
            "Body of the second finding.\n"
            "\n\n---\n"
            "*Posted automatically by Marcus AI agent. Reply to this "
            "ticket to interact with the agent.*"
        )
        findings = extract_findings_from_comment(content)
        assert len(findings) == 2
        assert findings[0].title == "First bug"
        assert "Second bug" not in findings[0].body
        assert findings[1].title == "Second bug"
        assert "Body of the second finding." in findings[1].body

    def test_heading_with_no_body_is_not_reported_as_a_finding(self):
        """A bare heading with nothing after it isn't a real finding —
        require a non-empty body so a malformed/truncated post never
        spawns an empty child ticket."""
        content = "### 🔍 Audit Finding: Nothing here\n"
        assert extract_findings_from_comment(content) == []

    def test_returns_auditfinding_dataclass_instances(self):
        content = "### 🔍 Audit Finding: X\nY"
        findings = extract_findings_from_comment(content)
        assert isinstance(findings[0], AuditFinding)
