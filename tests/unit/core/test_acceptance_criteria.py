"""
Unit tests for src/core/acceptance_criteria.py
"""

import hashlib

import pytest

from src.core.acceptance_criteria import (
    AcceptanceCriteria,
    ACChangeDetector,
    ACGenerator,
    ACItem,
    ACParser,
)

_SAMPLE_DESC = """
This is a task description.

<!-- MARCUS_AC_START -->
## Acceptance Criteria

- [ ] Deploy the service to production
- [x] Add unit tests
- [ ] Update documentation
<!-- MARCUS_AC_END -->

Some trailing text.
""".strip()

_NO_AC_DESC = "Just a plain description with no acceptance criteria block."


class TestACParser:
    """Tests for ACParser.extract, embed, remove."""

    def test_extract_parses_items(self):
        """extract() returns all checklist items from the block."""
        ac = ACParser.extract(_SAMPLE_DESC)
        assert ac is not None
        assert len(ac.items) == 3

    def test_extract_checked_item(self):
        """Checked items (- [x]) are parsed with checked=True."""
        ac = ACParser.extract(_SAMPLE_DESC)
        checked = [i for i in ac.items if i.checked]
        assert len(checked) == 1
        assert checked[0].text == "Add unit tests"

    def test_extract_unchecked_items(self):
        """Unchecked items (- [ ]) are parsed with checked=False."""
        ac = ACParser.extract(_SAMPLE_DESC)
        unchecked = [i for i in ac.items if not i.checked]
        assert len(unchecked) == 2

    def test_extract_returns_none_when_no_block(self):
        """Returns None when there is no Marcus AC block."""
        assert ACParser.extract(_NO_AC_DESC) is None

    def test_extract_hash_is_sha256(self):
        """ac_hash is a 64-char hex string."""
        ac = ACParser.extract(_SAMPLE_DESC)
        assert len(ac.ac_hash) == 64
        assert all(c in "0123456789abcdef" for c in ac.ac_hash)

    def test_embed_inserts_block(self):
        """embed() appends the AC block to a description without one."""
        result = ACParser.embed(_NO_AC_DESC, "- [ ] Test passes")
        assert "<!-- MARCUS_AC_START -->" in result
        assert "- [ ] Test passes" in result
        assert _NO_AC_DESC in result

    def test_embed_replaces_existing_block(self):
        """embed() replaces an existing AC block."""
        result = ACParser.embed(_SAMPLE_DESC, "- [ ] Brand new criterion")
        assert "Deploy the service" not in result
        assert "Brand new criterion" in result
        assert result.count("<!-- MARCUS_AC_START -->") == 1

    def test_remove_strips_block(self):
        """remove() strips the AC block leaving surrounding text."""
        result = ACParser.remove(_SAMPLE_DESC)
        assert "<!-- MARCUS_AC_START -->" not in result
        assert "This is a task description." in result
        assert "Some trailing text." in result

    def test_all_checked_property(self):
        """all_checked is True only when every item is checked."""
        ac = AcceptanceCriteria(
            items=[ACItem("A", True), ACItem("B", True)],
            raw_text="",
            ac_hash="",
        )
        assert ac.all_checked is True
        ac.items.append(ACItem("C", False))
        assert ac.all_checked is False

    def test_unchecked_items_property(self):
        """unchecked_items returns only the unchecked ones."""
        ac = AcceptanceCriteria(
            items=[ACItem("A", True), ACItem("B", False), ACItem("C", False)],
            raw_text="",
            ac_hash="",
        )
        assert len(ac.unchecked_items) == 2

    def test_extract_case_insensitive_checked(self):
        """Both [X] and [x] are treated as checked."""
        desc = "<!-- MARCUS_AC_START -->\n## Acceptance Criteria\n\n- [X] Item A\n<!-- MARCUS_AC_END -->"
        ac = ACParser.extract(desc)
        assert ac is not None
        assert ac.items[0].checked is True


