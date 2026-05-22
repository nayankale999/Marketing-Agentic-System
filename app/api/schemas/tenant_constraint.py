"""Pydantic schemas for /api/tenant-constraints (W20, E05-S05)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.enums import ChannelPlatform, TenantConstraintKind


class TenantConstraintCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: TenantConstraintKind
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_payload(self) -> "TenantConstraintCreate":
        # Per-kind payload validation. `forbid_channel` MUST name a real
        # platform; `hard_cap` MUST name a platform + per + limit.
        if self.kind == TenantConstraintKind.forbid_channel:
            platform = self.payload.get("platform")
            if not isinstance(platform, str) or platform not in {p.value for p in ChannelPlatform}:
                raise ValueError(
                    "forbid_channel requires payload.platform to be a valid channel platform"
                )
        elif self.kind == TenantConstraintKind.hard_cap:
            platform = self.payload.get("platform")
            per = self.payload.get("per")
            limit = self.payload.get("limit")
            if not isinstance(platform, str) or platform not in {p.value for p in ChannelPlatform}:
                raise ValueError("hard_cap requires payload.platform to be a valid channel platform")
            if per not in {"day", "week", "month"}:
                raise ValueError("hard_cap requires payload.per to be day|week|month")
            if not isinstance(limit, int) or limit <= 0:
                raise ValueError("hard_cap requires payload.limit to be a positive integer")
        return self


class TenantConstraintOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    kind: str
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class TenantConstraintListResponse(BaseModel):
    items: list[TenantConstraintOut]
    total: int
