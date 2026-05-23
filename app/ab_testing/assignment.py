"""Per-recipient A/B variant assignment (W35, E09-S02).

Why this exists: E09-S02 requires the SAME recipient to see the SAME
variant across retries, multi-step touchpoints, and pause/resume cycles.
A random pick at dispatch time would violate that. Hashing the recipient
+ test id into a fixed deterministic bucket does — and the assignment
table makes it durable against `traffic_split` edits (rare, but a
post-launch tweak shouldn't reshuffle who got what).

Flow:
  1. `assign_variant` first tries to read an existing assignment row.
  2. If none, it computes a deterministic bucket via `pick_variant_index`
     against the test's traffic_split.
  3. The insert is ON CONFLICT DO NOTHING — race-safe across concurrent
     dispatch workers.
  4. If the conflict path fires (another worker assigned the same
     recipient first), we re-read and return that.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AbTest, AbTestAssignment


class AbTestAssignmentError(Exception):
    """Raised when the A/B test is mis-configured (empty split, etc.).

    Dispatch callers catch this and fall back to the originally-scheduled
    asset rather than failing the send."""


def pick_variant_index(
    *,
    ab_test_id: UUID,
    audience_external_id: str,
    split: dict[str, int],
) -> str:
    """Deterministic bucket pick.

    The hash is blake2b for speed + good distribution; we don't need
    cryptographic strength, just uniformity. Bucket size is 10000 so
    1% steps in `split` are honoured exactly.
    """
    if not split:
        raise AbTestAssignmentError(
            f"ab_test {ab_test_id} has no traffic_split configured"
        )
    total = sum(split.values())
    if total <= 0:
        raise AbTestAssignmentError(
            f"ab_test {ab_test_id} traffic_split sums to {total}"
        )

    key = f"{ab_test_id}:{audience_external_id}".encode()
    digest = hashlib.blake2b(key, digest_size=8).digest()
    bucket = int.from_bytes(digest, "big") % total

    cumulative = 0
    # Sort by variant_id so the CDF is stable as new variants are added
    # mid-test (a rare but real operation).
    for variant_id, weight in sorted(split.items()):
        cumulative += int(weight)
        if bucket < cumulative:
            return variant_id

    # Fallback only happens if rounding fuzz pushed us past the end —
    # take the last variant.
    return sorted(split.keys())[-1]


async def assign_variant(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    ab_test_id: UUID,
    audience_external_id: str,
) -> UUID:
    """Return the variant id this recipient should see for this test.

    Idempotent — calling twice with the same inputs returns the same
    variant. Tenant context must be set by the caller (or RLS bypassed,
    e.g. for tests)."""
    existing = (
        await session.execute(
            select(AbTestAssignment.variant_id).where(
                AbTestAssignment.tenant_id == tenant_id,
                AbTestAssignment.ab_test_id == ab_test_id,
                AbTestAssignment.audience_external_id == audience_external_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    test = (
        await session.execute(
            select(AbTest).where(
                AbTest.id == ab_test_id,
                AbTest.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if test is None:
        raise AbTestAssignmentError(f"ab_test {ab_test_id} not found")

    split = _coerce_split(test.traffic_split)
    variant_id = UUID(
        pick_variant_index(
            ab_test_id=ab_test_id,
            audience_external_id=audience_external_id,
            split=split,
        )
    )

    stmt = (
        pg_insert(AbTestAssignment)
        .values(
            tenant_id=tenant_id,
            ab_test_id=ab_test_id,
            audience_external_id=audience_external_id,
            variant_id=variant_id,
        )
        .on_conflict_do_nothing(
            constraint="uq_ab_test_assignment_tenant_test_audience"
        )
    )
    await session.execute(stmt)

    # Re-read so we return whatever ended up persisted — own row or the
    # racer's.
    return (
        await session.execute(
            select(AbTestAssignment.variant_id).where(
                AbTestAssignment.tenant_id == tenant_id,
                AbTestAssignment.ab_test_id == ab_test_id,
                AbTestAssignment.audience_external_id == audience_external_id,
            )
        )
    ).scalar_one()


def _coerce_split(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise AbTestAssignmentError("traffic_split must be a JSON object")
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            weight = int(v)
        except (TypeError, ValueError) as exc:
            raise AbTestAssignmentError(
                f"traffic_split[{k!r}] is not an integer"
            ) from exc
        if weight <= 0:
            continue  # ignore zero-weight variants
        out[str(k)] = weight
    return out


__all__ = ["assign_variant", "pick_variant_index", "AbTestAssignmentError"]
