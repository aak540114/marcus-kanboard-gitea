"""
Structured AI comment protocol for human-gated ticket workflows.

Marcus communicates with humans exclusively through ticket comments.
This module defines a consistent comment format so that:

1. Humans can quickly parse Marcus's status at a glance.
2. Marcus can detect its own prior comments vs. human comments.
3. A board watcher can identify *new human comments* that need a response.

Comment anatomy
---------------
Every Marcus comment starts with a header sentinel and ends with a
footer sentinel::

    <!-- MARCUS_COMMENT type="progress" ticket_id="PROJ-42" -->
    ### Marcus Agent — Progress Update

    **Status:** In Progress (35%)
    **Branch:** `ticket/jira/proj-42`

    <progress body>

    ---
    *Posted automatically by Marcus AI agent. Do not edit this comment.*
    <!-- END_MARCUS_COMMENT -->

Classes
-------
CommentType
    Enum of comment purposes.
CommentFormatter
    Builds structured Marcus comments.
CommentParser
    Identifies and parses comments left by Marcus vs. humans.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

_SENTINEL_RE = re.compile(
    r"<!-- MARCUS_COMMENT .*?-->",
    re.DOTALL,
)
_TYPE_RE = re.compile(r'type="([^"]+)"')
_TICKET_RE = re.compile(r'ticket_id="([^"]+)"')
_FOOTER = (
    "\n\n---\n"
    "*Posted automatically by Marcus AI agent. "
    "Reply to this ticket to interact with the agent.*\n"
    "<!-- END_MARCUS_COMMENT -->"
)


class CommentType(Enum):
    """Supported types of Marcus comments.

    Attributes
    ----------
    AC_GENERATED : str
        Initial acceptance criteria posted when a new ticket is detected.
    STARTED : str
        Posted when AI begins work on a ticket.
    PROGRESS : str
        Periodic progress updates.
    REVISION_REQUESTED : str
        Acknowledgement that the AI received a revision request.
    READY_FOR_REVIEW : str
        AI is done; human needs to review and accept.
    DEV_ENV_STARTED : str
        Hot-reload dev environment is available.
    MERGED : str
        Branch was merged to main.
    ERROR : str
        An error occurred; needs human attention.
    """

    AC_GENERATED = "ac_generated"
    STARTED = "started"
    PROGRESS = "progress"
    REVISION_REQUESTED = "revision_requested"
    READY_FOR_REVIEW = "ready_for_review"
    DEV_ENV_STARTED = "dev_env_started"
    MERGED = "merged"
    ERROR = "error"


@dataclass
class ParsedComment:
    """A comment that was identified as coming from Marcus.

    Parameters
    ----------
    comment_type : CommentType
        The type of Marcus comment.
    ticket_id : str
        The ticket this comment belongs to.
    raw_body : str
        Full raw text of the comment.
    """

    comment_type: CommentType
    ticket_id: str
    raw_body: str


class CommentFormatter:
    """Builds structured Marcus comments in markdown.

    All methods return a complete comment string ready to be posted via
    the kanban provider's ``add_comment()`` method.
    """

    @staticmethod
    def _header(comment_type: CommentType, ticket_id: str) -> str:
        return (
            f'<!-- MARCUS_COMMENT type="{comment_type.value}" '
            f'ticket_id="{ticket_id}" -->'
        )

    @classmethod
    def ac_generated(
        cls,
        ticket_id: str,
        ac_markdown: str,
        *,
        was_human_created: bool = False,
    ) -> str:
        """Comment posted when Marcus generates acceptance criteria.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        ac_markdown : str
            The generated checklist (lines starting with ``- [ ]``).
        was_human_created : bool
            If ``True``, note that the ticket was created by a human and
            AC was auto-generated.

        Returns
        -------
        str
            Full comment body.
        """
        note = (
            "\n> *This ticket was created without explicit acceptance criteria. "
            "Marcus generated the checklist below from the ticket description. "
            "Edit the checklist directly in the ticket description "
            "to change requirements.*\n"
            if was_human_created
            else ""
        )
        body = (
            f"{cls._header(CommentType.AC_GENERATED, ticket_id)}\n"
            f"### Marcus Agent — Acceptance Criteria Generated\n"
            f"{note}\n"
            f"The following acceptance criteria have been added to the ticket "
            f"description and will be used to verify completion.\n\n"
            f"{ac_markdown}\n"
            f"\n**Next step:** Assign this ticket to yourself "
            f"to start AI implementation."
            f"{_FOOTER}"
        )
        return body

    @classmethod
    def started(
        cls,
        ticket_id: str,
        branch_name: str,
        assignee: str,
        ac_items: Optional[List[str]] = None,
    ) -> str:
        """Comment posted when the AI agent begins work.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        branch_name : str
            Git branch created for this ticket.
        assignee : str
            Human who assigned the ticket.
        ac_items : Optional[List[str]]
            Acceptance criteria items for reference.

        Returns
        -------
        str
            Full comment body.
        """
        ac_section = ""
        if ac_items:
            ac_section = (
                "\n**Working towards:**\n"
                + "\n".join(f"- [ ] {item}" for item in ac_items)
                + "\n"
            )

        body = (
            f"{cls._header(CommentType.STARTED, ticket_id)}\n"
            f"### Marcus Agent — Work Started\n\n"
            f"Picked up by Marcus AI agent on behalf of **{assignee}**.\n\n"
            f"**Branch:** `{branch_name}`\n"
            f"{ac_section}"
            f"\nProgress updates will be posted as comments. "
            f"You can edit the acceptance criteria in the ticket description "
            f"at any time — Marcus will detect the change and adjust."
            f"{_FOOTER}"
        )
        return body

    @classmethod
    def progress(
        cls,
        ticket_id: str,
        branch_name: str,
        percentage: int,
        message: str,
        *,
        commits: Optional[List[str]] = None,
    ) -> str:
        """Periodic progress update comment.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        branch_name : str
            Git branch.
        percentage : int
            Completion percentage (0–100).
        message : str
            Free-text progress description.
        commits : Optional[List[str]]
            Recent commit summaries.

        Returns
        -------
        str
            Full comment body.
        """
        bar_filled = "█" * (percentage // 10)
        bar_empty = "░" * (10 - percentage // 10)
        bar = f"[{bar_filled}{bar_empty}] {percentage}%"

        commit_section = ""
        if commits:
            commit_section = (
                "\n**Recent commits:**\n"
                + "\n".join(f"- `{c}`" for c in commits[-5:])
                + "\n"
            )

        body = (
            f"{cls._header(CommentType.PROGRESS, ticket_id)}\n"
            f"### Marcus Agent — Progress Update\n\n"
            f"**Progress:** {bar}\n"
            f"**Branch:** `{branch_name}`\n\n"
            f"{message}\n"
            f"{commit_section}"
            f"{_FOOTER}"
        )
        return body

    @classmethod
    def revision_requested(
        cls,
        ticket_id: str,
        human_comment: str,
        ai_understanding: str,
    ) -> str:
        """Acknowledgement that Marcus received a revision request.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        human_comment : str
            The exact human comment text.
        ai_understanding : str
            Marcus's interpretation of what needs to change.

        Returns
        -------
        str
            Full comment body.
        """
        body = (
            f"{cls._header(CommentType.REVISION_REQUESTED, ticket_id)}\n"
            f"### Marcus Agent — Revision Request Received\n\n"
            f"**Your request:**\n> {human_comment.strip()}\n\n"
            f"**My understanding of what needs to change:**\n{ai_understanding}\n\n"
            f"I'll re-read the latest acceptance criteria, apply the changes, "
            f"and post a new *Ready for Review* comment when done."
            f"{_FOOTER}"
        )
        return body

    @classmethod
    def ready_for_review(
        cls,
        ticket_id: str,
        branch_name: str,
        ac_items: List[str],
        *,
        dev_env_url: Optional[str] = None,
        commit_count: int = 0,
    ) -> str:
        """Comment posted when the AI agent finishes and awaits acceptance.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        branch_name : str
            Git branch containing the changes.
        ac_items : List[str]
            Acceptance criteria items (all should be met).
        dev_env_url : Optional[str]
            URL to the hot-reload dev environment, if available.
        commit_count : int
            Number of commits on the ticket branch.

        Returns
        -------
        str
            Full comment body.
        """
        ac_section = "\n".join(f"- [x] {item}" for item in ac_items)
        dev_section = (
            f"\n**Hot-reload preview:** [{dev_env_url}]({dev_env_url})\n"
            if dev_env_url
            else (
                "\n> To spin up a live preview: comment `@marcus start-dev-env` "
                "on this ticket.\n"
            )
        )
        commits_note = f" ({commit_count} commits)" if commit_count else ""

        body = (
            f"{cls._header(CommentType.READY_FOR_REVIEW, ticket_id)}\n"
            f"### Marcus Agent — Ready for Review\n\n"
            f"Implementation complete{commits_note}. "
            f"All acceptance criteria addressed:\n\n"
            f"{ac_section}\n"
            f"{dev_section}\n"
            f"**Branch:** `{branch_name}`\n\n"
            f"**To accept:** close this ticket (or transition it to *Done*). "
            f"The branch will be merged to main automatically.\n\n"
            f"**To request changes:** leave a comment describing what needs "
            f"to be different — Marcus will acknowledge and rework."
            f"{_FOOTER}"
        )
        return body

    @classmethod
    def dev_env_started(
        cls,
        ticket_id: str,
        branch_name: str,
        url: str,
        port: int,
    ) -> str:
        """Comment posted when the hot-reload dev environment is ready.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        branch_name : str
            Git branch the dev env is running.
        url : str
            Base URL to access the running environment.
        port : int
            TCP port.

        Returns
        -------
        str
            Full comment body.
        """
        body = (
            f"{cls._header(CommentType.DEV_ENV_STARTED, ticket_id)}\n"
            f"### Marcus Agent — Dev Environment Ready\n\n"
            f"A live hot-reload environment for branch `{branch_name}` "
            f"is running at:\n\n"
            f"**[{url}]({url})**  (port {port})\n\n"
            f"The environment rebuilds automatically on every new commit to "
            f"the branch.  It will shut down when the ticket is accepted or "
            f"after 4 hours of inactivity."
            f"{_FOOTER}"
        )
        return body

    @classmethod
    def merged(
        cls,
        ticket_id: str,
        branch_name: str,
        main_branch: str = "main",
    ) -> str:
        """Comment posted after the ticket branch is merged to main.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        branch_name : str
            Branch that was merged.
        main_branch : str
            The integration branch (default ``"main"``).

        Returns
        -------
        str
            Full comment body.
        """
        body = (
            f"{cls._header(CommentType.MERGED, ticket_id)}\n"
            f"### Marcus Agent — Merged to {main_branch}\n\n"
            f"Branch `{branch_name}` has been merged into `{main_branch}`. "
            f"This ticket is complete.\n\n"
            f"If you reopen the ticket, Marcus will rebase the branch on the "
            f"latest `{main_branch}` and continue with the new requirements."
            f"{_FOOTER}"
        )
        return body

    @classmethod
    def error(
        cls,
        ticket_id: str,
        error_summary: str,
        *,
        needs_human: bool = True,
    ) -> str:
        """Comment posted when the AI encounters an unrecoverable error.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        error_summary : str
            Brief description of what went wrong.
        needs_human : bool
            If ``True``, explicitly request human intervention.

        Returns
        -------
        str
            Full comment body.
        """
        action = (
            "\n**Action needed:** Please review the error above and either "
            "update the ticket requirements or comment with guidance."
            if needs_human
            else ""
        )
        body = (
            f"{cls._header(CommentType.ERROR, ticket_id)}\n"
            f"### Marcus Agent — Error\n\n"
            f"Marcus encountered an issue while working on this ticket:\n\n"
            f"> {error_summary}\n"
            f"{action}"
            f"{_FOOTER}"
        )
        return body


class CommentParser:
    r"""Identifies and parses Marcus comments vs. human comments.

    Usage
    -----
    >>> parser = CommentParser()
    >>> is_marcus = parser.is_marcus_comment("<!-- MARCUS_COMMENT ... -->...")
    >>> body = '<!-- MARCUS_COMMENT type="progress" ticket_id="42" -->'
    >>> parsed = parser.parse(body)
    """

    @staticmethod
    def is_marcus_comment(text: str) -> bool:
        """Return ``True`` if *text* is a Marcus-generated comment."""
        return bool(_SENTINEL_RE.search(text))

    @staticmethod
    def parse(text: str) -> Optional[ParsedComment]:
        """Extract metadata from a Marcus comment.

        Parameters
        ----------
        text : str
            Raw comment body.

        Returns
        -------
        Optional[ParsedComment]
            Parsed comment, or ``None`` if the comment is not from Marcus.
        """
        header_match = _SENTINEL_RE.search(text)
        if not header_match:
            return None

        header = header_match.group(0)
        type_match = _TYPE_RE.search(header)
        ticket_match = _TICKET_RE.search(header)

        if not type_match or not ticket_match:
            return None

        try:
            comment_type = CommentType(type_match.group(1))
        except ValueError:
            return None

        return ParsedComment(
            comment_type=comment_type,
            ticket_id=ticket_match.group(1),
            raw_body=text,
        )

    @staticmethod
    def extract_human_instructions(
        comments: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Filter a list of ticket comments to human-authored ones only.

        Parameters
        ----------
        comments : List[Dict[str, Any]]
            Raw comment objects from the kanban provider.  Each dict must
            contain at least ``"body"`` (str) and ``"author"`` (str) keys.

        Returns
        -------
        List[Dict[str, Any]]
            Only those comments NOT written by Marcus (i.e., human comments).
        """
        return [
            c
            for c in comments
            if not CommentParser.is_marcus_comment(c.get("body", ""))
        ]

    @staticmethod
    def contains_command(text: str, command: str) -> bool:
        """Check whether a comment body contains a @marcus command.

        Parameters
        ----------
        text : str
            Comment body.
        command : str
            Command keyword, e.g. ``"start-dev-env"``, ``"restart"``.

        Returns
        -------
        bool
            ``True`` if ``@marcus {command}`` appears in the text.
        """
        pattern = re.compile(rf"@marcus\s+{re.escape(command)}", re.IGNORECASE)
        return bool(pattern.search(text))
