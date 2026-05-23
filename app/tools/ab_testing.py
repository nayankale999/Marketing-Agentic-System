"""`ab.testing` tool — significance + lift + decision (W36, E11-S03).

The math lives here so neither the agent prompts nor the API layer have
to reason about it. Rate metrics (open rate, click rate, conversion rate)
use a two-proportion z-test; continuous metrics (revenue per recipient,
session length) use Welch's t-test. Pure-Python: `math.erf` powers the
normal CDF, no scipy dependency.

Determinism is an AC (E11-S03 #4) — given inputs, the outputs are
mechanical and stable.

Tool contract:
  Inputs:
    {
      "arm_a": {"n": int, "x": float, "mean"?: float, "variance"?: float},
      "arm_b": {"n": int, "x": float, "mean"?: float, "variance"?: float},
      "metric_kind": "rate" | "continuous",
      "confidence": 0.0..1.0   # default 0.95
    }
  For rate metrics: `x` is the count of successes (clicks, opens). For
  continuous metrics: `mean` and `variance` are required; `x` is the
  number of observations (same as `n`, kept for symmetry).

  Outputs:
    {
      "p_value": float,
      "lift": float,                 # (b - a) / a, or absolute delta for continuous
      "confidence_interval": [low, high],   # of the lift
      "decision": "winner" | "loser" | "inconclusive",
      "winner_arm"?: "a" | "b",
      "min_n_for_power"?: int        # when decision == inconclusive
    }
"""

from __future__ import annotations

import math
from typing import Any, ClassVar

from app.tools.base import Tool


# ---------------------------------------------------------------------------
# Math primitives — kept private so the surface area stays tight.
# ---------------------------------------------------------------------------


