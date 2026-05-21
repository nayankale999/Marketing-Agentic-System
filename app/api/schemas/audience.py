"""Pydantic schemas for /api/campaigns/.../audiences (E01 + E04 surfaces)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CsvRowErrorOut(BaseModel):
    row: int
    reason: str
    field: str | None = None
    value: str | None = None


class CsvUploadSummary(BaseModel):
    total_rows: int
    imported: int
    skipped_duplicate: int
    failed: int


class CsvUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audience_id: UUID
    audience_name: str
    summary: CsvUploadSummary
    errors: list[CsvRowErrorOut]
    errors_truncated: bool = False


class AudienceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    campaign_id: UUID
    name: str
    segment_criteria: dict[str, Any]
    estimated_size: int | None
    actual_size: int | None
    refreshed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AudienceFreshness(BaseModel):
    """Per-audience freshness summary (E01-S05)."""

    member_count: int
    stale_member_count: int
    ttl_days: int


class AudienceDetail(AudienceOut):
    """Audience info + the freshness summary."""

    freshness: AudienceFreshness


class SegmentationRuleIn(BaseModel):
    """One rule in a segmentation criteria payload."""

    model_config = ConfigDict(extra="forbid")

    field: str
    op: str
    value: Any


class SegmentationCriteriaIn(BaseModel):
    """Input shape for /api/audiences/estimate (and the future build call)."""

    model_config = ConfigDict(extra="forbid")

    include: list[SegmentationRuleIn] = []
    exclude: list[SegmentationRuleIn] = []


class EstimateResponse(BaseModel):
    total_reachable: int
    suppressed: int
    net: int
