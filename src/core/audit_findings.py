"""
Extraction of AI-Audit findings from ticket comments.

An audit ticket (created by :meth:`HumanGatedWorkflow.create_audit_ticket`)
instructs the worker agent to flag each independently-verified bug it finds
by posting a comment (via the existing ``post_ticket_progress`` tool) whose
body contains one or more entries starting with "### 🔍 Audit Finding:
<title>", followed by the bug explanation, reproduction steps, and proposed
fix in plain language.

When the audit ticket signals ready for review, Marcus scans its comments
for these entries and turns each into its own focused child ticket (reusing
:meth:`HumanGatedWorkflow._create_child_tickets` — the same write path
``decompose_ticket`` uses) instead of bundling every fix into the audit
ticket's own branch, so each finding gets an independent review.

Functions
---------
extract_findings_from_comment
    Pull every "🔍 Audit Finding: ..." entry out of a single comment's raw
    text.
"""

import re
from dataclasses import dataclass
from typing import List

# Matches "### 🔍 Audit Finding: <title>" (any heading level 1-6, so a
# hand-typed "## 🔍 Audit Finding: ..." still counts), capturing the title
# on that line and the body up to whichever comes first: the "\n\n---"
# footer CommentFormatter appends (see ``_FOOTER`` in
# src/core/comment_protocol.py — post_ticket_progress comments go through
# it), the START of another finding later in the SAME comment, or the end
# of the comment. Same lookahead-not-consuming trick as decision_notes.py's
# _NOTE_RE, for the same reason: without it, a comment flagging two
# findings in one post_ticket_progress call would merge into a single
# match spanning both.
_FINDING_RE = re.compile(
    r"^#{1,6}[ \t]*🔍[ \t]*Audit Finding:[ \t]*(.+?)[ \t]*\n"
    r"(.*?)"
    r"(?=\n\n---|\n#{1,6}[ \t]*🔍[ \t]*Audit Finding:|\Z)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)


@dataclass
class AuditFinding:
    """A single verified bug an audit ticket flagged.

    Parameters
    ----------
    title : str
        Short finding title (from the heading line).
    body : str
        The rest of the entry: bug explanation, reproduction steps, and
        proposed fix, trimmed of surrounding whitespace.
    """

    title: str
    body: str


def extract_findings_from_comment(content: str) -> List[AuditFinding]:
    """Return every flagged audit finding found in a single comment.

    Parameters
    ----------
    content : str
        Raw comment text (as returned by ``KanboardKanban.get_comments``'
        ``"content"`` key).

    Returns
    -------
    List[AuditFinding]
        Each "🔍 Audit Finding: ..." entry found, in the order they
        appear. Empty when the comment has no finding, or when a heading
        matched but its body was blank.
    """
    if not content:
        return []
    findings = []
    for match in _FINDING_RE.finditer(content):
        title = match.group(1).strip()
        body = match.group(2).strip()
        if title and body:
            findings.append(AuditFinding(title=title, body=body))
    return findings
