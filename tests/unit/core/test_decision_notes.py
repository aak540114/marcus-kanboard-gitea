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
