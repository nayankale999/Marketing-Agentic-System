"""Schemas for /api/approval-settings (W26, E07-S03/04)."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ApprovalSettingsUpsert(BaseModel):
    """Body for `PUT /api/approval-settings`. Both fields nullable so an
    admin can clear either gate by sending `null`."""

    model_config = ConfigDict(extra="forbid")

    admin_required_above_amount: Decimal | None = Field(
        default=None, ge=Decimal("0")
    )
    auto_approval_cap_amount: Decimal = Field(
        default=Decimal("0"), ge=Decimal("0")
    )
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")


class ApprovalSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID | None
    tenant_id: UUID
    admin_required_above_amount: Decimal | None
    auto_approval_cap_amount: Decimal
    currency: str
    created_at: datetime | None
    updated_at: datetime | None
