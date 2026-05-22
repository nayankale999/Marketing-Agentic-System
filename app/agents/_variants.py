"""A/B variant helpers for the Content Creator (W23, E06-S05).

Two pieces:

  * `VARIANT_ANGLES` + `angle_for_index` — pre-defined per-variant framing
    injected into the brief so two variants of the same touchpoint take
    genuinely different angles. Going predefined for MVP because the
    similarity check (E06-S05 #4) will catch regressions if the model
    decides to ignore the angle.

  * `parse_ab_test_specs` — pluck `ab_tests` entries off a strategy proposal
    payload and normalise into a `(channel, asset_type_hint, variants)`
    tuple per spec. Tolerant of missing/malformed entries so a buggy planner
    output doesn't crash the seed phase.

Cosine-similarity gating lives in `_compliance.body_cosine_similarity`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Pre-defined variant angles. Lookup is by zero-based index — variant 0 is the
# baseline (no extra angle), variants 1..4 take the angles below. Adding more
# slots is a one-line edit; the agent caps variants at MAX_VARIANTS regardless.
VARIANT_ANGLES: tuple[str, ...] = (
    "lead with social proof — cite customer outcomes or peer adoption",
    "lead with risk reduction — emphasise safety, support, guarantees you can defend",
    "lead with novelty — frame it as something the reader hasn't seen before",
    "lead with urgency — give a concrete reason to act this week, not next quarter",
    "lead with savings — quantify the time or money the reader gets back",
)

MAX_VARIANTS = 5
MIN_VARIANTS = 2
SIMILARITY_THRESHOLD = 0.9  # E06-S05 #4
SIM_REGEN_MAX_RETRIES = 2


@dataclass(frozen=True)
class AbTestSpec:
    """One entry from `strategy_proposal.payload.ab_tests[]`."""

    channel: str
    variants: int


def angle_for_index(index: int) -> str:
    """Return the angle directive for variant N (0-indexed). Variant 0 is
    the baseline — empty string so the brief doesn't get a meaningless
    directive injected. Out-of-range falls back to the last angle."""
    if index <= 0:
        return ""
    if index - 1 < len(VARIANT_ANGLES):
        return VARIANT_ANGLES[index - 1]
    return VARIANT_ANGLES[-1]


def parse_ab_test_specs(payload: dict[str, Any]) -> list[AbTestSpec]:
    """Pull `ab_tests` entries from a strategy proposal payload, normalised.

    Tolerant: any malformed entry is silently dropped rather than blowing up
    the entire content-seed phase. Variants are clamped to [MIN, MAX]."""
    raw = payload.get("ab_tests")
    if not isinstance(raw, list):
        return []
    out: list[AbTestSpec] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        channel = str(entry.get("channel", "")).strip().lower()
        if not channel:
            continue
        try:
            variants = int(entry.get("variants", MIN_VARIANTS))
        except (TypeError, ValueError):
            variants = MIN_VARIANTS
        variants = max(MIN_VARIANTS, min(MAX_VARIANTS, variants))
        out.append(AbTestSpec(channel=channel, variants=variants))
    return out


__all__ = [
    "AbTestSpec",
    "MAX_VARIANTS",
    "MIN_VARIANTS",
    "SIMILARITY_THRESHOLD",
    "SIM_REGEN_MAX_RETRIES",
    "VARIANT_ANGLES",
    "angle_for_index",
    "parse_ab_test_specs",
]
