"""
Unit tests for src/core/decision_notes.py

Background: agents are prompted (build_tiered_instructions Layer 1.25 in
src/marcus_mcp/tools/task.py) to post a "🏗️ Note: ..." comment via
post_ticket_progress whenever they make an implementation choice worth
flagging to a human. This module scans ticket comments for that prefix
and compiles them into the Decisions Log shown on the /project-description
page's Decisions tab.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.decision_notes import (
    _normalize_comment_date,
    extract_notes_from_comment,
    get_project_decision_notes,
)

pytestmark = pytest.mark.unit


# ── extract_notes_from_comment ──────────────────────────────────────────────

class TestExtractNotesFromComment:
    def test_extracts_note_from_full_progress_comment(self):
        """The typical shape: a post_ticket_progress comment built by
        CommentFormatter.progress, with the note as the message body."""
        content = (
            "### Marcus Agent — Progress Update\n\n"
            "**Progress:** [███░░░░░░░] 30%\n"
            "**Branch:** `ticket/kanboard/42`\n\n"
            "🏗️ Note: Chose httpx over requests because we need async "
            "support.\n"
            "\n\n---\n"
            "*Posted automatically by Marcus AI agent. Reply to this "
            "ticket to interact with the agent.*"
        )
        notes = extract_notes_from_comment(content)
        assert notes == [
            "Chose httpx over requests because we need async support."
        ]

    def test_extracts_note_with_no_trailing_footer(self):
        """A hand-typed or freeform comment with no CommentFormatter footer."""
        content = "🏗️ Note: Used the repository pattern for data access."
        notes = extract_notes_from_comment(content)
        assert notes == ["Used the repository pattern for data access."]

    def test_case_insensitive_on_note_word(self):
        content = "🏗️ NOTE: Picked Redis for the cache layer."
        notes = extract_notes_from_comment(content)
        assert notes == ["Picked Redis for the cache layer."]

    def test_no_note_returns_empty_list(self):
        content = (
            "### Marcus Agent — Progress Update\n\n"
            "**Progress:** [█████░░░░░] 50%\n"
            "**Branch:** `ticket/kanboard/7`\n\n"
            "Halfway done, no blockers.\n\n---\n"
            "*Posted automatically by Marcus AI agent.*"
        )
        assert extract_notes_from_comment(content) == []

    def test_empty_content_returns_empty_list(self):
        assert extract_notes_from_comment("") == []

    def test_multiple_notes_in_one_comment_all_extracted(self):
        """Not the expected shape, but the regex should still find every
        occurrence rather than stopping at the first."""
        content = (
            "🏗️ Note: Chose Postgres for ACID guarantees.\n\n---\n"
            "*Posted automatically by Marcus AI agent.*"
        )
        content_two_notes = content + "\n\n🏗️ Note: Also added a retry wrapper."
        notes = extract_notes_from_comment(content_two_notes)
        assert "Chose Postgres for ACID guarantees." in notes
        assert "Also added a retry wrapper." in notes

    def test_whitespace_only_note_text_not_included(self):
        content = "🏗️ Note:    \n\n---\n*footer*"
        assert extract_notes_from_comment(content) == []

    def test_two_notes_in_a_single_progress_comment_are_split_not_merged(self):
        """Regression: nothing stops an agent from flagging two decisions
        in ONE post_ticket_progress call, e.g. message="🏗️ Note: A.\\n\\n
        🏗️ Note: B.". Before this fix, the lazy capture only stopped at
        the comment's single trailing "\\n\\n---" footer (CommentFormatter
        appends it once, at the very end) — so both notes matched as ONE
        blob, with a stray "🏗️ Note:" fragment embedded inside the first
        note's text instead of being split into two clean entries."""
        content = (
            "### Marcus Agent — Progress Update\n\n"
            "**Progress:** [███░░░░░░░] 30%\n"
            "**Branch:** `ticket/kanboard/42`\n\n"
            "🏗️ Note: Chose httpx over requests for async support.\n\n"
            "🏗️ Note: Also added a retry wrapper around the API client.\n"
            "\n\n---\n"
            "*Posted automatically by Marcus AI agent. Reply to this "
            "ticket to interact with the agent.*"
        )
        notes = extract_notes_from_comment(content)
        assert notes == [
            "Chose httpx over requests for async support.",
            "Also added a retry wrapper around the API client.",
        ]
        # Neither note's text should contain a leftover marker from the
        # other — proof they were actually split, not just both present
        # somewhere in one merged string.
        for note in notes:
            assert "🏗️" not in note


