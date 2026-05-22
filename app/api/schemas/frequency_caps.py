"""Pydantic schemas for /api/frequency-caps (W29, E08-S04 #2)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import ChannelPlatform


class FrequencyCapUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_sends_per_recipient: int = Field(ge=1, le=100)
    window_days: int = Field(default=7, ge=1, le=365)
    enabled: bool = False


class FrequencyCapOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    channel_platform: ChannelPlatform
    max_sends_per_recipient: int
    window_days: int
    enabled: bool
    created_at: datetime
    updated_at: datetime


class FrequencyCapListResponse(BaseModel):
    items: list[FrequencyCapOut]
    total: int
