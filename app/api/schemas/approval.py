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
