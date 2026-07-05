"""
Acceptance criteria generation, extraction, and change detection.

When Marcus creates a task it automatically generates an acceptance
criteria (AC) checklist and embeds it in the ticket description using
a well-known marker block.  The board watcher can then detect when a
human edits the AC and notify the AI agent to re-read the requirements.

AC format in ticket descriptions
---------------------------------
The AC lives between two sentinel comments that survive round-trips
through GitHub/Jira::

    <!-- MARCUS_AC_START -->
    ## Acceptance Criteria

    - [ ] First criterion
    - [ ] Second criterion

    <!-- MARCUS_AC_END -->

Any text outside the sentinels is untouched by Marcus.

Classes
-------
ACGenerator
    Uses a lightweight heuristic (or an optional LLM call) to produce
    an initial AC checklist from a task title + description.
ACParser
    Extracts and parses the AC block embedded in ticket descriptions.
ACChangeDetector
    Compares the current AC hash against the stored hash to detect edits.
"""

import hashlib
import logging
import re
import textwrap
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_AC_START = "<!-- MARCUS_AC_START -->"
_AC_END = "<!-- MARCUS_AC_END -->"
_AC_HEADER = "## Acceptance Criteria"

# Regex to pull out the block between the sentinels (including sentinels).
_AC_BLOCK_RE = re.compile(
    r"<!-- MARCUS_AC_START -->.*?<!-- MARCUS_AC_END -->",
    re.DOTALL,
)

# Regex to find individual checklist items (checked or unchecked).
_ITEM_RE = re.compile(r"^- \[([xX ])\] (.+)$", re.MULTILINE)


@dataclass
class ACItem:
    """A single acceptance criterion.

    Parameters
    ----------
    text : str
        The criterion text.
    checked : bool
        Whether the criterion has been checked off by the human.
    """

    text: str
    checked: bool = False


@dataclass
class AcceptanceCriteria:
    """Parsed acceptance criteria block.

    Parameters
    ----------
    items : List[ACItem]
        Individual criteria.
    raw_text : str
        The raw markdown text of the AC block (sentinel tags excluded).
    ac_hash : str
        SHA-256 hex digest of *raw_text* (for change detection).
    """

    items: List[ACItem] = field(default_factory=list)
    raw_text: str = ""
    ac_hash: str = ""

    @property
    def all_checked(self) -> bool:
        """True when every criterion has been checked off."""
        return bool(self.items) and all(i.checked for i in self.items)

    @property
    def unchecked_items(self) -> List[ACItem]:
        """Items that have not yet been checked."""
        return [i for i in self.items if not i.checked]


class ACParser:
    r"""Extracts and parses the Marcus AC block from ticket descriptions.

    Usage
    -----
    >>> parser = ACParser()
    >>> ac = parser.extract(
    ...     "<!-- MARCUS_AC_START -->\n## Acceptance Criteria\n"
    ...     "- [ ] Deploy service\n<!-- MARCUS_AC_END -->"
    ... )
    >>> ac.items[0].text
    'Deploy service'
    """

    @staticmethod
    def extract(description: str) -> Optional[AcceptanceCriteria]:
        """Extract the AC block from a ticket description.

        Parameters
        ----------
        description : str
            Full ticket description text.

        Returns
        -------
        Optional[AcceptanceCriteria]
            Parsed AC, or ``None`` if no Marcus AC block is present.
        """
        match = _AC_BLOCK_RE.search(description)
        if not match:
            return None

        block = match.group(0)
        # Strip sentinels to get the inner markdown.
        inner = block.replace(_AC_START, "").replace(_AC_END, "").strip()

        items = [
            ACItem(text=m.group(2).strip(), checked=m.group(1).lower() == "x")
            for m in _ITEM_RE.finditer(inner)
        ]
        ac_hash = hashlib.sha256(inner.encode()).hexdigest()
        return AcceptanceCriteria(items=items, raw_text=inner, ac_hash=ac_hash)

    @staticmethod
    def embed(description: str, ac_markdown: str) -> str:
        """Insert or replace the AC block in a ticket description.

        Parameters
        ----------
        description : str
            Current ticket description (may or may not have an AC block).
        ac_markdown : str
            The new AC markdown text (without sentinels).

        Returns
        -------
        str
            Updated description with the AC block embedded.
        """
        block = f"{_AC_START}\n{_AC_HEADER}\n\n{ac_markdown.strip()}\n{_AC_END}"
        if _AC_BLOCK_RE.search(description):
            return _AC_BLOCK_RE.sub(block, description)
        return f"{description.rstrip()}\n\n{block}"

    @staticmethod
    def remove(description: str) -> str:
        """Remove the AC block from a description (leaves surrounding text)."""
        return _AC_BLOCK_RE.sub("", description).strip()


