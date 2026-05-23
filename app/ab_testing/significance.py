"""A/B test significance evaluator (W36, E09-S03).

`evaluate_test` is the one entry point. It pulls per-arm denominators
(audience exposed via the assignment table) and numerators (analytic
events of the configured primary metric) for each variant, calls the
`ab.testing` tool, and applies the E09-S03 decision logic to update
`ab_test.status`, `winner_id`, `confidence`, `lift`.

Throttle: at most one effective evaluation every 15 minutes per test
(AC #1). The `last_evaluated_at` column is the throttle anchor; a call
inside the window short-circuits with the current snapshot.

Manual stops win: tests in `stopped` are left alone (AC #4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import (
    AbTestStatus,
    EventKind,
)
from app.db.models import (
    AbTest,
    AbTestAssignment,
    AnalyticEvent,
    DispatchAttempt,
)
from app.tools.ab_testing import AbTestingTool


# AC #1: recompute at most every 15 minutes per test.
EVAL_THROTTLE = timedelta(minutes=15)

# `primary_metric` is a free-form string on AbTest (per E09-S01). We map
# the common shorthand to EventKind so the evaluator knows which events
# count toward the arm's success column.
_METRIC_TO_EVENT_KIND: dict[str, EventKind] = {
    "open": EventKind.open,
    "open_rate": EventKind.open,
    "click": EventKind.click,
    "click_rate": EventKind.click,
    "ctr": EventKind.click,
    "conversion": EventKind.conversion,
    "conversion_rate": EventKind.conversion,
    "reply": EventKind.reply,
    "unsubscribe": EventKind.unsubscribe,
}


@dataclass(frozen=True)
class EvalResult:
    """What the evaluator did this pass.

    `skipped` is True when we short-circuited (throttle, stopped, etc.) —
    no `ab_test` columns were touched."""

    ab_test_id: UUID
    status: AbTestStatus
    skipped: bool
    skip_reason: str | None = None
    decision: str | None = None
    winner_id: UUID | None = None
    p_value: float | None = None
    lift: float | None = None
    sample_a: tuple[int, int] | None = None  # (n, x)
    sample_b: tuple[int, int] | None = None


async def evaluate_test(
    session: AsyncSession,
    *,
    ab_test_id: UUID,
    now: datetime,
    confidence: float = 0.95,
) -> EvalResult:
    ab_test = await session.get(AbTest, ab_test_id)
    if ab_test is None:
        return EvalResult(
            ab_test_id=ab_test_id,
            status=AbTestStatus.designing,
            skipped=True,
            skip_reason="not_found",
        )

    if ab_test.status in {AbTestStatus.stopped, AbTestStatus.significant}:
        # AC #4: manual stop wins; an already-significant test doesn't
        # need to be re-evaluated either.
        return EvalResult(
            ab_test_id=ab_test_id,
            status=ab_test.status,
            skipped=True,
            skip_reason=f"already_{ab_test.status.value}",
        )

    if ab_test.status != AbTestStatus.running:
        return EvalResult(
            ab_test_id=ab_test_id,
            status=ab_test.status,
            skipped=True,
            skip_reason=f"status_{ab_test.status.value}",
        )

    if (
        ab_test.last_evaluated_at is not None
        and now - ab_test.last_evaluated_at < EVAL_THROTTLE
    ):
        return EvalResult(
            ab_test_id=ab_test_id,
            status=ab_test.status,
            skipped=True,
            skip_reason="throttled",
        )

    # Pull arm samples. We only evaluate 2-arm tests at this layer;
    # multivariate evaluation is a follow-up (winner across N arms can be
    # decomposed into pairwise, but we don't ship that today).
    variant_a = ab_test.variant_a_id
    variant_b = ab_test.variant_b_id
    if variant_a is None or variant_b is None:
        return EvalResult(
            ab_test_id=ab_test_id,
            status=ab_test.status,
            skipped=True,
            skip_reason="single_variant_test",
        )

    event_kind = _METRIC_TO_EVENT_KIND.get(ab_test.primary_metric)
    if event_kind is None:
        return EvalResult(
            ab_test_id=ab_test_id,
            status=ab_test.status,
            skipped=True,
            skip_reason=f"unsupported_metric:{ab_test.primary_metric}",
        )

    sample_a = await _arm_sample(
        session,
        tenant_id=ab_test.tenant_id,
        ab_test_id=ab_test.id,
        variant_id=variant_a,
        event_kind=event_kind,
    )
    sample_b = await _arm_sample(
        session,
        tenant_id=ab_test.tenant_id,
        ab_test_id=ab_test.id,
        variant_id=variant_b,
        event_kind=event_kind,
    )

    # AC #3: if the test has been running past max_runtime and we still
    # don't have a winner, flip to inconclusive.
    max_runtime = ab_test.max_runtime_hours
    timed_out = (
        max_runtime is not None
        and ab_test.started_at is not None
        and (now - ab_test.started_at) >= timedelta(hours=max_runtime)
    )

    tool = AbTestingTool()
    result = await tool.call(
        {
            "arm_a": {"n": sample_a[0], "x": sample_a[1]},
            "arm_b": {"n": sample_b[0], "x": sample_b[1]},
            "metric_kind": "rate",
            "confidence": confidence,
        }
    )

    ab_test.last_evaluated_at = now
    if result.get("lift") is not None:
        try:
            ab_test.lift = Decimal(str(round(float(result["lift"]), 4)))
        except (TypeError, ValueError):
            pass

    decision = result.get("decision")
    winner_id: UUID | None = None
    if decision == "winner":
        winner_arm = result.get("winner_arm")
        winner_id = variant_b if winner_arm == "b" else variant_a
        ab_test.status = AbTestStatus.significant
        ab_test.winner_id = winner_id
        try:
            ab_test.confidence = Decimal(str(round(confidence, 4)))
        except (TypeError, ValueError):
            pass
    elif timed_out:
        ab_test.status = AbTestStatus.inconclusive

    return EvalResult(
        ab_test_id=ab_test.id,
        status=ab_test.status,
        skipped=False,
        decision=decision,
        winner_id=winner_id,
        p_value=result.get("p_value"),
        lift=result.get("lift"),
        sample_a=sample_a,
        sample_b=sample_b,
    )


# ---------------------------------------------------------------------------
# Per-arm sample loader
# ---------------------------------------------------------------------------


async def _arm_sample(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    ab_test_id: UUID,
    variant_id: UUID,
    event_kind: EventKind,
) -> tuple[int, int]:
    """Return (n, x) for one arm.

    n = number of recipients assigned to this variant (the denominator).
        We use `ab_test_assignment` rather than dispatch_attempt so the
        denominator is consistent with the assignment used at send time,
        even if a recipient's send failed.
    x = number of analytic_events of `event_kind` attributable to this
        variant. Attribution chain matches W34's KPI rollup: direct
        `campaign_id` match OR via dispatch_attempt.provider_message_id.
    """
    n = (
        await session.execute(
            select(func.count(AbTestAssignment.id)).where(
                AbTestAssignment.tenant_id == tenant_id,
                AbTestAssignment.ab_test_id == ab_test_id,
                AbTestAssignment.variant_id == variant_id,
            )
        )
    ).scalar_one() or 0

    # Numerator: analytic events of `event_kind` attributed to this
    # variant. Direct attribution via channel_id won't work (channel is
    # per-platform, not per-variant) so we lean on dispatch_attempt's
    # `content_asset_id` linkage via `provider_message_id` and on the
    # event's own `campaign_id` filter to scope the universe.
    sg_message_id = AnalyticEvent.payload["sg_message_id"].astext
    attempt_link = exists(
        select(DispatchAttempt.id).where(
            DispatchAttempt.tenant_id == tenant_id,
            DispatchAttempt.content_asset_id == variant_id,
            DispatchAttempt.provider_message_id == sg_message_id,
        )
    )

    # Note: campaign-only (Plausible/UTM) attribution can't distinguish
    # variant A from variant B for a single touchpoint — Plausible sees
    # the click, not which arm sent it. We rely on the sg_message_id link
    # via dispatch_attempt for per-arm attribution. Adding utm_content
    # tagging per variant is a follow-up.

    x = (
        await session.execute(
            select(func.count(AnalyticEvent.id)).where(
                AnalyticEvent.tenant_id == tenant_id,
                AnalyticEvent.event_type == event_kind,
                attempt_link,
            )
        )
    ).scalar_one() or 0
    return (int(n), int(x))


__all__ = ["evaluate_test", "EvalResult", "EVAL_THROTTLE"]
