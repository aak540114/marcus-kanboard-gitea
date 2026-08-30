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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Matches "🏗️ Note: <text>", case-insensitive on "Note", capturing
# everything up to whichever comes first: the "\n\n---" footer separator
# that CommentFormatter appends (see ``_FOOTER`` in
# src/core/comment_protocol.py), the START of another "🏗️ Note:" later in
# the SAME comment, or the end of the comment (no footer, e.g. a
# hand-typed comment). The lookahead (rather than a consuming
# alternation) is what makes the second case work: it stops the capture
# without consuming the next "🏗️ Note:", so finditer's next scan starts
# right there and finds it as its own match — without it, one comment
# containing two notes (nothing stops an agent from flagging two
# decisions in a single post_ticket_progress call) merges into a single
# match spanning both, embedding a stray "🏗️ Note:" fragment inside the
# first note's captured text instead of splitting them.
_NOTE_RE = re.compile(
    r"🏗️\s*note:[ \t]*(.*?)(?=\n\n---|\n\n🏗️\s*note:|\Z)",
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


def _normalize_comment_date(raw: Any) -> Optional[str]:
    """Normalize a comment's raw ``date`` value to a sortable ISO 8601 string.

    ``KanboardKanban.get_comments`` passes ``date_creation`` straight
    through from Kanboard's JSON-RPC response as a raw Unix epoch integer
    (confirmed by ``TestGetComments.test_normalizes_comment_fields`` in
    ``tests/unit/integrations/test_kanboard_kanban.py``, e.g.
    ``{"date": 1700000001}``) despite that method's own docstring
    describing it as "ISO 8601". Sorting or displaying that integer
    directly is wrong: mixing it with ``None`` (falls back to ``""`` in
    the sort key) across different comments raises ``TypeError: '<' not
    supported between instances of 'int' and 'str'`` in
    :func:`get_project_decision_notes`'s sort, and even when it doesn't
    crash, a raw epoch int (e.g. ``1700000001``) is unreadable in the UI.
    ``0``/``"0"`` is Kanboard's convention for "unset" (see
    ``_parse_kanboard_ts`` in
    ``src/integrations/providers/kanboard_kanban.py``) and normalizes to
    ``None`` here too. A non-numeric value (e.g. another provider already
    returning an ISO string) passes through unchanged.

    Parameters
    ----------
    raw : Any
        The raw ``"date"`` value from a comment dict.

    Returns
    -------
    Optional[str]
        A UTC ISO 8601 string, the original string unchanged if it wasn't
        numeric, or ``None`` if there's nothing usable.
    """
    if not raw:
        return None
    if isinstance(raw, (int, float)) or (
        isinstance(raw, str) and raw.lstrip("-").isdigit()
    ):
        try:
            ts = int(raw)
        except (TypeError, ValueError):
            return None
        if ts == 0:
            return None
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    return str(raw)


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
        ``ticket_title``, ``note``, ``author`` and ``date`` (a UTC ISO
        8601 string, normalized via :func:`_normalize_comment_date` from
        Kanboard's raw Unix-epoch comment timestamp — never the raw
        int). Sorted newest first by comment date; notes without a date
        sort last. Empty list if the project has no tickets, no
        comments, or no flagged notes.
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
                        "date": _normalize_comment_date(comment.get("date")),
                    }
                )

    notes.sort(key=lambda n: n.get("date") or "", reverse=True)
    return notes
