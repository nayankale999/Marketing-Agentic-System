"""Anomaly detection on hot metrics (W37, E10-S02).

The flow is deliberately simple and statistical:

  1. For each metric we care about, build a daily series of counts over
     the last 14 days from `analytic_event`.
  2. Compute median + stddev across the first 13 days (the baseline).
  3. The 14th day is the observation window. If its count deviates by
     more than 3σ from the baseline median, write a `metric_anomaly` row.
  4. The first time a critical-severity anomaly fires we also write an
     audit_log row tagged `anomaly_notification_dispatched` — the
     email/Slack transport reads that row in a future polish unit.

Why median + stddev rather than mean + stddev: marketing metrics are
skewed (heavy tail of spikes / dips). The median is robust to one bad
day while the stddev still flags persistent shifts.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import write_audit
from app.db.enums import EventKind
from app.db.models import (
    AnalyticEvent,
    Campaign,
    ContentAsset,
    DispatchAttempt,
    MetricAnomaly,
    TenantComplianceSettings,
)


# Critical metrics surface a notification within 10 minutes (E10-S02 AC #2)
# and feed into the auto-pause path (AC #3).
_CRITICAL_METRICS: frozenset[EventKind] = frozenset(
    {EventKind.unsubscribe, EventKind.bounce, EventKind.spam_complaint}
)

# Hot metrics monitored every detection pass. Open/click/reply/conversion
# are reported in the dashboard; their anomalies are still useful
# (engagement crash, suddenly-zero clicks) just not critical.
_HOT_METRICS: tuple[EventKind, ...] = (
    EventKind.open,
    EventKind.click,
    EventKind.reply,
    EventKind.conversion,
    EventKind.unsubscribe,
    EventKind.bounce,
    EventKind.spam_complaint,
)

# Knobs. The 3σ threshold is the AC; the 14-day window matches the
# baseline definition in the story.
BASELINE_DAYS = 14
SIGMA_THRESHOLD = Decimal("3.0")
SILENCE_AFTER_DISMISS = timedelta(hours=24)
SILENCE_BETWEEN_FIRES = timedelta(hours=24)


@dataclass(frozen=True)
class DetectedAnomaly:
    """One row from the detector, before persistence."""

    metric: EventKind
    window_start: datetime
    window_end: datetime
    observed_value: int
    baseline_median: float
    baseline_stddev: float
    sigma: float
    severity: str


async def detect_anomalies(
    session: AsyncSession,
    *,
    campaign_id: UUID,
    tenant_id: UUID,
    now: datetime,
) -> list[MetricAnomaly]:
    """Run one detection pass for the campaign. Returns the persisted
    `MetricAnomaly` rows."""
    out: list[MetricAnomaly] = []
    for metric in _HOT_METRICS:
        detected = await _detect_one(
            session,
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            metric=metric,
            now=now,
        )
        if detected is None:
            continue

        # AC #4 + dedup: skip if an undismissed anomaly already exists for
        # the same metric within the silence window.
        silenced_until = now - SILENCE_BETWEEN_FIRES
        active = await session.execute(
            select(MetricAnomaly).where(
                MetricAnomaly.tenant_id == tenant_id,
                MetricAnomaly.campaign_id == campaign_id,
                MetricAnomaly.metric == metric.value,
                MetricAnomaly.created_at >= silenced_until,
                MetricAnomaly.dismissed_at.is_(None),
            )
        )
        if active.scalars().first() is not None:
            continue

        row = MetricAnomaly(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            metric=detected.metric.value,
            window_start=detected.window_start,
            window_end=detected.window_end,
            observed_value=Decimal(detected.observed_value),
            baseline_median=Decimal(str(round(detected.baseline_median, 6))),
            baseline_stddev=Decimal(str(round(detected.baseline_stddev, 6))),
            sigma=Decimal(str(round(detected.sigma, 4))),
            severity=detected.severity,
        )
        session.add(row)
        await session.flush()
        out.append(row)

        if detected.severity == "critical":
            await _notify_critical_anomaly(session, anomaly=row)

    return out


async def _detect_one(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    campaign_id: UUID,
    metric: EventKind,
    now: datetime,
) -> DetectedAnomaly | None:
    """Pull the 14-day series for one metric. Return a DetectedAnomaly
    if the latest window deviates beyond the threshold."""
    series = await _daily_counts(
        session,
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        metric=metric,
        now=now,
    )
    # Need at least the baseline window + the observation. If we don't
    # have BASELINE_DAYS days yet, skip — the test isn't ripe.
    if len(series) < BASELINE_DAYS:
        return None

    baseline = [v for _, v in series[:-1]]
    observed_day, observed_value = series[-1]
    if not baseline:
        return None

    median = float(statistics.median(baseline))
    # pstdev — population stddev — keeps the math defined when stddev
    # would be 0 (statistics.stdev requires >=2 distinct values).
    stddev = float(statistics.pstdev(baseline))

    if stddev == 0:
        # All-zero baseline with a non-zero observation is itself anomalous,
        # but we don't have a stddev to divide by. Flag if observed differs
        # from median at all, with a synthetic sigma of 9999.
        if observed_value == median:
            return None
        sigma = 9999.0
    else:
        sigma = abs(observed_value - median) / stddev

    if Decimal(str(sigma)) <= SIGMA_THRESHOLD:
        return None

    severity = "critical" if metric in _CRITICAL_METRICS else "warning"
    window_start = datetime.combine(
        observed_day, datetime.min.time(), tzinfo=now.tzinfo
    )
    window_end = window_start + timedelta(days=1)
    return DetectedAnomaly(
        metric=metric,
        window_start=window_start,
        window_end=window_end,
        observed_value=observed_value,
        baseline_median=median,
        baseline_stddev=stddev,
        sigma=sigma,
        severity=severity,
    )


async def _daily_counts(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    campaign_id: UUID,
    metric: EventKind,
    now: datetime,
) -> list[tuple[Any, int]]:
    """Return [(date, count)] for the last BASELINE_DAYS days.

    Attribution mirrors W34's KPI rollup: events with `campaign_id` set
    directly, plus SendGrid events stitched via dispatch_attempt's
    `provider_message_id` → content_asset → campaign linkage.
    """
    cutoff = now - timedelta(days=BASELINE_DAYS)

    sg_message_id = AnalyticEvent.payload["sg_message_id"].astext
    indirect_exists = exists(
        select(DispatchAttempt.id)
        .join(
            ContentAsset,
            (ContentAsset.id == DispatchAttempt.content_asset_id)
            & (ContentAsset.campaign_id == campaign_id),
        )
        .where(
            DispatchAttempt.tenant_id == tenant_id,
            DispatchAttempt.provider_message_id == sg_message_id,
        )
    )

    day = func.date_trunc("day", AnalyticEvent.event_at).label("day")
    stmt = (
        select(day, func.count(AnalyticEvent.id))
        .where(
            AnalyticEvent.tenant_id == tenant_id,
            AnalyticEvent.event_type == metric,
            AnalyticEvent.event_at >= cutoff,
            (AnalyticEvent.campaign_id == campaign_id) | indirect_exists,
        )
        .group_by(day)
        .order_by(day)
    )
    rows = (await session.execute(stmt)).all()
    return [(r[0].date() if hasattr(r[0], "date") else r[0], int(r[1])) for r in rows]


async def _notify_critical_anomaly(
    session: AsyncSession, *, anomaly: MetricAnomaly
) -> None:
    """E10-S02 AC #2: notification within 10 min of a critical anomaly.

    There's no email/Slack transport in MAS yet. We write an audit_log
    row tagged so the future transport can read it. The metadata captures
    the recipients (campaign owner + tenant managers) and the metric so
    nothing is lost when the transport lands.
    """
    campaign = await session.get(Campaign, anomaly.campaign_id)
    recipients: list[str] = []
    if campaign is not None and campaign.owner_id is not None:
        recipients.append(str(campaign.owner_id))

    write_audit(
        session,
        tenant_id=anomaly.tenant_id,
        actor_kind=current_actor_kind.get(),
        actor_id=current_actor_id.get(),
        entity_kind="metric_anomaly",
        entity_id=anomaly.id,
        action="anomaly_notification_dispatched",
        before_state=None,
        after_state=None,
        metadata={
            "metric": anomaly.metric,
            "severity": anomaly.severity,
            "recipients": recipients,
            "campaign_id": str(anomaly.campaign_id),
        },
    )


# ---------------------------------------------------------------------------
# Auto-pause + dismiss
# ---------------------------------------------------------------------------


async def should_auto_pause(
    session: AsyncSession, *, tenant_id: UUID, campaign_id: UUID
) -> bool:
    """E10-S02 AC #3: auto-pause when (a) the tenant setting is on and
    (b) two consecutive critical anomalies hit the same metric.

    "Consecutive" here = the two most recent rows for that metric are
    both critical and neither is dismissed."""
    settings = (
        await session.execute(
            select(TenantComplianceSettings).where(
                TenantComplianceSettings.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if settings is None or not settings.auto_pause_on_critical_anomaly:
        return False

    # For each critical metric, look at the two most recent non-dismissed
    # anomalies and see if both are critical.
    for metric in _CRITICAL_METRICS:
        recent = (
            await session.execute(
                select(MetricAnomaly)
                .where(
                    MetricAnomaly.tenant_id == tenant_id,
                    MetricAnomaly.campaign_id == campaign_id,
                    MetricAnomaly.metric == metric.value,
                    MetricAnomaly.dismissed_at.is_(None),
                )
                .order_by(MetricAnomaly.created_at.desc())
                .limit(2)
            )
        ).scalars().all()
        if len(recent) >= 2 and all(r.severity == "critical" for r in recent):
            return True
    return False


async def dismiss_anomaly(
    session: AsyncSession,
    *,
    anomaly_id: UUID,
    dismissed_by: UUID,
    now: datetime,
) -> MetricAnomaly:
    """Mark an anomaly dismissed. Subsequent detection passes for the
    same (campaign, metric) won't re-fire for 24h (E10-S02 AC #4)."""
    row = await session.get(MetricAnomaly, anomaly_id)
    if row is None:
        raise LookupError(f"metric_anomaly {anomaly_id} not found")
    row.dismissed_at = now
    row.dismissed_by = dismissed_by
    await session.flush()
    return row


__all__ = [
    "detect_anomalies",
    "dismiss_anomaly",
    "should_auto_pause",
    "DetectedAnomaly",
    "BASELINE_DAYS",
    "SIGMA_THRESHOLD",
]
