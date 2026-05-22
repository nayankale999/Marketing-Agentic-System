"""Pydantic schemas for /api/approvals (W25, E07-S01/02)."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import (
    ApprovalRejectionCategory,
    AssetType,
    ChannelPlatform,
)


class ApprovalQueueItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    asset_id: UUID
    campaign_id: UUID
    campaign_name: str
    campaign_end_date: date
    asset_type: str
    channel_platform: str | None
    title: str | None
    submitter_id: UUID | None
    submitted_at: datetime
    overdue: bool
    compliance_blocked: bool


class ApprovalQueueResponse(BaseModel):
    items: list[ApprovalQueueItem]
    total: int


class ApproveRequest(BaseModel):
    """Body for `POST /api/content-assets/{id}/approve`.

    If `edited_content` or `edited_fields` is provided, the decision recorded
    is `approved_with_edits` (E07-S02 #2); otherwise it's `approved`. The
    previous vs current values land in `approval_decision_log.edits` so the
    history shows exactly what changed."""

    model_config = ConfigDict(extra="forbid")

    edited_content: str | None = None
    edited_fields: dict[str, str] | None = None
    note: str | None = None


class RejectRequest(BaseModel):
    """Body for `POST /api/content-assets/{id}/reject` (E07-S02 #3)."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)
    category: ApprovalRejectionCategory = ApprovalRejectionCategory.other


class ApprovalDecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    content_asset_id: UUID
    reviewer_id: UUID
    decision: str
    reason: str | None
    edits: dict[str, Any] | None
    decided_at: datetime


class ApprovalHistoryResponse(BaseModel):
    asset_id: UUID
    decisions: list[ApprovalDecisionOut]
    total: int


class BatchApproveRequest(BaseModel):
    """Body for `POST /api/approvals/batch-approve` (W26, E07-S03)."""

    model_config = ConfigDict(extra="forbid")

    asset_ids: list[UUID] = Field(min_length=1, max_length=200)
    dry_run: bool = False


class BatchApprovalSummary(BaseModel):
    channel_counts: dict[str, int]
    total_spend_exposed: str  # Decimal as string for client-side rounding control
    currency: str
    would_approve_count: int
    excluded_count: int


class BatchApprovedEntry(BaseModel):
    asset_id: UUID
    decision_id: UUID | None  # None when dry_run=true


class BatchExclusionEntry(BaseModel):
    """One per asset that didn't make it into the auto-approved set. `reason`
    is a stable identifier; `details` carries the relevant amounts so a UI
    can render an explainer ('over cap by $X', 'requires admin role')."""

    asset_id: UUID
    reason: str  # compliance_blocked | above_auto_approval_cap | requires_admin_role | wrong_status | not_found
    details: dict[str, str | None] = Field(default_factory=dict)


class BatchApproveResponse(BaseModel):
    summary: BatchApprovalSummary
    approved: list[BatchApprovedEntry]
    excluded: list[BatchExclusionEntry]
    dry_run: bool
