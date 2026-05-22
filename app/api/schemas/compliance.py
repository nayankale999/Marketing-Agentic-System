"""Pydantic schemas for /api/compliance-rules (W23, E06-S08)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import CompliancePatternKind, ComplianceSeverity


class ComplianceRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(min_length=1, max_length=500)
    pattern_kind: CompliancePatternKind = CompliancePatternKind.exact
    severity: ComplianceSeverity = ComplianceSeverity.warn
    description: str | None = None


class ComplianceRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    keyword: str
    pattern_kind: str
    severity: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class ComplianceRuleListResponse(BaseModel):
    items: list[ComplianceRuleOut]
    total: int