class TestACGenerator:
    """Tests for ACGenerator.generate (heuristic path)."""

    @pytest.fixture
    def gen(self):
        return ACGenerator()  # no LLM injected

    @pytest.mark.asyncio
    async def test_generate_returns_checklist(self, gen):
        """generate() returns a non-empty checklist string."""
        result = await gen.generate("Add login page", "Implement OAuth login")
        assert "- [ ]" in result

    @pytest.mark.asyncio
    async def test_api_task_adds_api_criteria(self, gen):
        """API keywords trigger API-specific criteria."""
        result = await gen.generate("Build REST endpoint", "Create /users API endpoint")
        assert "API" in result or "endpoint" in result.lower()

    @pytest.mark.asyncio
    async def test_bug_task_adds_regression_criterion(self, gen):
        """Bug keyword triggers regression test criterion."""
        result = await gen.generate(
            "Fix login bug", "User cannot login due to null pointer bug"
        )
        lower = result.lower()
        assert "regression" in lower or "bug" in lower

    @pytest.mark.asyncio
    async def test_ui_task_adds_accessibility(self, gen):
        """UI keyword triggers accessibility criterion."""
        result = await gen.generate(
            "Add dashboard UI", "Build a frontend dashboard page"
        )
        lower = result.lower()
        assert "accessibility" in lower or "ui" in lower or "browser" in lower

    @pytest.mark.asyncio
    async def test_auth_task_adds_security(self, gen):
        """Security keywords trigger auth-specific criteria."""
        result = await gen.generate(
            "Implement JWT auth", "Add JWT token authentication"
        )
        lower = result.lower()
        assert "unauthorised" in lower or "401" in lower or "security" in lower

    @pytest.mark.asyncio
    async def test_baseline_criteria_always_present(self, gen):
        """Universal baseline criteria are included for any task."""
        result = await gen.generate("Do something", "Generic task")
        assert "existing tests" in result.lower() or "unit test" in result.lower()

    @pytest.mark.asyncio
    async def test_llm_fallback_on_error(self):
        """When LLM callable raises, falls back to heuristic gracefully."""

        async def failing_llm(prompt: str) -> str:
            raise RuntimeError("LLM unreachable")

        gen = ACGenerator(llm_generate=failing_llm)
        result = await gen.generate("Any title", "Any description")
        assert "- [ ]" in result  # heuristic result

    @pytest.mark.asyncio
    async def test_llm_result_used_when_available(self):
        """When LLM callable succeeds, its result is returned."""

        async def mock_llm(prompt: str) -> str:
            return "- [ ] LLM criterion one\n- [ ] LLM criterion two"

        gen = ACGenerator(llm_generate=mock_llm)
        result = await gen.generate("Task", "Description")
        assert "LLM criterion one" in result

    def test_format_for_description_wraps_in_sentinels(self, gen):
        """format_for_description wraps text in Marcus sentinels."""
        ac_text = "- [ ] Deploy service"
        result = gen.format_for_description(ac_text)
        assert "<!-- MARCUS_AC_START -->" in result
        assert "<!-- MARCUS_AC_END -->" in result
        assert "Deploy service" in result


class TestACChangeDetector:
    """Tests for ACChangeDetector.check and hash_ac."""

    def test_hash_ac_is_deterministic(self):
        """Same input always produces same hash."""
        text = "- [ ] Deploy service"
        h1 = ACChangeDetector.hash_ac(text)
        h2 = ACChangeDetector.hash_ac(text)
        assert h1 == h2

    def test_hash_ac_is_sha256(self):
        """hash_ac returns a 64-char hex SHA-256."""
        text = "- [ ] test"
        h = ACChangeDetector.hash_ac(text)
        assert h == hashlib.sha256(text.encode()).hexdigest()

    def test_check_detects_change(self):
        """check() returns changed=True when text differs from stored hash."""
        text = "- [ ] New criterion"
        stored_hash = "old_hash_not_matching"
        changed, new_hash = ACChangeDetector.check(text, stored_hash)
        assert changed is True
        assert len(new_hash) == 64

    def test_check_no_change_when_same(self):
        """check() returns changed=False when text matches stored hash."""
        text = "- [ ] Same criterion"
        stored_hash = ACChangeDetector.hash_ac(text)
        changed, new_hash = ACChangeDetector.check(text, stored_hash)
        assert changed is False
        assert new_hash == stored_hash

    def test_check_whitespace_sensitive(self):
        """Trailing space changes the hash."""
        text = "- [ ] Criterion"
        h1 = ACChangeDetector.hash_ac(text)
        h2 = ACChangeDetector.hash_ac(text + " ")
        assert h1 != h2

    def test_check_empty_stored_hash_always_changes(self):
        """Empty stored_hash (first check) always reports changed."""
        changed, _ = ACChangeDetector.check("- [ ] Something", "")
        assert changed is True
