"""Approval review UI (W32, E13-S03).

Two pages plus two POST handlers that wrap the existing API endpoints:

  - GET  /ui/approvals/queue                 — list pending assets
  - GET  /ui/approvals/{asset_id}            — full review page
  - POST /ui/approvals/{asset_id}/approve    — form post; calls api/approve
  - POST /ui/approvals/{asset_id}/reject     — form post; calls api/reject

The POST handlers return an HTML fragment that HTMX swaps in place of the
decision footer, so the marketer sees the result without a full page
reload. Forms also work without JS (HTMX's `hx-post` falls back to
standard form submission, and our POST handlers return a complete
review page when the request isn't an HTMX swap)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._preview import resolve_merge_fields
from app.api.deps import get_tenant_db, require_role
from app.api.ui.templates import templates
from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import column_snapshot, write_audit
from app.db.enums import (
    ApprovalDecision,
    ApprovalRejectionCategory,
    AssetStatus,
    UserRole,
)
from app.db.models import (
    AppUser,
    ApprovalDecisionLog,
    Audience,
    Campaign,
    ContentAsset,
)

router = APIRouter(prefix="/ui/approvals", tags=["ui"])

_OVERDUE_AFTER = timedelta(hours=24)


@router.get("/queue", response_class=HTMLResponse)
async def approval_queue(
    request: Request,
    _user: AppUser = Depends(require_role(UserRole.manager)),
    db: AsyncSession = Depends(get_tenant_db),
) -> HTMLResponse:
    """Approvals overview. Three sections:
      * Pending review — assets formally submitted (pending_approval).
      * Drafted, awaiting your call — assets sitting in `drafted` (the
        assistant flow lands them here when content_creator finishes).
      * Recently decided — last 20 approvals + rejections, with the
        reviewer + reason — so the page isn't blank when the queue is
        empty.
    """
    cutoff = datetime.now(UTC) - _OVERDUE_AFTER
    pending = await _items_in_status(db, AssetStatus.pending_approval, cutoff)
    drafted = await _items_in_status(db, AssetStatus.drafted, cutoff)
    recent = await _recent_decisions(db, limit=20)

    return templates.TemplateResponse(
        request,
        "approvals/queue.html",
        {
            "pending_items": pending,
            "drafted_items": drafted,
            "recent_decisions": recent,
        },
    )


async def _items_in_status(
    db: AsyncSession, status_value: AssetStatus, cutoff: datetime
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(ContentAsset, Campaign)
            .join(Campaign, Campaign.id == ContentAsset.campaign_id)
            .where(ContentAsset.status == status_value)
            .order_by(Campaign.end_date.asc(), ContentAsset.updated_at.asc())
        )
    ).all()
    items: list[dict[str, Any]] = []
    for asset, campaign in rows:
        meta = asset.extra_metadata or {}
        compliance = meta.get("compliance") or {}
        items.append(
            {
                "asset_id": asset.id,
                "title": asset.title,
                "asset_type": asset.asset_type.value,
                "channel_platform": meta.get("channel_platform"),
                "campaign_id": campaign.id,
                "campaign_name": campaign.name,
                "submitted_at": asset.updated_at,
                "overdue": asset.updated_at < cutoff,
                "compliance_blocked": bool(compliance.get("blocked")),
                "segment_label": meta.get("segment_label"),
            }
        )
    return items


async def _recent_decisions(
    db: AsyncSession, *, limit: int
) -> list[dict[str, Any]]:
    """Pull the last N approval decisions with reviewer + asset + campaign
    joined in so the queue page can show 'Approved by Ada · 12 min ago'
    next to each row."""
    rows = (
        await db.execute(
            select(ApprovalDecisionLog, ContentAsset, Campaign, AppUser)
            .join(ContentAsset, ContentAsset.id == ApprovalDecisionLog.content_asset_id)
            .join(Campaign, Campaign.id == ContentAsset.campaign_id)
            .join(AppUser, AppUser.id == ApprovalDecisionLog.reviewer_id)
            .order_by(ApprovalDecisionLog.decided_at.desc())
            .limit(limit)
        )
    ).all()
    out: list[dict[str, Any]] = []
    for decision, asset, campaign, reviewer in rows:
        out.append(
            {
                "decision_id": decision.id,
                "asset_id": asset.id,
                "asset_title": asset.title,
                "asset_type": asset.asset_type.value,
                "campaign_id": campaign.id,
                "campaign_name": campaign.name,
                "decision": decision.decision.value,
                "reason": decision.reason,
                "reviewer_name": (
                    reviewer.display_name or reviewer.email
                ),
                "decided_at": decision.decided_at,
            }
        )
    return out


@router.get("/{asset_id}", response_class=HTMLResponse)
async def approval_review(
    asset_id: UUID,
    request: Request,
    _user: AppUser = Depends(require_role(UserRole.manager)),
    db: AsyncSession = Depends(get_tenant_db),
) -> HTMLResponse:
    asset = await db.get(ContentAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="asset not found")
    campaign = await db.get(Campaign, asset.campaign_id)
    audience = (
        await db.execute(
            select(Audience)
            .where(Audience.campaign_id == asset.campaign_id)
            .order_by(Audience.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    preview_fields, preview_unresolved, channel_kind = _build_preview(asset)

    return templates.TemplateResponse(
        request,
        "approvals/review.html",
        {
            "asset": asset,
            "campaign": campaign,
            "audience": audience,
            "preview_fields": preview_fields,
            "preview_unresolved": preview_unresolved,
            "channel_kind": channel_kind,
        },
    )


@router.post("/{asset_id}/approve", response_class=HTMLResponse)
async def approve_via_ui(
    asset_id: UUID,
    request: Request,
    user: AppUser = Depends(require_role(UserRole.manager)),
    db: AsyncSession = Depends(get_tenant_db),
) -> HTMLResponse:
    """UI-side wrapper around the existing approve endpoint. Reads the
    form (multipart or urlencoded), builds the edits payload, calls the
    same persistence path the API uses, returns the result fragment."""
    form = await request.form()
    edited_content = (form.get("edited_content") or "").strip() or None
    note = (form.get("note") or "").strip() or None
    edited_fields = _collect_edited_fields(form)

    asset = await db.get(ContentAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="asset not found")
    campaign = await db.get(Campaign, asset.campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    # `drafted` is the state the assistant content-generation flow
    # leaves assets in (no formal submit_for_approval has been called
    # yet). We accept it here and let the auto-advance helper drive
    # the campaign through submit_for_approval → start_launch as the
    # last required asset gets approved.
    if asset.status not in {
        AssetStatus.pending_approval,
        AssetStatus.rejected,
        AssetStatus.drafted,
    }:
        return templates.TemplateResponse(
            request,
            "approvals/_decision_result.html",
            {
                "result_kind": "danger",
                "message": (
                    f"Asset is in '{asset.status.value}' — not eligible for approval."
                ),
                "decision_id": "—",
            },
        )

    if _is_compliance_blocked(asset):
        return templates.TemplateResponse(
            request,
            "approvals/_decision_result.html",
            {
                "result_kind": "danger",
                "message": (
                    "Asset is compliance-blocked. Clear via "
                    "/api/content-assets/{id}/clear-compliance first."
                ),
                "decision_id": "—",
            },
        )

    edits_payload: dict[str, Any] | None = None
    edits_present = edited_content is not None or edited_fields or note
    if edits_present:
        edits_payload = {
            "previous_content": asset.content,
            "previous_fields": dict(
                (asset.extra_metadata or {}).get("fields", {})
            ),
            "current_content": edited_content
            if edited_content is not None
            else asset.content,
            "current_fields": _merge_fields(
                (asset.extra_metadata or {}).get("fields", {}), edited_fields
            ),
        }
        if note:
            edits_payload["note"] = note
        if edited_content is not None:
            asset.content = edited_content
        if edited_fields:
            existing_metadata = dict(asset.extra_metadata or {})
            existing_fields = dict(existing_metadata.get("fields") or {})
            existing_fields.update(edited_fields)
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
        reason=note,
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
        metadata={"decision_id": str(decision.id), "via": "ui"},
    )

    # Auto-advance the campaign through the state machine. Handles the
    # "approved from drafted" case (campaign still in content_in_production
    # → first push to approval_pending → then start_launch) as well as
    # the normal "approved while pending_approval" case.
    from app.approvals import try_advance_after_approval

    advanced_to = await try_advance_after_approval(db, campaign)
    message_tail = ""
    if advanced_to == "ready_to_launch":
        message_tail = " Every required asset is approved — campaign is now ready to launch."

    return templates.TemplateResponse(
        request,
        "approvals/_decision_result.html",
        {
            "result_kind": "success",
            "message": (
                "Approved with edits." + message_tail
                if edits_payload is not None
                else "Approved." + message_tail
            ),
            "decision_id": str(decision.id),
        },
    )


@router.post("/{asset_id}/reject", response_class=HTMLResponse)
async def reject_via_ui(
    asset_id: UUID,
    request: Request,
    reason: str = Form(...),
    category: str = Form(...),
    user: AppUser = Depends(require_role(UserRole.manager)),
    db: AsyncSession = Depends(get_tenant_db),
) -> HTMLResponse:
    reason = reason.strip()
    if not reason:
        return templates.TemplateResponse(
            request,
            "approvals/_decision_result.html",
            {
                "result_kind": "danger",
                "message": "Rejection reason is required.",
                "decision_id": "—",
            },
        )

    try:
        category_enum = ApprovalRejectionCategory(category)
    except ValueError:
        category_enum = ApprovalRejectionCategory.other

    asset = await db.get(ContentAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="asset not found")

    if asset.status not in {AssetStatus.pending_approval, AssetStatus.drafted}:
        return templates.TemplateResponse(
            request,
            "approvals/_decision_result.html",
            {
                "result_kind": "danger",
                "message": (
                    f"Asset is in '{asset.status.value}' — not eligible for rejection."
                ),
                "decision_id": "—",
            },
        )

    before = column_snapshot(asset)
    decision = ApprovalDecisionLog(
        content_asset_id=asset.id,
        reviewer_id=user.id,
        decision=ApprovalDecision.rejected,
        reason=reason,
        edits={"category": category_enum.value},
    )
    db.add(decision)
    asset.status = AssetStatus.rejected
    # Mirror the assistant `reject_asset` semantics: rejection from the
    # UI means "drop this variant from the campaign", not "regenerate
    # it" (regenerate is a separate API flow). Flip is_required so the
    # rejection doesn't block the launch guard for the campaign's
    # remaining approved assets.
    asset.is_required = False
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
            "category": category_enum.value,
            "via": "ui",
        },
    )

    # Auto-advance: a rejection on the last blocker can be just as
    # campaign-moving as an approval (the rejected asset is now
    # is_required=False so it no longer blocks start_launch).
    from app.approvals import try_advance_after_approval

    campaign = await db.get(Campaign, asset.campaign_id)
    if campaign is not None:
        await try_advance_after_approval(db, campaign)

    return templates.TemplateResponse(
        request,
        "approvals/_decision_result.html",
        {
            "result_kind": "info",
            "message": f"Rejected — {category_enum.value}.",
            "decision_id": str(decision.id),
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_preview(asset: ContentAsset) -> tuple[dict[str, str], list[str], str | None]:
    """Render the asset's body + metadata.fields through the W24 merge-field
    resolver using built-in defaults. Returns (rendered_fields, unresolved_list,
    channel_kind) for the template to consume."""
    fields_in: dict[str, str] = {}
    if asset.content:
        fields_in["body"] = asset.content
    md_fields = (asset.extra_metadata or {}).get("fields") or {}
    for k, v in md_fields.items():
        if isinstance(v, str):
            fields_in[k] = v

    rendered, report = resolve_merge_fields(fields_in, sample_values={})
    channel_kind = (asset.extra_metadata or {}).get("channel_platform")
    return rendered, report.unresolved_fields, channel_kind


def _is_compliance_blocked(asset: ContentAsset) -> bool:
    if not asset.extra_metadata:
        return False
    compliance = asset.extra_metadata.get("compliance")
    return bool(isinstance(compliance, dict) and compliance.get("blocked"))


def _collect_edited_fields(form_data) -> dict[str, str]:
    """Pluck per-field edits from the form. Convention: input names prefixed
    with `edit_field_` become entries in `metadata.fields`."""
    out: dict[str, str] = {}
    for key in form_data.keys():
        if not key.startswith("edit_field_"):
            continue
        field_name = key[len("edit_field_") :]
        value = form_data.get(key)
        if isinstance(value, str) and value.strip():
            out[field_name] = value
    return out


def _merge_fields(
    existing: dict[str, str], overrides: dict[str, str]
) -> dict[str, str]:
    out = dict(existing)
    out.update(overrides)
    return out


