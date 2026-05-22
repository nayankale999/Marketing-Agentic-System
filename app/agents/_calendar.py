"""Sequence-calendar generator for the Strategist (W21, E05-S03 + E05-S05 #2).

Pure functions, no DB. The caller builds a list of `PlannedTouchpoint`
dataclasses from a strategy proposal payload + audience + hard-cap rules,
then persists them as `strategy_touchpoint` rows.

Three primitives:

  * `generate_calendar` — turn a proposal payload into a list of touchpoints
    spread across the campaign window, weighted by channel allocation.
  * `detect_frequency_warnings` — soft signal: >cap touches in a rolling
    window for the same audience → flag the offenders.
  * `enforce_hard_caps` — strict validation: raises `HardCapViolationError`
    listing every breach. Called by the generator (sanity check) and by the
    drag endpoint (rejects the move).

Touch-count formula per channel:
    touches = max(1, round(allocation_pct / 100 * duration_days / 7))

i.e. roughly one touch per campaign-week, weighted by channel share. A 4-week
campaign with email at 60% allocation gets ~2 emails; the same campaign with
email at 10% gets ~1 (the floor). Tuned via the constants below if needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

_TOUCH_PER_WEEK_FLOOR = 1
_DEFAULT_FREQUENCY_CAP = 3
_DEFAULT_FREQUENCY_WINDOW = timedelta(days=7)
_HARD_CAP_WINDOWS = {
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
}


class HardCapViolationError(Exception):
    """Raised when a generated/edited calendar violates a tenant hard cap."""

    def __init__(self, violations: list[dict[str, Any]]) -> None:
        self.violations = violations
        super().__init__(
            "hard cap violations: " + "; ".join(v["detail"] for v in violations)
        )


@dataclass
class PlannedTouchpoint:
    """One scheduled touch. Mutable on purpose — generator + warning-detector
    populate `frequency_warning` after the schedule is laid down."""

    channel_platform: str
    audience_id: UUID
    scheduled_at: datetime
    position: int = 0
    human_override: bool = False
    frequency_warning: dict[str, Any] | None = None


def generate_calendar(
    *,
    proposal_payload: dict[str, Any],
    start_date: date,
    end_date: date,
    audience_id: UUID,
    hard_caps: list[dict[str, Any]] | None = None,
) -> list[PlannedTouchpoint]:
    """Lay out touchpoints for an accepted proposal.

    Hard caps influence per-channel spacing — a `5/week` cap on email forces
    a minimum 7/5 ≈ 1.4-day gap between email touches. After laying down the
    grid we run `enforce_hard_caps` as a safety net; it should pass when the
    cap is satisfiable, and raise when the campaign window is too short to
    fit the required touch count under the cap."""
    duration_days = max(1, (end_date - start_date).days + 1)
    channels = proposal_payload.get("channels", [])
    caps = hard_caps or []
    cap_by_platform = _index_hard_caps(caps)

    out: list[PlannedTouchpoint] = []
    position_cursor = 0

    for ch in channels:
        if not isinstance(ch, dict):
            continue
        platform = str(ch.get("platform", "")).strip().lower()
        if not platform:
            continue
        try:
            allocation_pct = Decimal(str(ch.get("allocation_pct", 0)))
        except (ArithmeticError, TypeError, ValueError):
            allocation_pct = Decimal("0")
        if allocation_pct <= 0:
            continue

        touch_count = max(
            _TOUCH_PER_WEEK_FLOOR,
            int(round(float(allocation_pct) / 100.0 * duration_days / 7.0)),
        )

        cap = cap_by_platform.get(platform)
        if cap is not None:
            # If the cap forces a minimum interval, derive the max touches
            # fitting in the window — never overschedule.
            cap_max = _max_touches_for_cap(cap, duration_days)
            touch_count = min(touch_count, cap_max)

        touch_count = max(1, touch_count)
        touches = _distribute(
            count=touch_count,
            start_date=start_date,
            end_date=end_date,
            platform=platform,
            audience_id=audience_id,
            position_start=position_cursor,
        )
        position_cursor += len(touches)
        out.extend(touches)

    out.sort(key=lambda t: (t.scheduled_at, t.position))

    # Sanity check — if generation produced a violation despite the per-channel
    # cap-aware count, the calendar is genuinely infeasible. Surface it.
    enforce_hard_caps(out, caps)
    return out


def detect_frequency_warnings(
    touchpoints: list[PlannedTouchpoint],
    *,
    cap: int = _DEFAULT_FREQUENCY_CAP,
    window: timedelta = _DEFAULT_FREQUENCY_WINDOW,
) -> None:
    """In-place: set `frequency_warning` on each touchpoint that sits inside
    a >cap rolling window for its audience. Idempotent — clears stale warnings
    before re-evaluating, so callers can safely re-run after a drag."""
    for tp in touchpoints:
        tp.frequency_warning = None

    by_audience: dict[UUID, list[PlannedTouchpoint]] = {}
    for tp in touchpoints:
        by_audience.setdefault(tp.audience_id, []).append(tp)

    for audience_touches in by_audience.values():
        audience_touches.sort(key=lambda t: t.scheduled_at)
        for i, anchor in enumerate(audience_touches):
            in_window = [
                t
                for t in audience_touches[max(0, i - cap * 2) : i + cap * 2 + 1]
                if abs(t.scheduled_at - anchor.scheduled_at) < window
            ]
            if len(in_window) > cap:
                anchor.frequency_warning = {
                    "cap_per_days": int(window.total_seconds() // 86400),
                    "limit": cap,
                    "count_in_window": len(in_window),
                }


def enforce_hard_caps(
    touchpoints: list[PlannedTouchpoint],
    hard_caps: list[dict[str, Any]],
) -> None:
    """Raise `HardCapViolationError` if any cap is breached.

    Sliding-window check: for every `per` window starting on each touch's
    timestamp, count matching-platform touches and compare against `limit`.
    """
    if not hard_caps or not touchpoints:
        return

    violations: list[dict[str, Any]] = []
    for cap in hard_caps:
        platform = str(cap.get("platform", "")).lower()
        per = str(cap.get("per", "")).lower()
        try:
            limit = int(cap.get("limit", 0))
        except (TypeError, ValueError):
            continue
        if not platform or per not in _HARD_CAP_WINDOWS or limit <= 0:
            continue

        window = _HARD_CAP_WINDOWS[per]
        matching = sorted(
            (t for t in touchpoints if t.channel_platform == platform),
            key=lambda t: t.scheduled_at,
        )
        if len(matching) <= limit:
            continue

        # Slide a closed-on-left window across each anchor; if any window
        # contains more than `limit` touches, that's a breach.
        for i, anchor in enumerate(matching):
            count = 1
            for j in range(i + 1, len(matching)):
                if matching[j].scheduled_at - anchor.scheduled_at < window:
                    count += 1
                else:
                    break
            if count > limit:
                violations.append(
                    {
                        "platform": platform,
                        "per": per,
                        "limit": limit,
                        "observed": count,
                        "window_start": anchor.scheduled_at.isoformat(),
                        "detail": (
                            f"{platform}: {count} touches within {per} starting "
                            f"{anchor.scheduled_at.isoformat()} (cap {limit})"
                        ),
                    }
                )
                break  # one breach per cap is enough for the error message

    if violations:
        raise HardCapViolationError(violations)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _index_hard_caps(caps: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for c in caps:
        platform = str(c.get("platform", "")).lower()
        if platform:
            out[platform] = c
    return out


def _max_touches_for_cap(cap: dict[str, Any], duration_days: int) -> int:
    """Upper bound on touches for a given cap over the campaign window."""
    per = str(cap.get("per", "")).lower()
    try:
        limit = int(cap.get("limit", 0))
    except (TypeError, ValueError):
        return 0
    if per not in _HARD_CAP_WINDOWS or limit <= 0:
        return 0
    window_days = max(1, int(_HARD_CAP_WINDOWS[per].total_seconds() // 86400))
    return max(1, (duration_days * limit) // window_days)


def _distribute(
    *,
    count: int,
    start_date: date,
    end_date: date,
    platform: str,
    audience_id: UUID,
    position_start: int,
) -> list[PlannedTouchpoint]:
    """Spread `count` touches evenly across [start_date, end_date], all at
    09:00 UTC of the chosen day (a sensible default — Distribution can shift
    per-touch later)."""
    duration_days = max(1, (end_date - start_date).days + 1)
    out: list[PlannedTouchpoint] = []
    if count == 1:
        offsets = [duration_days // 2]
    else:
        # Place touch i at day = round(i * (duration-1) / (count-1)); first
        # touch on start_date, last on end_date.
        offsets = [round(i * (duration_days - 1) / (count - 1)) for i in range(count)]

    for idx, day_offset in enumerate(offsets):
        when = datetime.combine(
            start_date + timedelta(days=day_offset),
            time(hour=9, minute=0),
        )
        out.append(
            PlannedTouchpoint(
                channel_platform=platform,
                audience_id=audience_id,
                scheduled_at=when,
                position=position_start + idx,
            )
        )
    return out


__all__ = [
    "HardCapViolationError",
    "PlannedTouchpoint",
    "detect_frequency_warnings",
    "enforce_hard_caps",
    "generate_calendar",
]
