"""W36 — `ab.testing` tool (E11-S03).

Tool-level unit tests. Pure math, no DB.
"""

from __future__ import annotations

import pytest

from app.tools.ab_testing import AbTestingTool


# ---------------------------------------------------------------------------
# Rate metric (two-proportion z-test)
# ---------------------------------------------------------------------------


@pytest.fixture
def tool() -> AbTestingTool:
    return AbTestingTool()


async def test_rate_clear_winner_b(tool: AbTestingTool) -> None:
    # Arm B is clearly better: 25% vs 15% across 1000 each → p << 0.05.
    result = await tool.call(
        {
            "arm_a": {"n": 1000, "x": 150},
            "arm_b": {"n": 1000, "x": 250},
            "metric_kind": "rate",
            "confidence": 0.95,
        }
    )
    assert result["decision"] == "winner"
    assert result["winner_arm"] == "b"
    assert result["p_value"] < 0.05
    # Lift = (0.25 - 0.15) / 0.15 ≈ 0.667
    assert result["lift"] == pytest.approx(0.6667, abs=1e-3)


async def test_rate_clear_winner_a(tool: AbTestingTool) -> None:
    result = await tool.call(
        {
            "arm_a": {"n": 1000, "x": 250},
            "arm_b": {"n": 1000, "x": 150},
            "metric_kind": "rate",
        }
    )
    assert result["decision"] == "winner"
    assert result["winner_arm"] == "a"


async def test_rate_inconclusive_when_underpowered(tool: AbTestingTool) -> None:
    # Tiny sample with a small effect → can't reach significance, but
    # the math knows what N you'd need.
    result = await tool.call(
        {
            "arm_a": {"n": 50, "x": 10},
            "arm_b": {"n": 50, "x": 12},
            "metric_kind": "rate",
            "confidence": 0.95,
        }
    )
    assert result["decision"] == "inconclusive"
    assert result["min_n_for_power"] > 50


async def test_rate_degenerate_both_zero(tool: AbTestingTool) -> None:
    result = await tool.call(
        {
            "arm_a": {"n": 100, "x": 0},
            "arm_b": {"n": 100, "x": 0},
            "metric_kind": "rate",
        }
    )
    assert result["decision"] == "inconclusive"
    assert result["lift"] == 0.0


async def test_rate_zero_sample_returns_inconclusive(tool: AbTestingTool) -> None:
    result = await tool.call(
        {
            "arm_a": {"n": 0, "x": 0},
            "arm_b": {"n": 100, "x": 50},
            "metric_kind": "rate",
        }
    )
    assert result["decision"] == "inconclusive"


# ---------------------------------------------------------------------------
# Determinism (AC #4)
# ---------------------------------------------------------------------------


async def test_repeat_calls_match_exactly(tool: AbTestingTool) -> None:
    payload = {
        "arm_a": {"n": 1234, "x": 210},
        "arm_b": {"n": 1234, "x": 277},
        "metric_kind": "rate",
        "confidence": 0.95,
    }
    first = await tool.call(payload)
    second = await tool.call(payload)
    third = await tool.call(payload)
    assert first == second == third


# ---------------------------------------------------------------------------
# Continuous (Welch's t-test)
# ---------------------------------------------------------------------------


async def test_continuous_clear_winner(tool: AbTestingTool) -> None:
    result = await tool.call(
        {
            "arm_a": {"n": 500, "mean": 10.0, "variance": 4.0},
            "arm_b": {"n": 500, "mean": 11.0, "variance": 4.0},
            "metric_kind": "continuous",
            "confidence": 0.95,
        }
    )
    assert result["decision"] == "winner"
    assert result["winner_arm"] == "b"
    assert result["lift"] == pytest.approx(1.0, abs=1e-3)


async def test_continuous_too_few_observations(tool: AbTestingTool) -> None:
    result = await tool.call(
        {
            "arm_a": {"n": 1, "mean": 10.0, "variance": 4.0},
            "arm_b": {"n": 1, "mean": 11.0, "variance": 4.0},
            "metric_kind": "continuous",
        }
    )
    assert result["decision"] == "inconclusive"


async def test_unsupported_metric_kind(tool: AbTestingTool) -> None:
    result = await tool.call(
        {
            "arm_a": {"n": 100, "x": 10},
            "arm_b": {"n": 100, "x": 10},
            "metric_kind": "rank",
        }
    )
    assert result["decision"] == "inconclusive"
    assert "unsupported_metric_kind" in result["reason"]
