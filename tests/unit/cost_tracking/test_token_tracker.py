"""
Unit tests for src.cost_tracking.token_tracker.

TokenTracker is a lightweight, in-memory/JSON-persisted cost tracker used
by ai_usage_middleware.py and ai_analysis_engine.py for real-time spend
visibility — distinct from the authoritative, versioned per-model pricing
in src/cost_tracking/cost_store.py. These tests verify that track_tokens
actually uses the model string it's given to pick a per-model rate,
rather than a single flat rate for every model.
"""

from __future__ import annotations

import pytest

from src.cost_tracking.token_tracker import TokenTracker, _rate_for_model

pytestmark = pytest.mark.unit


@pytest.fixture
def tracker(tmp_path):
    """TokenTracker with its persistence file redirected to a tmp dir."""
    t = TokenTracker(cost_per_1k_tokens=0.03)
    t.data_file = tmp_path / "token_usage.json"
    return t


class TestRateForModel:
    """_rate_for_model: the lookup table track_tokens now consults."""

    def test_unrecognized_model_falls_back_to_default_rate(self):
        assert _rate_for_model("some-future-model", 0.03) == (0.03, 0.03)

    def test_haiku_is_far_cheaper_than_opus(self):
        haiku_in, haiku_out = _rate_for_model("claude-3-haiku-20240307", 0.03)
        opus_in, opus_out = _rate_for_model("claude-3-opus-20240229", 0.03)
        assert haiku_in < opus_in
        assert haiku_out < opus_out

    def test_gpt_4o_does_not_match_the_plain_gpt_4_row(self):
        """"gpt-4" is a substring of "gpt-4o" — the more specific entry
        must be checked first or gpt-4o silently gets gpt-4's rate."""
        assert _rate_for_model("gpt-4o", 0.03) != _rate_for_model("gpt-4", 0.03)


class TestTrackTokensUsesModelAwareCost:
    """Regression (confirmed finding #38): track_tokens accepted a
    `model` parameter but never used it in the cost formula — every
    call was billed at the single flat self.cost_per_1k_tokens rate
    regardless of which (far cheaper or far more expensive) model was
    actually used."""

    @pytest.mark.asyncio
    async def test_haiku_and_opus_calls_with_identical_tokens_cost_differently(
        self, tracker
    ):
        haiku_stats = await tracker.track_tokens(
            "proj-haiku", input_tokens=1000, output_tokens=1000,
            model="claude-3-haiku-20240307",
        )
        opus_stats = await tracker.track_tokens(
            "proj-opus", input_tokens=1000, output_tokens=1000,
            model="claude-3-opus-20240229",
        )

        assert haiku_stats["total_cost"] < opus_stats["total_cost"]
        # Roughly 60x cheaper for an equal input/output split — not just
        # "slightly different", proving a real per-model rate is used.
        assert opus_stats["total_cost"] > haiku_stats["total_cost"] * 10

    @pytest.mark.asyncio
    async def test_unrecognized_model_matches_old_flat_rate_behavior(
        self, tracker
    ):
        """An unrecognized model must still cost exactly what the old
        flat-rate formula produced — the fix must not change behavior
        for models it doesn't recognize."""
        stats = await tracker.track_tokens(
            "proj-x", input_tokens=500, output_tokens=500,
            model="some-future-model",
        )
        expected = (1000 / 1000) * tracker.cost_per_1k_tokens
        assert stats["total_cost"] == round(expected, 4)

    @pytest.mark.asyncio
    async def test_input_and_output_tokens_priced_independently(self, tracker):
        """Output tokens are typically priced higher than input tokens
        for the same model — an all-output call must cost more than an
        all-input call with the same token count."""
        input_heavy = await tracker.track_tokens(
            "proj-in", input_tokens=10_000, output_tokens=0,
            model="claude-3-opus-20240229",
        )
        output_heavy = await tracker.track_tokens(
            "proj-out", input_tokens=0, output_tokens=10_000,
            model="claude-3-opus-20240229",
        )
        assert output_heavy["total_cost"] > input_heavy["total_cost"]