def _normal_cdf(z: float) -> float:
    """Standard-normal CDF via erf — accurate to ~1e-7."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _z_critical(confidence: float) -> float:
    """Two-tailed z critical value via the inverse-CDF approximation
    (Beasley-Springer-Moro). Confidence is e.g. 0.95 → returns ~1.96.

    We don't need 1e-9 precision; this approximation is well under 1e-4
    on the [0.8, 0.999] confidence range.
    """
    alpha = 1.0 - confidence
    p = 1.0 - alpha / 2.0
    return _inv_norm_cdf(p)


def _inv_norm_cdf(p: float) -> float:
    """Inverse standard-normal CDF (Beasley-Springer-Moro)."""
    if not (0.0 < p < 1.0):
        raise ValueError("p must be in (0, 1)")
    # Acklam's coefficients give good accuracy across the full range.
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    ]

    p_low = 0.02425
    p_high = 1 - p_low
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (
            ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
        ) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
        )
    q = math.sqrt(-2 * math.log(1 - p))
    return -(
        ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
    ) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)


def _two_tailed_p_from_z(z: float) -> float:
    return 2.0 * (1.0 - _normal_cdf(abs(z)))


# ---------------------------------------------------------------------------
# Sample size needed to detect the observed lift with 80% power
# ---------------------------------------------------------------------------


def _min_n_for_proportion_power(
    p_a: float, p_b: float, *, confidence: float, power: float = 0.8
) -> int:
    """Pooled-variance approximation. Returns the per-arm sample size
    needed to detect the observed lift with the given power."""
    if p_a == p_b or p_a in (0.0, 1.0) and p_b == p_a:
        # No detectable difference (or both at the boundary).
        return 10_000_000
    z_alpha = _z_critical(confidence)
    z_beta = _inv_norm_cdf(power)
    pooled = (p_a + p_b) / 2.0
    numerator = (
        z_alpha * math.sqrt(2 * pooled * (1 - pooled))
        + z_beta * math.sqrt(p_a * (1 - p_a) + p_b * (1 - p_b))
    ) ** 2
    denom = (p_a - p_b) ** 2
    if denom == 0:
        return 10_000_000
    return max(1, int(math.ceil(numerator / denom)))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _two_proportion_test(
    *,
    n_a: int,
    x_a: float,
    n_b: int,
    x_b: float,
    confidence: float,
) -> dict[str, Any]:
    if n_a <= 0 or n_b <= 0:
        return _inconclusive(reason="zero_sample")
    p_a = x_a / n_a
    p_b = x_b / n_b
    pooled = (x_a + x_b) / (n_a + n_b)
    denom = math.sqrt(max(pooled * (1.0 - pooled) * (1.0 / n_a + 1.0 / n_b), 0.0))
    if denom == 0:
        # Either both arms 0% or both 100%. No detectable effect.
        return _inconclusive(
            reason="degenerate",
            extra={
                "p_value": 1.0,
                "lift": 0.0,
                "confidence_interval": [0.0, 0.0],
            },
        )

    z = (p_b - p_a) / denom
    p_value = _two_tailed_p_from_z(z)
    z_alpha = _z_critical(confidence)

    # Unpooled SE for the CI (more honest about per-arm variance).
    se_unpooled = math.sqrt(
        p_a * (1.0 - p_a) / n_a + p_b * (1.0 - p_b) / n_b
    )
    delta = p_b - p_a
    ci_low = delta - z_alpha * se_unpooled
    ci_high = delta + z_alpha * se_unpooled

    lift = (p_b - p_a) / p_a if p_a > 0 else float("inf") if p_b > 0 else 0.0

    significant = p_value < (1.0 - confidence) and z != 0
    if significant:
        winner_arm = "b" if p_b > p_a else "a"
        return {
            "p_value": p_value,
            "lift": lift,
            "confidence_interval": [ci_low, ci_high],
            "decision": "winner",
            "winner_arm": winner_arm,
        }

    # Not significant — check whether we even have enough samples to call
    # this with the given power.
    min_n = _min_n_for_proportion_power(p_a, p_b, confidence=confidence)
    if n_a < min_n or n_b < min_n:
        return {
            "p_value": p_value,
            "lift": lift,
            "confidence_interval": [ci_low, ci_high],
            "decision": "inconclusive",
            "min_n_for_power": min_n,
        }
    # Powered + still not significant → call it a loser (no detectable
    # effect at the configured confidence with adequate N).
    return {
        "p_value": p_value,
        "lift": lift,
        "confidence_interval": [ci_low, ci_high],
        "decision": "loser",
    }


def _welch_t_test(
    *,
    n_a: int,
    mean_a: float,
    var_a: float,
    n_b: int,
    mean_b: float,
    var_b: float,
    confidence: float,
) -> dict[str, Any]:
    if n_a < 2 or n_b < 2:
        return _inconclusive(reason="too_few_observations")
    se = math.sqrt(var_a / n_a + var_b / n_b)
    if se == 0:
        return _inconclusive(
            reason="zero_variance",
            extra={
                "p_value": 1.0,
                "lift": mean_b - mean_a,
                "confidence_interval": [0.0, 0.0],
            },
        )

    t = (mean_b - mean_a) / se
    # Welch-Satterthwaite df.
    df_num = (var_a / n_a + var_b / n_b) ** 2
    df_den = (var_a / n_a) ** 2 / (n_a - 1) + (var_b / n_b) ** 2 / (n_b - 1)
    df = df_num / df_den if df_den > 0 else max(n_a, n_b)
    # For df >> 30 the t distribution converges to normal; use that
    # approximation here to avoid pulling in a t-CDF implementation. The
    # error is < 5% for df >= 10 at 95% confidence, which is acceptable
    # for the MVP decision boundary.
    p_value = _two_tailed_p_from_z(t)
    z_alpha = _z_critical(confidence)
    delta = mean_b - mean_a
    ci_low = delta - z_alpha * se
    ci_high = delta + z_alpha * se

    significant = p_value < (1.0 - confidence) and t != 0
    if significant:
        winner_arm = "b" if mean_b > mean_a else "a"
        return {
            "p_value": p_value,
            "lift": delta,
            "confidence_interval": [ci_low, ci_high],
            "decision": "winner",
            "winner_arm": winner_arm,
            "degrees_of_freedom": df,
        }
    return {
        "p_value": p_value,
        "lift": delta,
        "confidence_interval": [ci_low, ci_high],
        "decision": "inconclusive" if max(n_a, n_b) < 30 else "loser",
        "degrees_of_freedom": df,
    }


def _inconclusive(*, reason: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    base: dict[str, Any] = {
        "p_value": 1.0,
        "lift": 0.0,
        "confidence_interval": [0.0, 0.0],
        "decision": "inconclusive",
        "reason": reason,
    }
    if extra:
        base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class AbTestingTool(Tool):
    name: ClassVar[str] = "ab.testing"
    description: ClassVar[str] = (
        "Compute statistical significance, lift, and decision for an A/B "
        "test arm pair. Rate metrics use a two-proportion z-test; "
        "continuous metrics use Welch's t-test."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "arm_a": {"type": "object"},
            "arm_b": {"type": "object"},
            "metric_kind": {"enum": ["rate", "continuous"]},
            "confidence": {
                "type": "number",
                "minimum": 0.5,
                "maximum": 0.9999,
                "default": 0.95,
            },
        },
        "required": ["arm_a", "arm_b", "metric_kind"],
    }
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "p_value": {"type": "number"},
            "lift": {"type": "number"},
            "confidence_interval": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 2,
                "maxItems": 2,
            },
            "decision": {"enum": ["winner", "loser", "inconclusive"]},
            "winner_arm": {"enum": ["a", "b"]},
            "min_n_for_power": {"type": "integer"},
        },
        "required": ["p_value", "lift", "confidence_interval", "decision"],
    }

    async def call(self, inputs: dict[str, Any]) -> dict[str, Any]:
        arm_a = inputs.get("arm_a") or {}
        arm_b = inputs.get("arm_b") or {}
        metric_kind = inputs.get("metric_kind", "rate")
        confidence = float(inputs.get("confidence", 0.95))

        if metric_kind == "rate":
            return _two_proportion_test(
                n_a=int(arm_a.get("n", 0)),
                x_a=float(arm_a.get("x", 0)),
                n_b=int(arm_b.get("n", 0)),
                x_b=float(arm_b.get("x", 0)),
                confidence=confidence,
            )
        if metric_kind == "continuous":
            return _welch_t_test(
                n_a=int(arm_a.get("n", 0)),
                mean_a=float(arm_a.get("mean", 0.0)),
                var_a=float(arm_a.get("variance", 0.0)),
                n_b=int(arm_b.get("n", 0)),
                mean_b=float(arm_b.get("mean", 0.0)),
                var_b=float(arm_b.get("variance", 0.0)),
                confidence=confidence,
            )
        return _inconclusive(reason=f"unsupported_metric_kind:{metric_kind}")


__all__ = ["AbTestingTool"]
