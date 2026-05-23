"""Pydantic schemas for /api/ab-tests (W23, E06-S05; W35, E09-S01/02)."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AbTestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    campaign_id: UUID
    name: str
    hypothesis: str | None
    primary_metric: str
    status: str
    variant_a_id: UUID | None
    variant_b_id: UUID | None
    winner_id: UUID | None
    confidence: Decimal | None
    traffic_split: dict[str, int]
    min_runtime_hours: int | None
    max_runtime_hours: int | None
    started_at: datetime | None
    stopped_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AbTestDetail(AbTestOut):
    """List of every variant content_asset id linked to this ab_test, beyond
    just the canonical a/b columns — for multivariate cases."""

    variant_ids: list[UUID]


class AbTestListResponse(BaseModel):
    items: list[AbTestOut]
    total: int


class AddVariantResponse(BaseModel):
    ab_test_id: UUID
    variant_id: UUID
    variant_index: int
    task_id: UUID


class CreateAbTestRequest(BaseModel):
    """W35 (E09-S01): define an A/B test on existing variants."""

    name: str = Field(min_length=1, max_length=200)
    hypothesis: str | None = None
    primary_metric: str = Field(min_length=1, max_length=50)
    variant_ids: list[UUID] = Field(min_length=2, max_length=5)
    traffic_split: dict[UUID, int] = Field(
        description="variant_id → integer percentage. Must sum to 100."
    )
    min_runtime_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    max_runtime_hours: int | None = Field(default=None, ge=1, le=24 * 60)
