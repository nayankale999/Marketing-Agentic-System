"""Pydantic schemas for /api/ab-tests (W23, E06-S05)."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


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
