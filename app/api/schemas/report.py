"""Pydantic schemas for campaign report endpoints (W38, E10-S04)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CampaignReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    campaign_id: UUID
    version: int
    generated_at: datetime
    generated_by: str
    data: dict[str, Any]
    is_latest: bool


class CampaignReportSummaryOut(BaseModel):
    """List view — same shape minus the heavy `data` blob."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    campaign_id: UUID
    version: int
    generated_at: datetime
    generated_by: str
    is_latest: bool


class CampaignReportListResponse(BaseModel):
    items: list[CampaignReportSummaryOut]
    total: int
