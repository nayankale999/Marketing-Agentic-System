"""Pydantic schemas for the /api/campaigns surface (E03 stories)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import CampaignStatus, CampaignType


class CampaignCreate(BaseModel):
    """Create-payload for E03-S01 (author a campaign brief)."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=200)]
    campaign_type: CampaignType
    objective: Annotated[str, Field(min_length=1)]
    start_date: date
    end_date: date
    budget_total: Annotated[Decimal, Field(default=Decimal("0"), ge=0)]
    currency: Annotated[
        str, Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    ]
    brief: str | None = None
    kpi_targets: dict[str, Any] = Field(default_factory=dict)


class CampaignPatch(BaseModel):
    """Partial-update payload for E03-S02 (edit before launch)."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str | None, Field(default=None, min_length=1, max_length=200)] = None
    campaign_type: CampaignType | None = None
    objective: Annotated[str | None, Field(default=None, min_length=1)] = None
    start_date: date | None = None
    end_date: date | None = None
    budget_total: Annotated[Decimal | None, Field(default=None, ge=0)] = None
    currency: Annotated[
        str | None, Field(default=None, min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    ] = None
    brief: str | None = None
    kpi_targets: dict[str, Any] | None = None


class CampaignOut(BaseModel):
    """Response shape for a single campaign."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    owner_id: UUID | None
    name: str
    status: CampaignStatus
    campaign_type: CampaignType
    objective: str
    budget_total: Decimal
    currency: str
    start_date: date
    end_date: date
    brief: str | None
    kpi_targets: dict[str, Any]
    launched_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CampaignListResponse(BaseModel):
    """Paginated list response for E03-S06."""

    items: list[CampaignOut]
    total: int
    limit: int
    offset: int


class PauseCampaignRequest(BaseModel):
    """Body for POST /api/campaigns/{id}/pause (W31, E08-S07)."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="manual", max_length=200)


class PauseCampaignResponse(BaseModel):
    campaign_id: UUID
    status: str
    reason: str
    cancelled_tasks: int


class ResumeCampaignResponse(BaseModel):
    campaign_id: UUID
    status: str
    requeued: int
    elapsed_failed: int