# ── get_project_decision_notes ──────────────────────────────────────────────

def _task(task_id: str, project_id: str, name: str) -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.project_id = project_id
    t.name = name
    return t


class TestGetProjectDecisionNotes:
    @pytest.mark.asyncio
    async def test_filters_to_the_requested_project_only(self):
        kanban = MagicMock()
        kanban.get_all_tasks = AsyncMock(
            return_value=[
                _task("1", "7", "Ticket in project 7"),
                _task("2", "9", "Ticket in a DIFFERENT project"),
            ]
        )

        async def get_comments(task_id):
            if task_id == "1":
                return [{"content": "🏗️ Note: In-scope note.", "author": "alice", "date": "2026-01-01"}]
            return [{"content": "🏗️ Note: Out-of-scope note.", "author": "bob", "date": "2026-01-02"}]

        kanban.get_comments = AsyncMock(side_effect=get_comments)

        result = await get_project_decision_notes(kanban, project_id=7)

        assert len(result) == 1
        assert result[0]["note"] == "In-scope note."
        assert result[0]["ticket_id"] == "1"
        assert result[0]["ticket_title"] == "Ticket in project 7"

    @pytest.mark.asyncio
    async def test_aggregates_notes_across_multiple_tickets(self):
        kanban = MagicMock()
        kanban.get_all_tasks = AsyncMock(
            return_value=[
                _task("1", "7", "Ticket A"),
                _task("2", "7", "Ticket B"),
            ]
        )

        async def get_comments(task_id):
            return {
                "1": [{"content": "🏗️ Note: Note from A.", "author": "alice", "date": "2026-01-01"}],
                "2": [{"content": "🏗️ Note: Note from B.", "author": "bob", "date": "2026-01-02"}],
            }[task_id]

        kanban.get_comments = AsyncMock(side_effect=get_comments)

        result = await get_project_decision_notes(kanban, project_id=7)
        notes_text = {n["note"] for n in result}
        assert notes_text == {"Note from A.", "Note from B."}

    @pytest.mark.asyncio
    async def test_sorted_newest_first_by_date(self):
        kanban = MagicMock()
        kanban.get_all_tasks = AsyncMock(return_value=[_task("1", "7", "Ticket")])
        kanban.get_comments = AsyncMock(
            return_value=[
                {"content": "🏗️ Note: Older.", "author": "a", "date": "2026-01-01T00:00:00"},
                {"content": "🏗️ Note: Newer.", "author": "a", "date": "2026-01-02T00:00:00"},
            ]
        )

        result = await get_project_decision_notes(kanban, project_id=7)
        assert [n["note"] for n in result] == ["Newer.", "Older."]

    @pytest.mark.asyncio
    async def test_notes_without_a_date_sort_last(self):
        kanban = MagicMock()
        kanban.get_all_tasks = AsyncMock(return_value=[_task("1", "7", "Ticket")])
        kanban.get_comments = AsyncMock(
            return_value=[
                {"content": "🏗️ Note: No date.", "author": "a", "date": None},
                {"content": "🏗️ Note: Has date.", "author": "a", "date": "2026-01-01"},
            ]
        )

        result = await get_project_decision_notes(kanban, project_id=7)
        assert [n["note"] for n in result] == ["Has date.", "No date."]

    @pytest.mark.asyncio
    async def test_no_tasks_returns_empty_list(self):
        kanban = MagicMock()
        kanban.get_all_tasks = AsyncMock(return_value=[])
        kanban.get_comments = AsyncMock(return_value=[])

        result = await get_project_decision_notes(kanban, project_id=7)
        assert result == []

    @pytest.mark.asyncio
    async def test_tickets_with_no_flagged_notes_contribute_nothing(self):
        kanban = MagicMock()
        kanban.get_all_tasks = AsyncMock(return_value=[_task("1", "7", "Ticket")])
        kanban.get_comments = AsyncMock(
            return_value=[{"content": "Just a regular progress update.", "author": "a", "date": "2026-01-01"}]
        )

        result = await get_project_decision_notes(kanban, project_id=7)
        assert result == []

    @pytest.mark.asyncio
    async def test_does_not_crash_on_real_kanboard_shaped_dates(self):
        """Regression: KanboardKanban.get_comments() actually returns
        "date" as a raw Unix-epoch INT (confirmed by
        TestGetComments.test_normalizes_comment_fields in
        tests/unit/integrations/test_kanboard_kanban.py:
        {"date": 1700000001}), not an ISO string as get_comments()'s own
        docstring claims. Sorting a mix of int dates and the "" fallback
        used for a missing date previously raised
        "TypeError: '<' not supported between instances of 'int' and 'str'"
        inside get_project_decision_notes, which project_decisions_api's
        try/except silently swallowed — showing an EMPTY Decisions tab
        even though real notes existed. Must not raise, and must return
        every note."""
        kanban = MagicMock()
        kanban.get_all_tasks = AsyncMock(
            return_value=[
                _task("1", "7", "Ticket A"),
                _task("2", "7", "Ticket B"),
            ]
        )

        async def get_comments(task_id):
            if task_id == "1":
                return [
                    {
                        "content": "🏗️ Note: Has a real Kanboard timestamp.",
                        "author": "a",
                        "date": 1700000001,
                    }
                ]
            return [
                {
                    "content": "🏗️ Note: Missing timestamp.",
                    "author": "b",
                    "date": None,
                }
            ]

        kanban.get_comments = AsyncMock(side_effect=get_comments)

        result = await get_project_decision_notes(kanban, project_id=7)
        notes_text = {n["note"] for n in result}
        assert notes_text == {
            "Has a real Kanboard timestamp.",
            "Missing timestamp.",
        }

    @pytest.mark.asyncio
    async def test_raw_epoch_date_is_normalized_to_readable_iso_string(self):
        """The Decisions tab UI must show a human-readable date, not a
        raw Unix epoch integer like 1700000001."""
        kanban = MagicMock()
        kanban.get_all_tasks = AsyncMock(return_value=[_task("1", "7", "Ticket")])
        kanban.get_comments = AsyncMock(
            return_value=[
                {"content": "🏗️ Note: Timestamped.", "author": "a", "date": 1700000001}
            ]
        )

        result = await get_project_decision_notes(kanban, project_id=7)
        assert result[0]["date"] == "2023-11-14T22:13:21+00:00"


class TestNormalizeCommentDate:
    """Unit tests for _normalize_comment_date, the helper that converts
    Kanboard's raw Unix-epoch comment timestamps into sortable,
    displayable ISO 8601 strings."""

    def test_converts_int_epoch_to_iso_string(self):
        assert _normalize_comment_date(1700000001) == "2023-11-14T22:13:21+00:00"

    def test_converts_numeric_string_epoch_to_iso_string(self):
        assert _normalize_comment_date("1700000001") == "2023-11-14T22:13:21+00:00"

    def test_none_returns_none(self):
        assert _normalize_comment_date(None) is None

    def test_zero_int_returns_none(self):
        """Kanboard's convention for "unset" (see _parse_kanboard_ts in
        src/integrations/providers/kanboard_kanban.py)."""
        assert _normalize_comment_date(0) is None

    def test_zero_string_returns_none(self):
        assert _normalize_comment_date("0") is None

    def test_empty_string_returns_none(self):
        assert _normalize_comment_date("") is None

    def test_non_numeric_string_passes_through_unchanged(self):
        """A provider that already returns an ISO string (or any other
        non-numeric date format) must not be mangled."""
        assert _normalize_comment_date("2026-01-01T00:00:00") == "2026-01-01T00:00:00"
