"""Custom KPI evaluator (W41, E10-S07).

A custom KPI is a small JSONB formula over analytic_event:

    {
      "event_type": "click",
      "filters": [
        {"path": "payload.utm_content", "op": "eq", "value": "demo"},
        {"path": "payload.utm_source", "op": "in", "value": ["email", "li"]}
      ],
      "window_days": 7
    }

Why JSONB and not SQL: the formula has to be hand-editable in a tiny UI
and survives round-trips to the report snapshot. A composite "X within
7d of Y" language is a follow-up; for W41 the evaluator counts events
of one type with payload filters.

AC #3 is the subtlety worth flagging: an evaluator that finds no events
must distinguish "this campaign has no events of this type at all"
(return `value=None, missing_event=True`) from "the filtered subset is
empty" (return `value=0`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import EventKind
from app.db.models import (
    AnalyticEvent,
    ContentAsset,
    CustomKpi,
    DispatchAttempt,
)


# Filter operators the evaluator understands. Composite expressions are
# AND-joined at this layer.
_SUPPORTED_OPS: frozenset[str] = frozenset({"eq", "neq", "in", "not_in", "contains"})


@dataclass(frozen=True)
class EvaluationResult:
    value: int | None
    missing_event: bool
    message: str | None = None


async def evaluate_custom_kpi(
    session: AsyncSession,
    *,
    kpi: CustomKpi,
    campaign_id: UUID,
    now: datetime,
) -> EvaluationResult:
    """Return the evaluated count for a campaign + KPI pair. `now` anchors
    the rolling window (`formula.window_days`)."""
    formula = kpi.formula or {}
    raw_event_type = formula.get("event_type")
    try:
        event_kind = EventKind(raw_event_type) if raw_event_type else None
    except ValueError:
        return EvaluationResult(
            value=None,
            missing_event=True,
            message=f"unknown event_type '{raw_event_type}'",
        )
    if event_kind is None:
        return EvaluationResult(
            value=None,
            missing_event=True,
            message="formula.event_type is required",
        )

    # AC #3: distinguish "no events of this kind exist for this campaign"
    # from "filters narrowed to zero". We check the broader bucket first.
    any_of_kind = await _has_any_event_of_kind(
        session,
        tenant_id=kpi.tenant_id,
        campaign_id=campaign_id,
        event_kind=event_kind,
    )
    if not any_of_kind:
        return EvaluationResult(
            value=None,
            missing_event=True,
            message=f"no '{event_kind.value}' events recorded for this campaign",
        )

    # Build the filtered count.
    sg_message_id = AnalyticEvent.payload["sg_message_id"].astext
    indirect_link = exists(
        select(DispatchAttempt.id)
        .join(
            ContentAsset,
            (ContentAsset.id == DispatchAttempt.content_asset_id)
            & (ContentAsset.campaign_id == campaign_id),
        )
        .where(
            DispatchAttempt.tenant_id == kpi.tenant_id,
            DispatchAttempt.provider_message_id == sg_message_id,
        )
    )

    conditions = [
        AnalyticEvent.tenant_id == kpi.tenant_id,
        AnalyticEvent.event_type == event_kind,
        (AnalyticEvent.campaign_id == campaign_id) | indirect_link,
    ]

    window_days = formula.get("window_days")
    if isinstance(window_days, (int, float)) and window_days > 0:
        cutoff = now - timedelta(days=int(window_days))
        conditions.append(AnalyticEvent.event_at >= cutoff)

    for f in formula.get("filters") or []:
        clause = _filter_clause(f)
        if clause is not None:
            conditions.append(clause)

    count = (
        await session.execute(
            select(func.count(AnalyticEvent.id)).where(*conditions)
        )
    ).scalar_one()
    return EvaluationResult(value=int(count), missing_event=False)


async def _has_any_event_of_kind(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    campaign_id: UUID,
    event_kind: EventKind,
) -> bool:
    sg_message_id = AnalyticEvent.payload["sg_message_id"].astext
    indirect_link = exists(
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
    row = (
        await session.execute(
            select(AnalyticEvent.id)
            .where(
                AnalyticEvent.tenant_id == tenant_id,
                AnalyticEvent.event_type == event_kind,
                (AnalyticEvent.campaign_id == campaign_id) | indirect_link,
            )
            .limit(1)
        )
    ).first()
    return row is not None


def _filter_clause(filter_obj: Any) -> Any:
    """Translate one filter spec to a SQLAlchemy condition. Returns None
    for malformed filters (operator unsupported, path empty) so the rest
    of the evaluation still runs — we don't fail the KPI for one bad
    line."""
    if not isinstance(filter_obj, dict):
        return None
    path = filter_obj.get("path") or ""
    op = filter_obj.get("op") or "eq"
    value = filter_obj.get("value")
    if not path or op not in _SUPPORTED_OPS:
        return None

    column = _column_for_path(path)
    if column is None:
        return None

    if op == "eq":
        return column == _str_value(value)
    if op == "neq":
        return column != _str_value(value)
    if op == "in":
        if not isinstance(value, list):
            return None
        return column.in_([_str_value(v) for v in value])
    if op == "not_in":
        if not isinstance(value, list):
            return None
        return column.notin_([_str_value(v) for v in value])
    if op == "contains":
        return column.ilike(f"%{_str_value(value)}%")
    return None


def _column_for_path(path: str):
    """Resolve a dotted path to a SQLAlchemy expression. Supports
    `payload.<key>` for JSONB payload access and bare column names for
    top-level analytic_event columns we want to filter by."""
    parts = [p for p in path.split(".") if p]
    if not parts:
        return None
    if parts[0] == "payload":
        if len(parts) < 2:
            return None
        # Drill into nested payload via JSONB ->> for the leaf and -> for
        # parents. For the simple case (payload.utm_source), one level
        # is enough.
        expr = AnalyticEvent.payload
        for key in parts[1:-1]:
            expr = expr[key]
        return expr[parts[-1]].astext

    # Top-level column allow-list. Channel id + event_type are useful;
    # the evaluator can be expanded if needed.
    if parts[0] == "channel_id":
        return AnalyticEvent.channel_id
    if parts[0] == "event_type":
        return AnalyticEvent.event_type
    return None


def _str_value(v: Any) -> str:
    return str(v) if v is not None else ""


__all__ = ["evaluate_custom_kpi", "EvaluationResult"]