class ACGenerator:
    """Generates acceptance criteria checklists for tasks.

    This implementation uses a rule-based heuristic that produces a
    reasonable AC checklist for common task types without requiring an
    LLM call.  For richer AC generation, pass an *llm_generate* callable.

    Parameters
    ----------
    llm_generate : Optional[callable]
        Async callable ``(prompt: str) -> str`` that returns LLM-generated
        text.  When provided it is used to produce AC instead of the
        heuristic fallback.
    """

    def __init__(self, llm_generate: Optional[object] = None) -> None:
        """Initialise the generator."""
        self._llm_generate = llm_generate

    async def generate(
        self,
        title: str,
        description: str,
        labels: Optional[List[str]] = None,
    ) -> str:
        """Generate a markdown AC checklist for a task.

        Parameters
        ----------
        title : str
            Task title / summary.
        description : str
            Task body / description.
        labels : Optional[List[str]]
            Any labels/tags on the ticket (e.g. ``["bug", "frontend"]``).

        Returns
        -------
        str
            Markdown checklist string (lines starting with ``- [ ]``).
        """
        if self._llm_generate is not None:
            return await self._llm_generate_ac(title, description, labels or [])
        return self._heuristic_ac(title, description, labels or [])

    async def _llm_generate_ac(
        self, title: str, description: str, labels: List[str]
    ) -> str:
        """Call the injected LLM to generate AC."""
        assert self._llm_generate is not None
        label_hint = f"Labels: {', '.join(labels)}\n" if labels else ""
        prompt = textwrap.dedent(f"""
            You are a senior software engineer writing acceptance criteria for a task.

            Task title: {title}
            {label_hint}Description:
            {description}

            Write a concise GitHub-flavoured markdown checklist of acceptance
            criteria.  Each item must be a verifiable, testable condition.
            Output ONLY the checklist, nothing else.  Use this format:
            - [ ] criterion one
            - [ ] criterion two
        """).strip()
        try:
            result = await self._llm_generate(prompt)  # type: ignore[operator]
            return str(result).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LLM AC generation failed, falling back to heuristic: %s", exc
            )
            return self._heuristic_ac(title, description, labels)

    def _heuristic_ac(self, title: str, description: str, labels: List[str]) -> str:
        """Produce a rule-based AC checklist from task metadata."""
        items: List[str] = []
        text = f"{title} {description}".lower()
        label_text = " ".join(labels).lower()

        # Universal baseline criteria.
        items.append("- [ ] Implementation satisfies the stated requirements")
        items.append("- [ ] All existing tests continue to pass")
        items.append("- [ ] New or updated code has unit test coverage")

        # Type-specific criteria.
        if any(kw in text for kw in ("api", "endpoint", "rest", "graphql", "route")):
            items.append(
                "- [ ] API endpoint returns correct status codes "
                "for happy and error paths"
            )
            items.append("- [ ] API response schema is documented or typed")

        if any(
            kw in text
            for kw in ("ui", "frontend", "component", "page", "screen", "dashboard")
        ):
            items.append(
                "- [ ] UI renders correctly in supported browsers / viewport sizes"
            )
            items.append(
                "- [ ] Accessibility: semantic HTML, keyboard-navigable, "
                "colour-contrast passes"
            )

        if any(kw in text for kw in ("bug", "fix", "regression", "broken", "error")):
            items.append(
                "- [ ] The originally reported bug can no longer be reproduced"
            )
            items.append("- [ ] A regression test is added to prevent recurrence")

        if any(
            kw in text for kw in ("database", "db", "schema", "migration", "sql", "orm")
        ):
            items.append("- [ ] Database migration is idempotent and reversible")
            items.append(
                "- [ ] Queries are covered by integration tests against a real DB"
            )

        if any(
            kw in text
            for kw in (
                "auth",
                "login",
                "permission",
                "role",
                "security",
                "jwt",
                "token",
            )
        ):
            items.append(
                "- [ ] Unauthenticated / unauthorised requests "
                "are rejected with 401/403"
            )
            items.append("- [ ] Security review completed (OWASP top-10 items checked)")

        if any(
            kw in text
            for kw in ("perf", "performance", "latency", "slow", "optimis", "cache")
        ):
            items.append("- [ ] Performance target met (specify: e.g. p95 < 200 ms)")

        if "docker" in text or "container" in label_text:
            items.append(
                "- [ ] Docker image builds successfully and passes health checks"
            )

        # Code-quality baseline.
        items.append("- [ ] Code reviewed and all review comments addressed")
        items.append("- [ ] No new linter warnings or type errors introduced")

        return "\n".join(items)

    def format_for_description(self, ac_markdown: str) -> str:
        """Wrap AC checklist in a description-ready block.

        Parameters
        ----------
        ac_markdown : str
            Raw checklist markdown.

        Returns
        -------
        str
            Full block (with sentinels) ready to be appended to a description.
        """
        return ACParser.embed("", ac_markdown).strip()


class ACChangeDetector:
    """Detects when a human has edited the acceptance criteria.

    Compares the SHA-256 hash of the current AC text against the hash
    stored in the ticket's lifecycle record.

    Usage
    -----
    >>> detector = ACChangeDetector()
    >>> changed, new_hash = detector.check("- [ ] Old criterion", "old_hash")
    >>> changed
    False  # hash matches, no change
    """

    @staticmethod
    def hash_ac(ac_text: str) -> str:
        """Compute the SHA-256 hex digest of *ac_text*.

        Parameters
        ----------
        ac_text : str
            Raw acceptance criteria markdown.

        Returns
        -------
        str
            64-character hex string.
        """
        return hashlib.sha256(ac_text.encode()).hexdigest()

    @staticmethod
    def check(current_ac_text: str, stored_hash: str) -> Tuple[bool, str]:
        """Check whether the AC has changed since it was last read.

        Parameters
        ----------
        current_ac_text : str
            AC text fetched from the kanban provider right now.
        stored_hash : str
            Hash stored in the TicketRecord when AC was last read.

        Returns
        -------
        Tuple[bool, str]
            ``(changed, new_hash)`` — *changed* is ``True`` if the hash
            differs, *new_hash* is the freshly computed digest.
        """
        new_hash = ACChangeDetector.hash_ac(current_ac_text)
        return new_hash != stored_hash, new_hash
