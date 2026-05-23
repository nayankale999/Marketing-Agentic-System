"""Pydantic schemas for custom KPI + spend reconciliation endpoints
(W41, E10-S07 / E10-S06)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Custom KPI
# ---------------------------------------------------------------------------


class CustomKpiOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    campaign_id: UUID | None
    name: str
    formula: dict[str, Any]
    deleted_at: datetime | None
    created_by: UUID | None
    created_at: datetime


class CustomKpiListResponse(BaseModel):
    items: list[CustomKpiOut]
    total: int


class CreateCustomKpiRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    campaign_id: UUID | None = None
    formula: dict[str, Any]


class CustomKpiEvaluation(BaseModel):
    kpi_id: UUID
    name: str
    value: int | None
    missing_event: bool
    message: str | None


class CustomKpiEvaluationListResponse(BaseModel):
    items: list[CustomKpiEvaluation]
    total: int


# ---------------------------------------------------------------------------
# Spend reconciliation
# ---------------------------------------------------------------------------


class SpendReconciliationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    campaign_id: UUID
    period_start: date
    period_end: date
    committed_amount: Decimal
    invoiced_amount: Decimal
    delta_pct: Decimal
    status: str
    note: str | None
    resolved_at: datetime | None
    resolved_by: UUID | None
    created_at: datetime


class SpendReconciliationListResponse(BaseModel):
    items: list[SpendReconciliationOut]
    total: int


class RunReconciliationRequest(BaseModel):
    period_start: date
    period_end: date
    invoices: dict[UUID, Decimal] = Field(
        description="campaign_id → invoiced amount"
    )


class ExplainReconciliationRequest(BaseModel):
    note: str = Field(min_length=1, max_length=5000)


class DisputeReconciliationRequest(BaseModel):
    note: str | None = None
