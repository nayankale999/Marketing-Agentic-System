"""Pydantic schemas for /api/campaigns/{id}/strategy + /api/strategy-proposals."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

class StrategyProposeRequest(BaseModel):
    """Body for `POST /api/campaigns/{id}/strategy`. Empty for now — kept as a
    schema for future fields (e.g. caller-supplied audience override)."""

    model_config = ConfigDict(extra="forbid")


class StrategyProposeResponse(BaseModel):
    task_id: UUID
    skill_name: str
    status: str


class StrategyProposalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    campaign_id: UUID
    version: int
    payload: dict[str, Any]
    is_accepted: bool
    created_by_kind: str
    created_by_id: UUID | None
    validation_warnings: list[dict[str, Any]]
    created_at: datetime


class StrategyProposalListResponse(BaseModel):
    items: list[StrategyProposalOut]
    total: int


class ChannelOverridePatch(BaseModel):
    """A single channel-row override the marketer wants to apply to a proposal."""

    model_config = ConfigDict(extra="forbid")

    platform: str = Field(min_length=1)
    allocation_pct: float | None = Field(default=None, ge=0, le=100)
    allocation_amount: str | None = None  # stringified Decimal
    human_override: bool = True


class StrategyOverridePatch(BaseModel):
    """Body for `PATCH /api/strategy-proposals/{id}` — apply 1+ channel overrides."""

    model_config = ConfigDict(extra="forbid")

    channel_overrides: list[ChannelOverridePatch] = Field(min_length=1)


class TouchpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    proposal_id: UUID
    channel_platform: str
    audience_id: UUID
    scheduled_at: datetime
    position: int
    human_override: bool
    frequency_warning: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class CalendarResponse(BaseModel):
    proposal_id: UUID
    items: list[TouchpointOut]
    total: int


class TouchpointPatch(BaseModel):
    """Body for `PATCH /api/strategy-touchpoints/{id}` — drag a touch to a new
    date (E05-S03 #3). `human_override` defaults true because a hand-edit by
    definition is an override; clients can pass false to clear the flag."""

    model_config = ConfigDict(extra="forbid")

    scheduled_at: datetime
    human_override: bool = True
