"""
Unit tests for src/core/comment_protocol.py
"""

import pytest

from src.core.comment_protocol import (
    CommentFormatter,
    CommentParser,
    CommentType,
    ParsedComment,
)


class TestCommentFormatter:
    """Tests for CommentFormatter class methods."""

    def test_ac_generated_contains_sentinel(self):
        """ac_generated comment contains MARCUS_COMMENT sentinel."""
        body = CommentFormatter.ac_generated(
            ticket_id="PROJ-1",
            ac_markdown="- [ ] Deploy service",
        )
        assert "<!-- MARCUS_COMMENT" in body
        assert "<!-- END_MARCUS_COMMENT -->" in body

    def test_ac_generated_type_attribute(self):
        """ac_generated comment has type='ac_generated'."""
        body = CommentFormatter.ac_generated("PROJ-1", "- [ ] test")
        assert 'type="ac_generated"' in body

    def test_ac_generated_ticket_id_attribute(self):
        """ac_generated comment embeds ticket_id."""
        body = CommentFormatter.ac_generated("PROJ-99", "- [ ] test")
        assert 'ticket_id="PROJ-99"' in body

    def test_ac_generated_human_created_note(self):
        """Note is included when was_human_created=True."""
        body = CommentFormatter.ac_generated(
            "T-1", "- [ ] test", was_human_created=True
        )
        assert "created without explicit acceptance criteria" in body.lower()

    def test_started_contains_branch_name(self):
        """started comment shows the git branch name."""
        body = CommentFormatter.started(
            ticket_id="T-2",
            branch_name="ticket/jira/t-2",
            assignee="alice",
        )
        assert "ticket/jira/t-2" in body

    def test_started_contains_assignee(self):
        """started comment mentions the assignee."""
        body = CommentFormatter.started(
            ticket_id="T-2",
            branch_name="ticket/jira/t-2",
            assignee="alice",
        )
        assert "alice" in body

    def test_started_with_ac_items(self):
        """started comment lists AC items when provided."""
        body = CommentFormatter.started(
            ticket_id="T-3",
            branch_name="b",
            assignee="bob",
            ac_items=["Deploy service", "Write tests"],
        )
        assert "Deploy service" in body
        assert "Write tests" in body

    def test_progress_bar_renders(self):
        """progress comment renders a text progress bar."""
        body = CommentFormatter.progress(
            ticket_id="T-4",
            branch_name="b",
            percentage=50,
            message="halfway there",
        )
        assert "50%" in body
        assert "█" in body

    def test_progress_includes_commits(self):
        """progress comment includes commit list when provided."""
        body = CommentFormatter.progress(
            ticket_id="T-5",
            branch_name="b",
            percentage=20,
            message="in progress",
            commits=["abc1234 initial", "def5678 add tests"],
        )
        assert "abc1234" in body

    def test_revision_requested_quotes_human(self):
        """revision_requested quotes the human's comment."""
        body = CommentFormatter.revision_requested(
            ticket_id="T-6",
            human_comment="Please add error handling",
            ai_understanding="I'll add try/except blocks",
        )
        assert "Please add error handling" in body
        assert "I'll add try/except blocks" in body

    def test_ready_for_review_shows_checked_items(self):
        """ready_for_review marks all AC items as checked."""
        body = CommentFormatter.ready_for_review(
            ticket_id="T-7",
            branch_name="ticket/jira/t-7",
            ac_items=["Deploy service", "Write tests"],
        )
        assert "- [x] Deploy service" in body
        assert "- [x] Write tests" in body

    def test_ready_for_review_includes_dev_env_url(self):
        """ready_for_review shows dev env URL when provided."""
        body = CommentFormatter.ready_for_review(
            ticket_id="T-8",
            branch_name="b",
            ac_items=["test"],
            dev_env_url="http://localhost:9100",
        )
        assert "http://localhost:9100" in body

    def test_dev_env_started_shows_url_and_port(self):
        """dev_env_started comment shows URL and port."""
        body = CommentFormatter.dev_env_started(
            ticket_id="T-9",
            branch_name="b",
            url="http://localhost:9200",
            port=9200,
        )
        assert "http://localhost:9200" in body
        assert "9200" in body

    def test_merged_shows_branch_and_main(self):
        """merged comment names both the ticket branch and main branch."""
        body = CommentFormatter.merged(
            ticket_id="T-10",
            branch_name="ticket/jira/t-10",
            main_branch="main",
        )
        assert "ticket/jira/t-10" in body
        assert "main" in body

    def test_error_shows_error_summary(self):
        """error comment includes the error summary."""
        body = CommentFormatter.error(
            ticket_id="T-11",
            error_summary="Merge conflict in src/main.py",
        )
        assert "Merge conflict" in body

    def test_error_with_needs_human_false(self):
        """error comment without human action note."""
        body = CommentFormatter.error(
            ticket_id="T-12",
            error_summary="Minor warning",
            needs_human=False,
        )
        assert "Action needed" not in body


