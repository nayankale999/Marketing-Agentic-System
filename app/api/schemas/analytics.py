"""Pydantic schemas for the analytics agent surface (W37, E10-S02/S03)."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class MetricAnomalyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    campaign_id: UUID
    metric: str
    window_start: datetime
    window_end: datetime
    observed_value: Decimal
    baseline_median: Decimal
    baseline_stddev: Decimal
    sigma: Decimal
    severity: str
    dismissed_at: datetime | None
    dismissed_by: UUID | None
    created_at: datetime


class MetricAnomalyListResponse(BaseModel):
    items: list[MetricAnomalyOut]
    total: int


class OptimisationRecommendationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    campaign_id: UUID
    kind: str
    proposal: dict[str, Any]
    rationale: str | None
    predicted_uplift: Decimal | None
    status: str
    applied_at: datetime | None
    applied_by: UUID | None
    created_at: datetime


class RecommendationListResponse(BaseModel):
    items: list[OptimisationRecommendationOut]
    total: int
