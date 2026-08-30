"""
Extraction of agent-flagged "silent decisions" from ticket comments.

Agents are prompted (see ``build_tiered_instructions`` Layer 1.25 in
``src/marcus_mcp/tools/task.py``) to post a short comment on a ticket
whenever they make an implementation choice worth flagging to a human —
a library pick, a design pattern, a tradeoff — that isn't already spelled
out in the ticket's acceptance criteria. That comment is posted through
the existing ``post_ticket_progress`` MCP tool (already scoped to the
ticket the decision happened on), with the message prefixed
``"🏗️ Note:"``.

This module scans a project's tickets for that prefix and compiles the
results into a flat, human-readable "Decisions Log" — the data behind the
Decisions tab on the ``/project-description`` page
(``project_decisions_api`` in ``src/marcus_mcp/server.py``).

Functions
---------
extract_notes_from_comment
    Pull every "🏗️ Note: ..." entry out of a single comment's raw text.
get_project_decision_notes
    Scan every ticket in a project and compile all flagged notes.
"""

import re
from typing import Any, Dict, List

# Matches "🏗️ Note: <text>", case-insensitive on "Note", capturing
# everything up to the "\n\n---" footer separator that CommentFormatter
# appends (see ``_FOOTER`` in src/core/comment_protocol.py) or the end of
# the comment if there's no footer (e.g. a hand-typed comment).
_NOTE_RE = re.compile(
    r"🏗️\s*note:[ \t]*(.*?)(?:\n\n---|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def extract_notes_from_comment(content: str) -> List[str]:
    """Return every flagged decision note found in a single comment.

    Parameters
    ----------
    content : str
        Raw comment text (as returned by
        ``KanboardKanban.get_comments``' ``"content"`` key).

    Returns
    -------
    List[str]
        Each "🏗️ Note: ..." entry found, trimmed of surrounding
        whitespace, in the order they appear. Empty when the comment has
        no note.
    """
    if not content:
        return []
    notes = []
    for match in _NOTE_RE.finditer(content):
        text = match.group(1).strip()
        if text:
            notes.append(text)
    return notes


async def get_project_decision_notes(
    kanban_client: Any, project_id: int
) -> List[Dict[str, Any]]:
    """Compile every flagged decision note across a project's tickets.

    Parameters
    ----------
    kanban_client : Any
        A connected kanban client exposing ``get_all_tasks()`` and
        ``get_comments(task_id)`` (e.g. ``KanboardKanban``).
    project_id : int
        The project to scan. Tickets belonging to other projects are
        skipped.

    Returns
    -------
    List[Dict[str, Any]]
        One entry per flagged note, each with keys ``ticket_id``,
        ``ticket_title``, ``note``, ``author`` and ``date``. Sorted
        newest first by comment date; notes without a date sort last.
        Empty list if the project has no tickets, no comments, or no
        flagged notes.
    """
    tasks = await kanban_client.get_all_tasks()
    project_id_str = str(project_id)

    notes: List[Dict[str, Any]] = []
    for task in tasks:
        if task.project_id != project_id_str:
            continue
        comments = await kanban_client.get_comments(task.id)
        for comment in comments:
            for note_text in extract_notes_from_comment(comment.get("content", "")):
                notes.append(
                    {
                        "ticket_id": task.id,
                        "ticket_title": task.name,
                        "note": note_text,
                        "author": comment.get("author"),
                        "date": comment.get("date"),
                    }
                )

    notes.sort(key=lambda n: n.get("date") or "", reverse=True)
    return notes
