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


class TestEnabledProjectIds:
    """enabled_project_ids() is what scopes which boards Marcus reads."""

    def test_lists_only_explicitly_enabled_projects(self, tmp_path):
        """Disabled and never-configured projects are excluded."""
        mgr = ProjectAccessSettingManager(data_dir=tmp_path)
        mgr.set_project_enabled(8, True)
        mgr.set_project_enabled(7, False)
        mgr.set_project_enabled(2, True)
        assert mgr.enabled_project_ids() == [2, 8]

    def test_empty_when_nothing_enabled(self, tmp_path):
        """The default-off state reads no board at all."""
        mgr = ProjectAccessSettingManager(data_dir=tmp_path)
        assert mgr.enabled_project_ids() == []

    def test_survives_a_reload_from_disk(self, tmp_path):
        """JSON object keys are strings; they must still come back as ints
        so the provider can pass them to Kanboard's API."""
        ProjectAccessSettingManager(data_dir=tmp_path).set_project_enabled(8, True)
        reloaded = ProjectAccessSettingManager(data_dir=tmp_path)
        assert reloaded.enabled_project_ids() == [8]

    def test_ignores_a_corrupt_key(self, tmp_path):
        """A hand-edited file with a junk key must not break scoping."""
        mgr = ProjectAccessSettingManager(data_dir=tmp_path)
        mgr.set_project_enabled(8, True)
        mgr._data["projects"]["oops"] = {"enabled": True}
        assert mgr.enabled_project_ids() == [8]


class TestSaveAtomicity:
    """_save() used to write self._path directly — this ONE file is the
    master allowlist for EVERY project. A process killed mid-write left
    a truncated/invalid file; _load's broad except-and-reset-to-empty on
    the next load would then silently disable every project Marcus was
    enabled for, not just the one being changed — a crash while
    disabling project B taking project A offline too."""

    def test_failed_write_does_not_corrupt_existing_file(self, tmp_path):
        mgr = ProjectAccessSettingManager(data_dir=tmp_path)
        mgr.set_project_enabled(1, True)
        original_content = (tmp_path / "project_access_settings.json").read_text()

        mgr._data["projects"]["2"] = {"enabled": object()}
        mgr._save()  # swallows the exception, logs an error

        assert (
            tmp_path / "project_access_settings.json"
        ).read_text() == original_content
        assert not (tmp_path / "project_access_settings.json.tmp").exists()

    def test_successful_save_leaves_no_temp_file(self, tmp_path):
        mgr = ProjectAccessSettingManager(data_dir=tmp_path)
        mgr.set_project_enabled(1, True)

        assert (tmp_path / "project_access_settings.json").exists()
        assert not (tmp_path / "project_access_settings.json.tmp").exists()

    def test_a_project_b_crash_does_not_disable_project_a(self, tmp_path):
        """The concrete two-project failure scenario: a poisoned write
        for project B must never be able to revert project A's
        already-saved enabled state."""
        mgr = ProjectAccessSettingManager(data_dir=tmp_path)
        mgr.set_project_enabled(1, True)  # project A, saved successfully

        mgr._data["projects"]["2"] = {"enabled": object()}  # unsavable
        mgr._save()  # project B's poisoned write fails

        reloaded = ProjectAccessSettingManager(data_dir=tmp_path)
        assert reloaded.is_enabled(1) is True  # project A still enabled
