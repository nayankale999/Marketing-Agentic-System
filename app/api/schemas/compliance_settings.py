"""Pydantic schemas for /api/compliance-settings (W29, E16-S04)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ComplianceSettingsUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    postal_address: str | None = Field(default=None, max_length=2000)
    unsubscribe_secret: str | None = Field(default=None, max_length=200)


class ComplianceSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID | None
    tenant_id: UUID
    postal_address: str | None
    # Boolean indicator only — never expose the actual secret over the API.
    has_unsubscribe_secret: bool
    created_at: datetime | None
    updated_at: datetime | None
