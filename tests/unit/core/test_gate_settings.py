"""
Unit tests for src/core/gate_settings.py
"""

import json
from pathlib import Path

import pytest

from src.core.gate_settings import GateSettingManager


class TestGateSettingManager:
    """Tests for GateSettingManager — gate mode and AI-verify-count settings."""

    @pytest.fixture()
    def mgr(self, tmp_path: Path) -> GateSettingManager:
        """Manager backed by a temp directory."""
        return GateSettingManager(data_dir=tmp_path)

    # ── Gate defaults ────────────────────────────────────────────────────

    def test_get_project_gate_returns_none_when_not_set(self, mgr):
        """No project setting → get_project_gate returns None."""
        assert mgr.get_project_gate(1) is None

    def test_get_ticket_gate_returns_none_when_not_set(self, mgr):
        """No ticket setting → get_ticket_gate returns None."""
        assert mgr.get_ticket_gate("42") is None

    def test_get_effective_gate_defaults_to_human(self, mgr):
        """When nothing is set, effective gate is 'human'."""
        assert mgr.get_effective_gate("99", 5) == "human"

    # ── Gate — project settings ──────────────────────────────────────────

    def test_set_and_get_project_gate_human(self, mgr):
        """set_project_gate then get_project_gate round-trips 'human'."""
        mgr.set_project_gate(1, "human")
        assert mgr.get_project_gate(1) == "human"

    def test_set_and_get_project_gate_ai(self, mgr):
        """set_project_gate then get_project_gate round-trips 'ai'."""
        mgr.set_project_gate(2, "ai")
        assert mgr.get_project_gate(2) == "ai"

    def test_project_gate_persisted_to_disk(self, tmp_path):
        """set_project_gate writes data that survives a new manager instance."""
        mgr1 = GateSettingManager(data_dir=tmp_path)
        mgr1.set_project_gate(3, "ai")

        mgr2 = GateSettingManager(data_dir=tmp_path)
        assert mgr2.get_project_gate(3) == "ai"

    def test_project_gate_overrides_default(self, mgr):
        """Project 'ai' setting overrides the global 'human' default."""
        mgr.set_project_gate(1, "ai")
        assert mgr.get_effective_gate("10", 1) == "ai"

    # ── Gate — ticket settings ───────────────────────────────────────────

    def test_set_and_get_ticket_gate(self, mgr):
        """set_ticket_gate then get_ticket_gate round-trips correctly."""
        mgr.set_ticket_gate("42", "ai")
        assert mgr.get_ticket_gate("42") == "ai"

    def test_ticket_gate_overrides_project_gate(self, mgr):
        """Per-ticket 'ai' overrides project 'human' setting."""
        mgr.set_project_gate(1, "human")
        mgr.set_ticket_gate("10", "ai")
        assert mgr.get_effective_gate("10", 1) == "ai"

    def test_ticket_gate_overrides_project_gate_in_reverse(self, mgr):
        """Per-ticket 'human' overrides project 'ai' setting."""
        mgr.set_project_gate(1, "ai")
        mgr.set_ticket_gate("10", "human")
        assert mgr.get_effective_gate("10", 1) == "human"

    def test_ticket_gate_none_clears_override(self, mgr):
        """set_ticket_gate(None) removes the per-ticket override."""
        mgr.set_project_gate(1, "ai")
        mgr.set_ticket_gate("10", "human")
        mgr.set_ticket_gate("10", None)
        assert mgr.get_effective_gate("10", 1) == "ai"

    def test_ticket_gate_persisted_to_disk(self, tmp_path):
        """set_ticket_gate writes data that survives a new manager instance."""
        mgr1 = GateSettingManager(data_dir=tmp_path)
        mgr1.set_ticket_gate("55", "ai")

        mgr2 = GateSettingManager(data_dir=tmp_path)
        assert mgr2.get_ticket_gate("55") == "ai"

    # ── Gate — effective resolution precedence ───────────────────────────

    def test_effective_precedence_ticket_over_project_over_default(self, mgr):
        """Resolution: ticket → project → default ('human')."""
        mgr.set_project_gate(1, "human")
        mgr.set_ticket_gate("7", "ai")
        assert mgr.get_effective_gate("7", 1) == "ai"
        assert mgr.get_effective_gate("8", 1) == "human"
        assert mgr.get_effective_gate("9", 99) == "human"

    # ── Gate — isolation ─────────────────────────────────────────────────

    def test_settings_isolated_per_project(self, mgr):
        """Project 1 and project 2 have independent settings."""
        mgr.set_project_gate(1, "human")
        mgr.set_project_gate(2, "ai")
        assert mgr.get_project_gate(1) == "human"
        assert mgr.get_project_gate(2) == "ai"

    def test_settings_isolated_per_ticket(self, mgr):
        """Ticket 10 and ticket 20 have independent settings."""
        mgr.set_ticket_gate("10", "human")
        mgr.set_ticket_gate("20", "ai")
        assert mgr.get_ticket_gate("10") == "human"
        assert mgr.get_ticket_gate("20") == "ai"

    # ── Verify count defaults ────────────────────────────────────────────

    def test_get_project_verify_count_returns_none_when_not_set(self, mgr):
        """No project verify_count setting → get_project_verify_count returns None."""
        assert mgr.get_project_verify_count(1) is None

    def test_get_ticket_verify_count_returns_none_when_not_set(self, mgr):
        """No ticket verify_count setting → get_ticket_verify_count returns None."""
        assert mgr.get_ticket_verify_count("42") is None

    def test_get_effective_verify_count_defaults_to_zero(self, mgr):
        """When nothing is set, effective verify_count is 0."""
        assert mgr.get_effective_verify_count("99", 5) == 0

    # ── Verify count — project settings ──────────────────────────────────

    def test_set_and_get_project_verify_count_one(self, mgr):
        """set_project_verify_count(1) then get returns 1."""
        mgr.set_project_verify_count(1, 1)
        assert mgr.get_project_verify_count(1) == 1

    def test_set_and_get_project_verify_count_zero(self, mgr):
        """set_project_verify_count(0) then get returns 0."""
        mgr.set_project_verify_count(1, 0)
        assert mgr.get_project_verify_count(1) == 0

    def test_set_and_get_project_verify_count_three(self, mgr):
        """set_project_verify_count(3) then get returns 3."""
        mgr.set_project_verify_count(1, 3)
        assert mgr.get_project_verify_count(1) == 3

    def test_project_verify_count_persisted_to_disk(self, tmp_path):
        """set_project_verify_count writes data that survives a new manager instance."""
        mgr1 = GateSettingManager(data_dir=tmp_path)
        mgr1.set_project_verify_count(5, 2)

        mgr2 = GateSettingManager(data_dir=tmp_path)
        assert mgr2.get_project_verify_count(5) == 2

    def test_project_verify_count_overrides_default_zero(self, mgr):
        """Project verify_count=3 overrides the global 0 default."""
        mgr.set_project_verify_count(1, 3)
        assert mgr.get_effective_verify_count("10", 1) == 3

    # ── Verify count — ticket settings ───────────────────────────────────

    def test_set_and_get_ticket_verify_count(self, mgr):
        """set_ticket_verify_count then get round-trips correctly."""
        mgr.set_ticket_verify_count("42", 2)
        assert mgr.get_ticket_verify_count("42") == 2

    def test_ticket_verify_count_overrides_project(self, mgr):
        """Per-ticket count=2 overrides project count=0."""
        mgr.set_project_verify_count(1, 0)
        mgr.set_ticket_verify_count("10", 2)
        assert mgr.get_effective_verify_count("10", 1) == 2

    def test_ticket_verify_count_overrides_project_with_zero(self, mgr):
        """Per-ticket count=0 overrides project count=3."""
        mgr.set_project_verify_count(1, 3)
        mgr.set_ticket_verify_count("10", 0)
        assert mgr.get_effective_verify_count("10", 1) == 0

    def test_ticket_verify_count_none_clears_override(self, mgr):
        """set_ticket_verify_count(None) removes the per-ticket override."""
        mgr.set_project_verify_count(1, 3)
        mgr.set_ticket_verify_count("10", 0)
        mgr.set_ticket_verify_count("10", None)
        assert mgr.get_effective_verify_count("10", 1) == 3

    def test_ticket_verify_count_persisted_to_disk(self, tmp_path):
        """set_ticket_verify_count writes data that survives a new manager instance."""
        mgr1 = GateSettingManager(data_dir=tmp_path)
        mgr1.set_ticket_verify_count("55", 2)

        mgr2 = GateSettingManager(data_dir=tmp_path)
        assert mgr2.get_ticket_verify_count("55") == 2

    # ── Verify count — effective resolution precedence ───────────────────

    def test_verify_count_effective_precedence(self, mgr):
        """Verify_count resolution: ticket → project → default (0)."""
        mgr.set_project_verify_count(1, 1)
        mgr.set_ticket_verify_count("7", 3)
        assert mgr.get_effective_verify_count("7", 1) == 3
        assert mgr.get_effective_verify_count("8", 1) == 1
        assert mgr.get_effective_verify_count("9", 99) == 0

    # ── Gate and verify count coexist ────────────────────────────────────

    def test_gate_and_verify_count_stored_together(self, tmp_path):
        """Gate and verify_count can be set independently on the same project."""
        mgr = GateSettingManager(data_dir=tmp_path)
        mgr.set_project_gate(1, "ai")
        mgr.set_project_verify_count(1, 2)
        assert mgr.get_project_gate(1) == "ai"
        assert mgr.get_project_verify_count(1) == 2

    def test_gate_and_verify_count_independent_per_ticket(self, mgr):
        """Setting gate does not affect verify_count and vice versa."""
        mgr.set_ticket_gate("42", "ai")
        mgr.set_ticket_verify_count("42", 2)
        assert mgr.get_ticket_gate("42") == "ai"
        assert mgr.get_ticket_verify_count("42") == 2

    # ── Resilience ────────────────────────────────────────────────────────

    def test_loads_cleanly_when_file_missing(self, tmp_path):
        """No gate_settings.json on disk → manager starts with empty state."""
        mgr = GateSettingManager(data_dir=tmp_path)
        assert mgr.get_project_gate(1) is None
        assert mgr.get_ticket_gate("1") is None
        assert mgr.get_project_verify_count(1) is None

    def test_survives_corrupt_json(self, tmp_path):
        """Corrupt JSON file → manager falls back to empty state."""
        (tmp_path / "gate_settings.json").write_text("NOT JSON {{{{")
        mgr = GateSettingManager(data_dir=tmp_path)
        assert mgr.get_effective_gate("1", 1) == "human"
        assert mgr.get_effective_verify_count("1", 1) == 0

    def test_json_file_structure(self, tmp_path):
        """Saved JSON has 'projects' and 'tickets' keys with nested dicts."""
        mgr = GateSettingManager(data_dir=tmp_path)
        mgr.set_project_gate(1, "ai")
        mgr.set_project_verify_count(1, 2)
        raw = json.loads((tmp_path / "gate_settings.json").read_text())
        assert "projects" in raw
        assert "tickets" in raw
        assert raw["projects"]["1"]["gate"] == "ai"
        assert raw["projects"]["1"]["verify_count"] == 2

    def test_migrates_old_string_format(self, tmp_path):
        """Old format (string value) is transparently migrated on first access."""
        data = {"projects": {"1": "ai"}, "tickets": {"42": "human"}}
        (tmp_path / "gate_settings.json").write_text(json.dumps(data))
        mgr = GateSettingManager(data_dir=tmp_path)
        assert mgr.get_project_gate(1) == "ai"
        assert mgr.get_ticket_gate("42") == "human"

    def test_migrates_old_bool_verify_true_to_count_one(self, tmp_path):
        """Old format verify=true is migrated to verify_count=1."""
        data = {
            "projects": {"1": {"gate": "ai", "verify": True}},
            "tickets": {"42": {"gate": "ai", "verify": True}},
        }
        (tmp_path / "gate_settings.json").write_text(json.dumps(data))
        mgr = GateSettingManager(data_dir=tmp_path)
        assert mgr.get_project_verify_count(1) == 1
        assert mgr.get_ticket_verify_count("42") == 1

    def test_migrates_old_bool_verify_false_to_count_zero(self, tmp_path):
        """Old format verify=false is migrated to verify_count=0."""
        data = {
            "projects": {"1": {"gate": "ai", "verify": False}},
            "tickets": {"42": {"gate": "ai", "verify": False}},
        }
        (tmp_path / "gate_settings.json").write_text(json.dumps(data))
        mgr = GateSettingManager(data_dir=tmp_path)
        assert mgr.get_project_verify_count(1) == 0
        assert mgr.get_ticket_verify_count("42") == 0

    def test_bool_migration_persists_to_disk(self, tmp_path):
        """Bool→int migration is written to disk so it doesn't re-run on next load."""
        data = {"projects": {"1": {"gate": "ai", "verify": True}}, "tickets": {}}
        (tmp_path / "gate_settings.json").write_text(json.dumps(data))
        # First access triggers migration
        mgr1 = GateSettingManager(data_dir=tmp_path)
        _ = mgr1.get_project_verify_count(1)

        # Second instance reads the already-migrated file
        mgr2 = GateSettingManager(data_dir=tmp_path)
        raw = json.loads((tmp_path / "gate_settings.json").read_text())
        assert "verify" not in raw["projects"]["1"]
        assert raw["projects"]["1"]["verify_count"] == 1
