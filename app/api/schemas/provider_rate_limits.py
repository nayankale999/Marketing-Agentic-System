"""Pydantic schemas for /api/provider-rate-limits (W31, E08-S06)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProviderRateLimitUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests_per_minute: int = Field(ge=1, le=10000)
    enabled: bool = False


class ProviderRateLimitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    provider: str
    requests_per_minute: int
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ProviderRateLimitListResponse(BaseModel):
    items: list[ProviderRateLimitOut]
    total: int
