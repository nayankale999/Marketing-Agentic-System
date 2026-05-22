"""Pydantic schemas for /api/content-assets surfaces (W22, E06-S01)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ContentAssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    campaign_id: UUID
    channel_id: UUID | None
    asset_type: str
    status: str
    title: str | None
    content: str | None
    extra_metadata: dict[str, Any]
    is_required: bool
    scheduled_at: datetime | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ContentAssetListResponse(BaseModel):
    items: list[ContentAssetOut]
    total: int


class StartContentResponse(BaseModel):
    """Returned when /api/campaigns/{id}/content/start fires the transition."""

    campaign_id: UUID
    status: str
    assets_created: int
