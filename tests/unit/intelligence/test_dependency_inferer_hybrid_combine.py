"""
Unit tests for HybridDependencyInferer._combine_dependencies.

Regression (confirmed finding #41): after _clean_dependencies() runs
(dedup, circular-dependency removal, transitive-dependency removal), the
cleaned list's length and order can differ from the pre-cleaning list —
removal is exactly what that step exists to do. The old code re-attached
each cleaned dependency's hybrid-only metadata (inference_method,
pattern_confidence, ai_confidence, ai_reasoning) by POSITION
(`final_deps_list[i]`), silently misattaching one dependency's metadata
onto a completely unrelated task pair the moment anything earlier in the
list was removed.
"""

from __future__ import annotations

import pytest

from src.intelligence.dependency_inferer_hybrid import (
    HybridDependency,
    HybridDependencyInferer,
)

pytestmark = pytest.mark.unit


class TestCombineDependenciesMetadataAttachment:
    @pytest.mark.asyncio
    async def test_metadata_follows_its_own_dependency_after_a_removal(self) -> None:
        """Three dependencies: A and B form a 2-node cycle (X<->Y) where
        A has much lower confidence than B, so _clean_dependencies drops
        A and keeps B. C is an unrelated pair placed after A and B in
        list order. After cleaning, B and C must each carry their OWN
        hybrid metadata — not A's (for B) or B's (for C), which is what
        positional re-indexing after A's removal would produce."""
        inferer = HybridDependencyInferer()

        # A: Y depends on X — low effective confidence via the "both
        # agree" merge path (no individual threshold gate), so it is
        # weak enough to be the one removed when breaking the cycle.
        dep_a = HybridDependency(
            dependent_task_id="Y",
            dependency_task_id="X",
            dependency_type="soft",
            confidence=0.1,
            reasoning="weak pattern match",
            source="pattern_matching",
            inference_method="pattern",
        )
        ai_a = HybridDependency(
            dependent_task_id="Y",
            dependency_task_id="X",
            dependency_type="soft",
            confidence=0.1,
            reasoning="weak ai guess",
            source="ai_inference",
            inference_method="ai",
            ai_confidence=0.1,
            ai_reasoning="weak ai guess",
        )

        # B: X depends on Y — the other half of the cycle, high
        # confidence, with a distinguishing metadata marker.
        dep_b = HybridDependency(
            dependent_task_id="X",
            dependency_task_id="Y",
            dependency_type="hard",
            confidence=0.95,
            reasoning="strong pattern match for B",
            source="pattern_matching",
            inference_method="pattern_B_marker",
            pattern_confidence=0.42,
        )

        # C: unrelated pair, placed after A and B, with its own distinct
        # marker.
        dep_c = HybridDependency(
            dependent_task_id="Q",
            dependency_task_id="P",
            dependency_type="hard",
            confidence=0.85,
            reasoning="strong pattern match for C",
            source="pattern_matching",
            inference_method="pattern_C_marker",
            pattern_confidence=0.99,
        )

        pattern_deps = {
            ("Y", "X"): dep_a,
            ("X", "Y"): dep_b,
            ("Q", "P"): dep_c,
        }
        ai_deps = {("Y", "X"): ai_a}

        result = await inferer._combine_dependencies(pattern_deps, ai_deps, [])

        by_pair = {(d.dependent_task_id, d.dependency_task_id): d for d in result}

        # A was the weakest link in the cycle and must be gone.
        assert ("Y", "X") not in by_pair

        assert "X" in [d.dependent_task_id for d in result]
        dep_b_result = by_pair[("X", "Y")]
        assert dep_b_result.inference_method == "pattern_B_marker"
        assert dep_b_result.pattern_confidence == 0.42

        dep_c_result = by_pair[("Q", "P")]
        assert dep_c_result.inference_method == "pattern_C_marker"
        assert dep_c_result.pattern_confidence == 0.99
