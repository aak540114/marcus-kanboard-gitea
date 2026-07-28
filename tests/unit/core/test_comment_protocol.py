"""
Unit tests for src/core/comment_protocol.py
"""

from types import SimpleNamespace

from src.core.comment_protocol import (
    CommentFormatter,
    CommentParser,
    CommentType,
)


class TestCommentFormatter:
    """Tests for CommentFormatter class methods."""

    def test_ac_generated_has_no_visible_html_markers(self):
        """Comments carry NO raw HTML sentinels (Kanboard renders them as
        literal text on the card) — Marcus is recognised by its title."""
        body = CommentFormatter.ac_generated(
            ticket_id="PROJ-1",
            ac_markdown="- [ ] Deploy service",
        )
        assert "<!-- MARCUS_COMMENT" not in body
        assert "<!-- END_MARCUS_COMMENT -->" not in body
        assert CommentParser.is_marcus_comment(body) is True

    def test_ac_generated_type_derived_from_title(self):
        """The comment's type is recoverable from its visible title."""
        body = CommentFormatter.ac_generated("PROJ-1", "- [ ] test")
        parsed = CommentParser.parse(body)
        assert parsed is not None
        assert parsed.comment_type == CommentType.AC_GENERATED

    def test_ac_generated_has_marcus_title(self):
        """Every Marcus comment opens with a '### Marcus …' title."""
        body = CommentFormatter.ac_generated("PROJ-99", "- [ ] test")
        assert "### Marcus" in body

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

    def test_started_without_resumed_commits_has_no_resume_language(self):
        """A genuinely new ticket's comment says nothing about resuming."""
        body = CommentFormatter.started(
            ticket_id="T-5",
            branch_name="b",
            assignee="bob",
        )
        assert "resum" not in body.lower()

    def test_started_with_resumed_commits_flags_prior_work(self):
        """A ticket whose branch already had commits (resumed, not fresh)
        tells the reader there is prior work to review before continuing."""
        body = CommentFormatter.started(
            ticket_id="T-6",
            branch_name="b",
            assignee="bob",
            resumed_commits=["abc1234 add login form", "def5678 wire up auth"],
        )
        low = body.lower()
        assert "resum" in low
        assert "2 commit" in low
        assert "abc1234 add login form" in body
        assert "def5678 wire up auth" in body
        assert "review" in low

    def test_started_resumed_commits_truncated_with_count(self):
        """A long commit history is truncated with an explicit '…and N more'
        rather than dumping every commit into the comment."""
        commits = [f"{i:07x} commit {i}" for i in range(15)]
        body = CommentFormatter.started(
            ticket_id="T-7",
            branch_name="b",
            assignee="bob",
            resumed_commits=commits,
        )
        assert "15 commit" in body.lower()
        assert "and 5 more" in body.lower()
        # Only the first 10 are listed verbatim.
        assert commits[9] in body
        assert commits[10] not in body

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

    def test_every_formatter_output_is_recognised_as_marcus_own(self):
        """EVERY comment the formatter produces must be recognised by
        is_marcus_comment.

        This is the round trip the whole comment protocol rests on.
        _on_comment_added in human_gated_workflow.py uses it as the only
        filter separating Marcus's own comments from a human's, and on a
        ticket in WAITING_FOR_HUMAN any comment that isn't Marcus's is
        treated as human input: the ticket transitions back to
        IN_PROGRESS and the card is moved back to the In Progress column.

        So a formatter whose title this regex fails to match makes Marcus
        respond to itself — it posts 'Ready for Review', moves the card to
        Waiting for Human, reads its own comment back on the next poll and
        drags the card straight back again. The card visibly refuses to
        stay where Marcus put it.

        Parametrised over every public formatter so a newly added comment
        type with an off-pattern title fails here rather than in
        production.
        """
        passed = SimpleNamespace(passed=True, findings=[])
        failed = SimpleNamespace(passed=False, findings=["nope"])
        bodies = [
            CommentFormatter.ac_generated("T-1", "- [ ] x"),
            CommentFormatter.ac_generated("T-1", "- [ ] x", was_human_created=True),
            CommentFormatter.started("T-1", "ticket/kanboard/1", "alice"),
            CommentFormatter.started(
                "T-1", "ticket/kanboard/1", "alice", resumed_commits=["abc pre"]
            ),
            CommentFormatter.progress("T-1", "b", 50, "halfway"),
            CommentFormatter.revision_requested("T-1", "change x", "will do"),
            CommentFormatter.ready_for_review("T-1", "b", ["- [x] done"]),
            CommentFormatter.dev_env_started("T-1", "b", "http://localhost:1", 1),
            CommentFormatter.merged("T-1", "b", "main"),
            CommentFormatter.error("T-1", "boom"),
            CommentFormatter.error("T-1", "boom", needs_human=False),
            CommentFormatter.verification_failed("T-1", ["issue"]),
            # Last round passing, an earlier round passing, and a failure —
            # each takes a different branch and builds a different title.
            CommentFormatter.verification_round_result("T-1", 1, 1, passed),
            CommentFormatter.verification_round_result("T-1", 1, 3, passed),
            CommentFormatter.verification_round_result("T-1", 2, 3, failed),
        ]
        for body in bodies:
            assert CommentParser.is_marcus_comment(body) is True, body[:120]

    def test_recognises_bracket_prefixed_marcus_comments(self):
        """Marcus comments that bypass CommentFormatter must be recognised too.

        Several provider-level comments are built by hand with a
        ``[Marcus …]`` prefix instead of a '### Marcus …' heading — see
        ``KanboardKanban.report_blocker`` ("[Marcus BLOCKER — HIGH]"),
        ``update_task_progress`` ("[Marcus] Progress: 50%") and
        ``assign_task``'s fallback ("[Marcus] Assigned to: …").

        BoardWatcher emits ticket.comment_added for these exactly as for
        any other comment, and _on_comment_added treats anything
        is_marcus_comment rejects as human input: on a ticket in
        WAITING_FOR_HUMAN it transitions back to IN_PROGRESS and moves the
        card back to the In Progress column. report_blocker is the sharp
        case — it moves the card to Blocked and then its own comment drags
        it straight back out again, so the card visibly refuses to stay in
        the column Marcus just put it in.
        """
        assert (
            CommentParser.is_marcus_comment("[Marcus] Progress: 50% | Status: ok")
            is True
        )
        assert (
            CommentParser.is_marcus_comment(
                "[Marcus BLOCKER — HIGH]\n\nCannot reach the database"
            )
            is True
        )
        assert (
            CommentParser.is_marcus_comment("[Marcus] Assigned to: alice") is True
        )

    def test_bracket_prefix_must_start_the_comment(self):
        """A human quoting Marcus mid-comment is still a human comment."""
        assert (
            CommentParser.is_marcus_comment(
                "You wrote [Marcus] Progress: 50% but that looks wrong to me."
            )
            is False
        )

    def test_is_marcus_comment_false_for_human_heading_addressed_to_marcus(self):
        """A human's revision request that happens to open with a Markdown
        heading whose first word is 'Marcus' must NOT be mistaken for
        Marcus's own comment — every real Marcus title is '### Marcus
        Agent — …' or '### Marcus AI Verifier — …', never bare 'Marcus'
        followed directly by something else. Getting this wrong means
        _on_comment_added silently drops the human's revision request
        (treats it as Marcus's own comment and returns early)."""
        body = "# Marcus, please also fix the logout redirect\n\nDetails below."
        assert CommentParser.is_marcus_comment(body) is False

    def test_parse_ac_generated(self):
        """parse() correctly identifies ac_generated comments."""
        body = CommentFormatter.ac_generated("T-2", "- [ ] test")
        parsed = CommentParser.parse(body)
        assert parsed is not None
        assert parsed.comment_type == CommentType.AC_GENERATED
        # ticket_id is no longer embedded in the body (unused in production).
        assert parsed.ticket_id == ""

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