class TestCommentParser:
    """Tests for CommentParser class methods."""

    def test_is_marcus_comment_true(self):
        """is_marcus_comment returns True for Marcus comments."""
        body = CommentFormatter.ac_generated("T-1", "- [ ] test")
        assert CommentParser.is_marcus_comment(body) is True

    def test_is_marcus_comment_false_for_human(self):
        """is_marcus_comment returns False for plain human text."""
        assert CommentParser.is_marcus_comment("Please add more tests.") is False

    def test_parse_ac_generated(self):
        """parse() correctly identifies ac_generated comments."""
        body = CommentFormatter.ac_generated("T-2", "- [ ] test")
        parsed = CommentParser.parse(body)
        assert parsed is not None
        assert parsed.comment_type == CommentType.AC_GENERATED
        assert parsed.ticket_id == "T-2"

    def test_parse_progress(self):
        """parse() correctly identifies progress comments."""
        body = CommentFormatter.progress("T-3", "branch", 40, "halfway")
        parsed = CommentParser.parse(body)
        assert parsed is not None
        assert parsed.comment_type == CommentType.PROGRESS

    def test_parse_ready_for_review(self):
        """parse() correctly identifies ready_for_review comments."""
        body = CommentFormatter.ready_for_review("T-4", "branch", ["Deploy"])
        parsed = CommentParser.parse(body)
        assert parsed is not None
        assert parsed.comment_type == CommentType.READY_FOR_REVIEW

    def test_parse_returns_none_for_human_comment(self):
        """parse() returns None for non-Marcus comments."""
        assert CommentParser.parse("Just a human comment.") is None

    def test_extract_human_instructions_filters_marcus(self):
        """extract_human_instructions excludes Marcus comments."""
        marcus_body = CommentFormatter.progress("T-5", "b", 50, "update")
        comments = [
            {"id": "1", "body": "Please fix the bug", "author": "alice"},
            {"id": "2", "body": marcus_body, "author": "marcus-bot"},
            {"id": "3", "body": "Also add tests", "author": "bob"},
        ]
        human = CommentParser.extract_human_instructions(comments)
        assert len(human) == 2
        ids = [c["id"] for c in human]
        assert "1" in ids
        assert "3" in ids
        assert "2" not in ids

    def test_contains_command_match(self):
        """contains_command detects @marcus commands."""
        assert (
            CommentParser.contains_command(
                "@marcus start-dev-env please", "start-dev-env"
            )
            is True
        )

    def test_contains_command_case_insensitive(self):
        """contains_command is case-insensitive."""
        assert (
            CommentParser.contains_command("@MARCUS Start-Dev-Env", "start-dev-env")
            is True
        )

    def test_contains_command_no_match(self):
        """contains_command returns False when command is absent."""
        assert (
            CommentParser.contains_command("Please start the dev env", "start-dev-env")
            is False
        )

    def test_parse_merged_comment(self):
        """parse() identifies merged comments."""
        body = CommentFormatter.merged("T-6", "ticket/jira/t-6")
        parsed = CommentParser.parse(body)
        assert parsed is not None
        assert parsed.comment_type == CommentType.MERGED

    def test_parse_error_comment(self):
        """parse() identifies error comments."""
        body = CommentFormatter.error("T-7", "Something broke")
        parsed = CommentParser.parse(body)
        assert parsed is not None
        assert parsed.comment_type == CommentType.ERROR
