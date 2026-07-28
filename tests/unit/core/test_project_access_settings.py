"""
Unit tests for src/core/project_access_settings.py
"""

from pathlib import Path

import pytest

from src.core.project_access_settings import ProjectAccessSettingManager


class TestProjectAccessSettingManager:
    """Per-project allowlist: is Marcus (and any AI agent) permitted to work
    on this Kanboard project's tickets at all — independent of gate mode,
    which governs HOW Marcus works once it's already allowed to."""

    @pytest.fixture()
    def mgr(self, tmp_path: Path) -> ProjectAccessSettingManager:
        """Manager backed by a temp directory."""
        return ProjectAccessSettingManager(data_dir=tmp_path)

    # ── Default: off ─────────────────────────────────────────────────────

    def test_unconfigured_project_is_disabled_by_default(self, mgr):
        """A project with no explicit setting is NOT enabled — the whole
        point of this allowlist is that Marcus stays off until a human
        opts a project in."""
        assert mgr.is_enabled(1) is False

    def test_unconfigured_project_returns_none_for_explicit_getter(self, mgr):
        """The raw explicit-value getter distinguishes 'never configured'
        from 'explicitly set to False' (useful for the UI / diagnostics),
        even though both resolve to is_enabled() == False."""
        assert mgr.get_project_enabled(1) is None

    # ── Set / get round-trip ─────────────────────────────────────────────

    def test_set_enabled_true_then_is_enabled(self, mgr):
        mgr.set_project_enabled(7, True)
        assert mgr.is_enabled(7) is True
        assert mgr.get_project_enabled(7) is True

    def test_set_enabled_false_explicitly(self, mgr):
        """Explicitly disabling a project (e.g. revoking a previously
        enabled one) is distinct from 'never configured' but resolves the
        same way through is_enabled()."""
        mgr.set_project_enabled(7, True)
        mgr.set_project_enabled(7, False)
        assert mgr.is_enabled(7) is False
        assert mgr.get_project_enabled(7) is False

    def test_settings_are_per_project(self, mgr):
        """Enabling one project does not affect another."""
        mgr.set_project_enabled(1, True)
        assert mgr.is_enabled(1) is True
        assert mgr.is_enabled(2) is False

    def test_persisted_to_disk(self, tmp_path):
        """set_project_enabled writes data that survives a new manager
        instance (a fresh HTTP-route-side manager must see a UI toggle
        flipped by another manager instance)."""
        mgr1 = ProjectAccessSettingManager(data_dir=tmp_path)
        mgr1.set_project_enabled(3, True)

        mgr2 = ProjectAccessSettingManager(data_dir=tmp_path)
        assert mgr2.is_enabled(3) is True

    def test_corrupt_file_falls_back_to_disabled(self, tmp_path):
        """A corrupt/unreadable settings file must never crash Marcus, and
        must fail SAFE — closed (disabled), not open."""
        path = tmp_path / "project_access_settings.json"
        path.write_text("{not valid json", encoding="utf-8")
        mgr = ProjectAccessSettingManager(data_dir=tmp_path)
        assert mgr.is_enabled(1) is False
