"""Approval queue + per-asset decision endpoints (W25, E07-S01/02).

  - GET   /api/approvals/queue                          — pending_approval assets
  - GET   /api/content-assets/{id}/approval-history     — past decisions
  - POST  /api/content-assets/{id}/approve              — approve (with optional edits)
  - POST  /api/content-assets/{id}/reject               — reject + enqueue regenerate

The queue endpoint is gated to manager+ role. Per-asset decisions also
require manager+. Compliance-blocked assets must be cleared via
/clear-compliance (W23) before approve will accept them — reject is still
allowed because rejecting + regenerating is the right path if compliance
keeps flagging.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decimal import Decimal, InvalidOperation

from app.agents.content_creator import ensure_content_creator_agent
from app.api.deps import get_tenant_db, require_role
from app.api.schemas.approval import (
    ApprovalDecisionOut,
    ApprovalHistoryResponse,
    ApprovalQueueItem,
    ApprovalQueueResponse,
    ApproveRequest,
    BatchApproveRequest,
    BatchApproveResponse,
    BatchApprovalSummary,
    BatchApprovedEntry,
    BatchExclusionEntry,
    RejectRequest,
)
from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import column_snapshot, write_audit
from app.db.enums import (
    ApprovalDecision,
    AssetStatus,
    AssetType,
    CampaignStatus,
    ChannelPlatform,
    UserRole,
)
from app.db.models import (
    AppUser,
    ApprovalDecisionLog,
    Campaign,
    ContentAsset,
)
from app.orchestrator.queue import enqueue_task
from app.orchestrator.state_machine import (
    GuardFailedError,
    UnknownTransitionError,
    campaign_sm,
)

router = APIRouter(prefix="/api", tags=["approvals"])

_OVERDUE_AFTER = timedelta(hours=24)


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


@router.get("/approvals/queue", response_model=ApprovalQueueResponse)
async def list_approval_queue(
    campaign_id: Annotated[UUID | None, Query()] = None,
    channel_platform: Annotated[ChannelPlatform | None, Query()] = None,
    asset_type: Annotated[AssetType | None, Query()] = None,
    submitter_id: Annotated[UUID | None, Query()] = None,
    _user: AppUser = Depends(require_role(UserRole.manager)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ApprovalQueueResponse:
    """E07-S01: assets in `pending_approval` across the tenant, ordered by
    campaign end_date then submitted_at (= asset.updated_at when flipped to
    pending_approval). Per-item `overdue` is computed against now-24h."""
    stmt = (
        select(ContentAsset, Campaign)
        .join(Campaign, Campaign.id == ContentAsset.campaign_id)
        .where(ContentAsset.status == AssetStatus.pending_approval)
        .order_by(Campaign.end_date.asc(), ContentAsset.updated_at.asc())
    )
    if campaign_id is not None:
        stmt = stmt.where(ContentAsset.campaign_id == campaign_id)
    if asset_type is not None:
        stmt = stmt.where(ContentAsset.asset_type == asset_type)
    if submitter_id is not None:
        stmt = stmt.where(Campaign.owner_id == submitter_id)

    rows = (await db.execute(stmt)).all()

    cutoff = datetime.now(UTC) - _OVERDUE_AFTER
    items: list[ApprovalQueueItem] = []
    for asset, campaign in rows:
        platform = _platform_for(asset)
        if channel_platform is not None and platform != channel_platform.value:
            continue
        items.append(
            ApprovalQueueItem(
                asset_id=asset.id,
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                campaign_end_date=campaign.end_date,
                asset_type=asset.asset_type.value,
                channel_platform=platform,
                title=asset.title,
                submitter_id=campaign.owner_id,
                submitted_at=asset.updated_at,
                overdue=asset.updated_at < cutoff,
                compliance_blocked=_is_compliance_blocked(asset),
            )
        )
    return ApprovalQueueResponse(items=items, total=len(items))


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@router.get(
    "/content-assets/{asset_id}/approval-history",
    response_model=ApprovalHistoryResponse,
)
async def get_approval_history(
    asset_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ApprovalHistoryResponse:
    """The audit trail for an asset — every approve/reject decision in
    descending chronological order. RLS ensures cross-tenant access is
    blocked (the join through content_asset enforces tenant isolation)."""
    asset = await db.get(ContentAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="content asset not found")

    decisions = (
        await db.execute(
            select(ApprovalDecisionLog)
            .where(ApprovalDecisionLog.content_asset_id == asset_id)
            .order_by(ApprovalDecisionLog.decided_at.desc())
        )
    ).scalars().all()
    return ApprovalHistoryResponse(
        asset_id=asset_id,
        decisions=[ApprovalDecisionOut.model_validate(d) for d in decisions],
        total=len(decisions),
    )


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------


@router.post(
    "/content-assets/{asset_id}/approve",
    response_model=ApprovalDecisionOut,
    status_code=status.HTTP_201_CREATED,
)
async def approve_asset(
    asset_id: UUID,
    body: ApproveRequest,
    user: AppUser = Depends(require_role(UserRole.manager)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ApprovalDecisionLog:
    """E07-S02 #1/#2: approve, optionally with inline edits.

    Compliance-blocked assets require `/clear-compliance` first — the
    approve endpoint refuses with 422 to surface the right next action
    explicitly rather than silently approving a flagged draft."""
    asset, campaign = await _load_asset_and_campaign(db, asset_id)

    if asset.status not in {AssetStatus.pending_approval, AssetStatus.rejected}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"asset is in '{asset.status.value}', not eligible for approval",
        )

    if _is_compliance_blocked(asset):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="asset is compliance-blocked — clear via /clear-compliance first",
        )

    # E07-S04: threshold gate. Snapshot was taken at submit_for_approval time
    # so the value applied here is what was true when the asset entered the
    # queue, not the latest tenant settings value.
    threshold_decision = _evaluate_threshold(asset, campaign, user)
    if threshold_decision["requires_admin"] and user.role != UserRole.admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={
                "message": "requires higher role: admin",
                "applied_threshold": threshold_decision["applied_threshold"],
            },
        )

    edits_payload: dict[str, Any] | None = None
    if body.edited_content is not None or body.edited_fields:
        edits_payload = {
            "previous_content": asset.content,
            "previous_fields": dict(
                (asset.extra_metadata or {}).get("fields", {})
            ),
            "current_content": body.edited_content
            if body.edited_content is not None
            else asset.content,
            "current_fields": _merge_fields(
                (asset.extra_metadata or {}).get("fields", {}),
                body.edited_fields or {},
            ),
        }
        if body.note:
            edits_payload["note"] = body.note

        # Apply the edits to the asset itself so downstream agents see the
        # approved (edited) content as the canonical draft.
        if body.edited_content is not None:
            asset.content = body.edited_content
        if body.edited_fields:
            existing_metadata = dict(asset.extra_metadata or {})
            existing_fields = dict(existing_metadata.get("fields") or {})
            existing_fields.update(body.edited_fields)
            existing_metadata["fields"] = existing_fields
            asset.extra_metadata = existing_metadata

    decision_kind = (
        ApprovalDecision.approved_with_edits
        if edits_payload is not None
        else ApprovalDecision.approved
    )

    before = column_snapshot(asset)
    decision = ApprovalDecisionLog(
        content_asset_id=asset.id,
        reviewer_id=user.id,
        decision=decision_kind,
        reason=body.note,
        edits=edits_payload,
    )
    db.add(decision)
    asset.status = AssetStatus.approved
    await db.flush()
    after = column_snapshot(asset)

    write_audit(
        db,
        tenant_id=asset.tenant_id,
        actor_kind=current_actor_kind.get(),
        actor_id=current_actor_id.get(),
        entity_kind="content_asset",
        entity_id=asset.id,
        action="approved" if edits_payload is None else "approved_with_edits",
        before_state=before,
        after_state=after,
        metadata={
            "decision_id": str(decision.id),
            # E07-S04 #4: record the threshold the reviewer was evaluated
            # against so the audit trail captures what gate applied.
            "applied_threshold": threshold_decision["applied_threshold"],
        },
    )

    await _maybe_advance_to_ready_to_launch(db, campaign=campaign)
    return decision


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


@router.post(
    "/content-assets/{asset_id}/reject",
    response_model=ApprovalDecisionOut,
    status_code=status.HTTP_201_CREATED,
)
async def reject_asset(
    asset_id: UUID,
    body: RejectRequest,
    user: AppUser = Depends(require_role(UserRole.manager)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ApprovalDecisionLog:
    """E07-S02 #3/#4: reject with reason + enqueue regenerate task.

    The asset lands in `rejected` (per AC) and a regenerate task is queued
    with the reason in input_data. The worker (when E06-S06 lands) will
    splice the reason into the regenerate prompt; for W25 the reason is
    carried through but not yet consumed by the agent."""
    asset, campaign = await _load_asset_and_campaign(db, asset_id)

    if asset.status != AssetStatus.pending_approval:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"asset is in '{asset.status.value}', not eligible for rejection",
        )

    before = column_snapshot(asset)
    decision = ApprovalDecisionLog(
        content_asset_id=asset.id,
        reviewer_id=user.id,
        decision=ApprovalDecision.rejected,
        reason=body.reason,
        edits={"category": body.category.value},
    )
    db.add(decision)
    asset.status = AssetStatus.rejected
    await db.flush()
    after = column_snapshot(asset)

    write_audit(
        db,
        tenant_id=asset.tenant_id,
        actor_kind=current_actor_kind.get(),
        actor_id=current_actor_id.get(),
        entity_kind="content_asset",
        entity_id=asset.id,
        action="rejected",
        before_state=before,
        after_state=after,
        metadata={
            "decision_id": str(decision.id),
            "category": body.category.value,
        },
    )

    # E07-S02 #4: enqueue the regenerate task with the reason attached.
    # The worker resets the asset back to `requested` on pick-up; for the
    # API consumer immediately after this call, the asset is still `rejected`.
    agent = await ensure_content_creator_agent(db, asset.tenant_id)
    await enqueue_task(
        db,
        tenant_id=asset.tenant_id,
        agent_id=agent.id,
        campaign_id=asset.campaign_id,
        skill_name="content_creator.generate_asset",
        input_data={
            "asset_id": str(asset.id),
            "campaign_id": str(asset.campaign_id),
            "triggered_by_user_id": str(user.id),
            "rejection_reason": body.reason,
            "rejection_category": body.category.value,
        },
    )

    await _maybe_revert_to_content_in_production(db, campaign=campaign)
    return decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_asset_and_campaign(
    db: AsyncSession, asset_id: UUID
) -> tuple[ContentAsset, Campaign]:
    asset = await db.get(ContentAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="content asset not found")
    campaign = await db.get(Campaign, asset.campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")
    return asset, campaign


def _platform_for(asset: ContentAsset) -> str | None:
    if asset.extra_metadata:
        platform = asset.extra_metadata.get("channel_platform")
        if isinstance(platform, str) and platform:
            return platform
    return None


def _is_compliance_blocked(asset: ContentAsset) -> bool:
    if not asset.extra_metadata:
        return False
    compliance = asset.extra_metadata.get("compliance")
    return bool(isinstance(compliance, dict) and compliance.get("blocked"))


def _merge_fields(
    existing: dict[str, str], overrides: dict[str, str]
) -> dict[str, str]:
    out = dict(existing)
    out.update(overrides)
    return out


async def _maybe_advance_to_ready_to_launch(
    db: AsyncSession, *, campaign: Campaign
) -> None:
    """If every required asset is approved, drive the campaign forward."""
    if campaign.status != CampaignStatus.approval_pending:
        return
    try:
        await campaign_sm.apply(db, campaign, "start_launch")
    except (UnknownTransitionError, GuardFailedError):
        return


async def _maybe_revert_to_content_in_production(
    db: AsyncSession, *, campaign: Campaign
) -> None:
    """Send the campaign back so the regenerate task's output gets re-queued
    through submit_for_approval when it lands."""
    if campaign.status != CampaignStatus.approval_pending:
        return
    try:
        await campaign_sm.apply(db, campaign, "regenerate_after_rejection")
    except (UnknownTransitionError, GuardFailedError):
        return


# ---------------------------------------------------------------------------
# Threshold evaluation (W26, E07-S04)
# ---------------------------------------------------------------------------


def _evaluate_threshold(
    asset: ContentAsset, campaign: Campaign, user: AppUser
) -> dict[str, object]:
    """Decide whether the reviewer's role is sufficient for this asset.

    Returns a dict with:
      - `requires_admin`: bool — true if the threshold says admin role needed
      - `applied_threshold`: structured dict for audit + 403 detail
    """
    snapshot = (asset.extra_metadata or {}).get("approval_threshold") or {}
    admin_raw = snapshot.get("admin_required_above_amount")
    snapshot_currency = snapshot.get("currency") or "USD"

    applied: dict[str, object] = {
        "admin_required_above_amount": admin_raw,
        "snapshot_currency": snapshot_currency,
        "campaign_budget": str(campaign.budget_total),
        "campaign_currency": campaign.currency,
        "snapshot_taken_at": snapshot.get("snapshot_taken_at"),
        "skipped": None,
    }

    # No snapshot → no gate applied (e.g. assets that bypassed the state
    # machine in a test harness). Same outcome as "no threshold configured".
    if admin_raw is None:
        applied["skipped"] = "no_threshold_configured"
        return {"requires_admin": False, "applied_threshold": applied}

    # Currency mismatch → we don't have FX plumbing; skip rather than convert.
    # Logged in audit so operators can see the gap.
    if snapshot_currency != campaign.currency:
        applied["skipped"] = "currency_mismatch"
        return {"requires_admin": False, "applied_threshold": applied}

    try:
        admin_required_above = Decimal(str(admin_raw))
    except (InvalidOperation, ValueError):
        applied["skipped"] = "invalid_threshold_value"
        return {"requires_admin": False, "applied_threshold": applied}

    requires_admin = campaign.budget_total > admin_required_above
    return {"requires_admin": requires_admin, "applied_threshold": applied}


def _exceeds_auto_approval_cap(
    asset: ContentAsset, campaign: Campaign
) -> tuple[bool, Decimal | None]:
    """Check the auto_approval_cap from the asset's snapshot. Returns
    `(exceeds, cap)` so the caller can include the cap in the exclusion entry."""
    snapshot = (asset.extra_metadata or {}).get("approval_threshold") or {}
    cap_raw = snapshot.get("auto_approval_cap_amount")
    if cap_raw is None:
        # Default 0 still applies — treat any campaign budget > 0 as over cap.
        cap_raw = "0"
    snapshot_currency = snapshot.get("currency") or "USD"
    if snapshot_currency != campaign.currency:
        return False, None  # currency mismatch → skip cap too
    try:
        cap = Decimal(str(cap_raw))
    except (InvalidOperation, ValueError):
        return False, None
    return campaign.budget_total > cap, cap


# ---------------------------------------------------------------------------
# Batch approve (W26, E07-S03)
# ---------------------------------------------------------------------------


@router.post(
    "/approvals/batch-approve",
    response_model=BatchApproveResponse,
)
async def batch_approve(
    body: BatchApproveRequest,
    user: AppUser = Depends(require_role(UserRole.manager)),
    db: AsyncSession = Depends(get_tenant_db),
) -> BatchApproveResponse:
    """E07-S03: approve up to 200 assets in one call. Each asset is evaluated
    against compliance, status, auto_approval_cap, and admin-role threshold.
    Excluded items return a structured reason so the UI can surface them
    individually for follow-up.

    Per-asset atomicity (AC #2): each successful approval lives in its own
    savepoint, so a constraint failure on one row doesn't roll back the others.
    """
    rows = (
        await db.execute(
            select(ContentAsset).where(ContentAsset.id.in_(body.asset_ids))
        )
    ).scalars().all()
    rows_by_id: dict[UUID, ContentAsset] = {row.id: row for row in rows}

    # Pre-load every distinct campaign in one query.
    campaign_ids = {row.campaign_id for row in rows}
    campaigns = (
        await db.execute(
            select(Campaign).where(Campaign.id.in_(campaign_ids))
        )
    ).scalars().all()
    campaigns_by_id: dict[UUID, Campaign] = {c.id: c for c in campaigns}

    approved: list[BatchApprovedEntry] = []
    excluded: list[BatchExclusionEntry] = []
    channel_counts: dict[str, int] = {}
    approved_campaign_ids: set[UUID] = set()
    currency_for_summary: str | None = None

    for asset_id in body.asset_ids:
        asset = rows_by_id.get(asset_id)
        if asset is None:
            excluded.append(
                BatchExclusionEntry(asset_id=asset_id, reason="not_found", details={})
            )
            continue
        campaign = campaigns_by_id.get(asset.campaign_id)
        if campaign is None:
            excluded.append(
                BatchExclusionEntry(
                    asset_id=asset_id,
                    reason="not_found",
                    details={"missing": "campaign"},
                )
            )
            continue

        # Status check first — the cheapest filter.
        if asset.status not in {AssetStatus.pending_approval, AssetStatus.rejected}:
            excluded.append(
                BatchExclusionEntry(
                    asset_id=asset_id,
                    reason="wrong_status",
                    details={"status": asset.status.value},
                )
            )
            continue

        # Compliance — never auto-approve a flagged asset (AC E07-S03 #3).
        if _is_compliance_blocked(asset):
            excluded.append(
                BatchExclusionEntry(
                    asset_id=asset_id,
                    reason="compliance_blocked",
                    details={},
                )
            )
            continue

        # Auto-approval cap (AC E07-S03 #4).
        exceeds_cap, cap_value = _exceeds_auto_approval_cap(asset, campaign)
        if exceeds_cap:
            excluded.append(
                BatchExclusionEntry(
                    asset_id=asset_id,
                    reason="above_auto_approval_cap",
                    details={
                        "campaign_budget": str(campaign.budget_total),
                        "cap": str(cap_value) if cap_value is not None else None,
                        "currency": campaign.currency,
                    },
                )
            )
            continue

        # Admin-role threshold (AC E07-S04 #1/#2 applied in batch context too).
        threshold = _evaluate_threshold(asset, campaign, user)
        if threshold["requires_admin"] and user.role != UserRole.admin:
            excluded.append(
                BatchExclusionEntry(
                    asset_id=asset_id,
                    reason="requires_admin_role",
                    details={
                        "campaign_budget": str(campaign.budget_total),
                        "threshold": str(
                            threshold["applied_threshold"].get(
                                "admin_required_above_amount"
                            )
                        ),
                        "currency": campaign.currency,
                    },
                )
            )
            continue

        # All filters passed — this one would (or will) auto-approve.
        platform = _platform_for(asset) or asset.asset_type.value
        channel_counts[platform] = channel_counts.get(platform, 0) + 1
        approved_campaign_ids.add(campaign.id)
        currency_for_summary = currency_for_summary or campaign.currency

        if body.dry_run:
            approved.append(
                BatchApprovedEntry(asset_id=asset_id, decision_id=None)
            )
            continue

        # Real write — per-asset savepoint so failures don't cascade.
        try:
            async with db.begin_nested():
                decision = ApprovalDecisionLog(
                    content_asset_id=asset.id,
                    reviewer_id=user.id,
                    decision=ApprovalDecision.approved,
                    reason=None,
                    edits={"batch": True},
                )
                db.add(decision)
                before = column_snapshot(asset)
                asset.status = AssetStatus.approved
                await db.flush()
                after = column_snapshot(asset)
                write_audit(
                    db,
                    tenant_id=asset.tenant_id,
                    actor_kind=current_actor_kind.get(),
                    actor_id=current_actor_id.get(),
                    entity_kind="content_asset",
                    entity_id=asset.id,
                    action="approved",
                    before_state=before,
                    after_state=after,
                    metadata={
                        "decision_id": str(decision.id),
                        "applied_threshold": threshold["applied_threshold"],
                        "via": "batch_approve",
                    },
                )
        except Exception as exc:  # noqa: BLE001 — record + continue per AC #2
            excluded.append(
                BatchExclusionEntry(
                    asset_id=asset_id,
                    reason="write_failed",
                    details={"error": str(exc)[:200]},
                )
            )
            continue

        approved.append(
            BatchApprovedEntry(asset_id=asset_id, decision_id=decision.id)
        )

    total_spend = sum(
        (campaigns_by_id[cid].budget_total for cid in approved_campaign_ids),
        start=Decimal("0"),
    )
    summary = BatchApprovalSummary(
        channel_counts=channel_counts,
        total_spend_exposed=str(total_spend),
        currency=currency_for_summary or "USD",
        would_approve_count=len(approved),
        excluded_count=len(excluded),
    )

    if not body.dry_run:
        # Drive the campaign forward if every required asset is approved.
        for campaign_id in approved_campaign_ids:
            await _maybe_advance_to_ready_to_launch(
                db, campaign=campaigns_by_id[campaign_id]
            )

    return BatchApproveResponse(
        summary=summary,
        approved=approved,
        excluded=excluded,
        dry_run=body.dry_run,
    )
